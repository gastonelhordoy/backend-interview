"""Minimal MCP server (Streamable HTTP transport, JSON responses, stateless) at ``/mcp``.

Implements ``initialize``, ``ping``, ``tools/list`` and ``tools/call``. Every request
needs an OAuth access token issued for this resource (see ``app/oauth``); without one the
server answers 401 with the metadata URL clients use to start the login flow.

A tool call is replayed as an HTTP request against this same app, authenticated as the
token's user with a short-lived REST API token, so validation and privacy rules are
exactly those of the REST API.
"""
import json
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.auth.security import create_access_token
from app.errors import AuthenticationError
from app.mcp.registry import endpoint_tools, tool_definitions
from app.mesh.mesh import Principal
from app.oauth.router import get_oauth_service
from app.oauth.service import MCP_SCOPE, OAuthService

SUPPORTED_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "meeting-notes-api", "version": "1.0.0"}
INSTRUCTIONS = ("Tools for the Meeting Notes API. Every call runs as the user who signed in for this "
                "connection; you only see meetings that user attends.")
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS = -32700, -32600, -32601, -32602

router = APIRouter()


class RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@router.post("/mcp", include_in_schema=False)
async def mcp_endpoint(request: Request, oauth: OAuthService = Depends(get_oauth_service)):
    # Browsers send Origin; only accept local pages (DNS-rebinding protection, required by the spec).
    origin = request.headers.get("origin")
    if origin and urlparse(origin).hostname not in _LOCAL_HOSTS:
        return JSONResponse(_error(None, INVALID_REQUEST, "Origin not allowed"), status_code=403)
    principal = await run_in_threadpool(_authenticate, request, oauth)
    if principal is None:
        return _unauthorized(request, oauth)
    try:
        message = await request.json()
    except ValueError:
        return JSONResponse(_error(None, PARSE_ERROR, "Parse error"), status_code=400)
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return JSONResponse(_error(None, INVALID_REQUEST, "Expected a single JSON-RPC 2.0 message"), status_code=400)
    if "id" not in message or "method" not in message:
        return Response(status_code=202)  # notification or response: nothing to answer

    try:
        result = await _dispatch(request, principal, message["method"], message.get("params") or {})
    except RpcError as exc:
        return JSONResponse(_error(message["id"], exc.code, exc.message))
    return JSONResponse({"jsonrpc": "2.0", "id": message["id"], "result": result})


@router.get("/mcp", include_in_schema=False)
@router.delete("/mcp", include_in_schema=False)
async def mcp_unsupported():
    # Stateless server: no server-initiated SSE stream and no sessions to terminate.
    return Response(status_code=405, headers={"Allow": "POST"})


def _authenticate(request: Request, oauth: OAuthService) -> Optional[Principal]:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    try:
        return oauth.verify_access_token(token)
    except AuthenticationError:
        return None


def _unauthorized(request: Request, oauth: OAuthService) -> JSONResponse:
    """RFC 9728 §5.1: point the client at the protected resource metadata."""
    challenge = f'Bearer resource_metadata="{oauth.issuer}/.well-known/oauth-protected-resource/mcp", scope="{MCP_SCOPE}"'
    if request.headers.get("authorization"):
        challenge += ', error="invalid_token"'
    return JSONResponse({"error": "invalid_token", "error_description": "Sign in to use this MCP server"},
                        status_code=401, headers={"WWW-Authenticate": challenge})


async def _dispatch(request: Request, principal: Principal, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
    if method == "initialize":
        requested = params.get("protocolVersion")
        return {
            "protocolVersion": requested if requested in SUPPORTED_PROTOCOL_VERSIONS else SUPPORTED_PROTOCOL_VERSIONS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": INSTRUCTIONS,
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": tool_definitions(request.app)}
    if method == "tools/call":
        return await _call_tool(request, principal, params.get("name"), params.get("arguments") or {})
    raise RpcError(METHOD_NOT_FOUND, f"Method not found: {method}")


async def _call_tool(request: Request, principal: Principal, name: Optional[str],
                     arguments: Dict[str, Any]) -> Dict[str, Any]:
    tool = endpoint_tools(request.app).get(name)
    if tool is None:
        raise RpcError(INVALID_PARAMS, f"Unknown tool: {name}")
    try:
        http_request = tool.build_request(arguments)
    except ValidationError as exc:
        return _tool_result(f"Invalid arguments: {exc}", is_error=True)

    # The MCP token is not valid for the REST API (different audience): mint a one-off one.
    headers = {"Authorization": f"Bearer {create_access_token(principal.user_id, ttl_minutes=1)}"}
    transport = httpx.ASGITransport(app=request.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://api.internal") as client:
        response = await client.request(headers=headers, **http_request)

    if response.status_code >= 400:
        return _tool_result(f"HTTP {response.status_code}: {response.text}", is_error=True)
    if not response.content:
        return _tool_result(f"HTTP {response.status_code}: done")
    return _tool_result(json.dumps(response.json(), ensure_ascii=False))


def _tool_result(text: str, is_error: bool = False) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _error(request_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
