import base64
import html
from pathlib import Path
from string import Template
from typing import Any, Dict, Optional
from urllib.parse import unquote, urlsplit

from fastapi import APIRouter, Body, Depends, Form, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.errors import AuthenticationError
from app.oauth.service import (AuthorizationRequest, OAuthError, OAuthService, RedirectableOAuthError,
                               with_query)

router = APIRouter(include_in_schema=False)

_AUTHORIZE_PARAMS = ("client_id", "redirect_uri", "response_type", "state", "code_challenge",
                     "code_challenge_method", "scope", "resource")
_LOGIN_TEMPLATE = Template((Path(__file__).parent / "templates" / "login.html").read_text())
_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
_PAGE_HEADERS = {**_NO_STORE, "X-Frame-Options": "DENY", "Content-Security-Policy": "frame-ancestors 'none'",
                 "Referrer-Policy": "no-referrer"}


def issuer(request: Request) -> str:
    return settings.public_base_url or str(request.base_url).rstrip("/")


def get_oauth_service(request: Request, db: Session = Depends(get_db)) -> OAuthService:
    return OAuthService(db, issuer(request))


def _json_error(exc: OAuthError) -> JSONResponse:
    headers = {**_NO_STORE, **({"WWW-Authenticate": "Basic"} if exc.status_code == 401 else {})}
    return JSONResponse({"error": exc.error, "error_description": exc.description},
                        status_code=exc.status_code, headers=headers)


# Discovery

@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/mcp")
def protected_resource_metadata(service: OAuthService = Depends(get_oauth_service)):
    return service.protected_resource_metadata()


@router.get("/.well-known/oauth-authorization-server")
def authorization_server_metadata(service: OAuthService = Depends(get_oauth_service)):
    return service.authorization_server_metadata()


# Dynamic client registration

@router.post("/oauth/register", status_code=201)
def register_client(metadata: Dict[str, Any] = Body(...), service: OAuthService = Depends(get_oauth_service)):
    try:
        return JSONResponse(service.register_client(metadata), status_code=201, headers=_NO_STORE)
    except OAuthError as exc:
        return _json_error(exc)


# Authorization endpoint: the login page

def _login_page(auth: AuthorizationRequest, params: Dict[str, Optional[str]], email: str = "",
                error: Optional[str] = None, status_code: int = 200) -> HTMLResponse:
    hidden = "\n      ".join(
        f'<input type="hidden" name="{name}" value="{html.escape(value)}">'
        for name, value in params.items() if name in _AUTHORIZE_PARAMS and value is not None)
    page = _LOGIN_TEMPLATE.substitute(
        client_name=html.escape(auth.client.client_name or "An MCP client"),
        redirect_host=html.escape(urlsplit(auth.redirect_uri).netloc or auth.redirect_uri),
        hidden_fields=hidden,
        email=html.escape(email),
        error=f'<div class="error" role="alert">{html.escape(error)}</div>' if error else "",
    )
    return HTMLResponse(page, status_code=status_code, headers=_PAGE_HEADERS)


def _error_page(exc: OAuthError) -> HTMLResponse:
    body = (f"<!doctype html><meta charset=utf-8><title>Authorization error</title>"
            f"<h1>Authorization error</h1><p>{html.escape(exc.error)}: {html.escape(exc.description)}</p>")
    return HTMLResponse(body, status_code=400, headers=_PAGE_HEADERS)


def _authorize(service: OAuthService, params: Dict[str, Optional[str]], handler):
    try:
        return handler(service.validate_authorization_request(params))
    except RedirectableOAuthError as exc:
        location = with_query(exc.redirect_uri, {"error": exc.error, "error_description": exc.description,
                                                 "state": exc.state, "iss": service.issuer})
        return RedirectResponse(location, status_code=302)
    except OAuthError as exc:
        return _error_page(exc)


@router.get("/oauth/authorize")
def authorize_page(request: Request, service: OAuthService = Depends(get_oauth_service)):
    params = {name: request.query_params.get(name) for name in _AUTHORIZE_PARAMS}
    return _authorize(service, params, lambda auth: _login_page(auth, params))


@router.post("/oauth/authorize")
def authorize_submit(
    client_id: Optional[str] = Form(None), redirect_uri: Optional[str] = Form(None),
    response_type: Optional[str] = Form(None), state: Optional[str] = Form(None),
    code_challenge: Optional[str] = Form(None), code_challenge_method: Optional[str] = Form(None),
    scope: Optional[str] = Form(None), resource: Optional[str] = Form(None),
    email: str = Form(""), password: str = Form(""), action: str = Form("approve"),
    service: OAuthService = Depends(get_oauth_service),
):
    # Everything is re-validated: the hidden fields come back from the browser.
    params = dict(client_id=client_id, redirect_uri=redirect_uri, response_type=response_type, state=state,
                  code_challenge=code_challenge, code_challenge_method=code_challenge_method, scope=scope,
                  resource=resource)

    def handle(auth: AuthorizationRequest):
        if action != "approve":
            return RedirectResponse(service.deny_redirect(auth), status_code=302)
        try:
            return RedirectResponse(service.approve(auth, email, password), status_code=302)
        except AuthenticationError:
            return _login_page(auth, params, email=email, error="Incorrect email or password.", status_code=401)

    return _authorize(service, params, handle)


# Token and revocation endpoints

def _client_credentials(authorization: Optional[str], client_id: Optional[str],
                        client_secret: Optional[str]):
    """client_secret_basic (Authorization header) or client_secret_post / none (form)."""
    if authorization and authorization.lower().startswith("basic "):
        try:
            decoded = base64.b64decode(authorization[6:]).decode()
            basic_id, _, basic_secret = decoded.partition(":")
            return unquote(basic_id), unquote(basic_secret)
        except (ValueError, UnicodeDecodeError):
            raise OAuthError("invalid_client", "Malformed Basic credentials", status_code=401) from None
    return client_id, client_secret


@router.post("/oauth/token")
def token(
    grant_type: Optional[str] = Form(None), code: Optional[str] = Form(None),
    redirect_uri: Optional[str] = Form(None), code_verifier: Optional[str] = Form(None),
    refresh_token: Optional[str] = Form(None), scope: Optional[str] = Form(None),
    resource: Optional[str] = Form(None), client_id: Optional[str] = Form(None),
    client_secret: Optional[str] = Form(None), authorization: Optional[str] = Header(None),
    service: OAuthService = Depends(get_oauth_service),
):
    try:
        client = service.authenticate_client(*_client_credentials(authorization, client_id, client_secret))
        if grant_type == "authorization_code":
            tokens = service.exchange_code(client, code, redirect_uri, code_verifier, resource)
        elif grant_type == "refresh_token":
            tokens = service.refresh(client, refresh_token, scope, resource)
        else:
            raise OAuthError("unsupported_grant_type", "Supported: authorization_code, refresh_token")
    except OAuthError as exc:
        return _json_error(exc)
    return JSONResponse(tokens, headers=_NO_STORE)


@router.post("/oauth/revoke")
def revoke(
    token: Optional[str] = Form(None), client_id: Optional[str] = Form(None),
    client_secret: Optional[str] = Form(None), authorization: Optional[str] = Header(None),
    service: OAuthService = Depends(get_oauth_service),
):
    try:
        service.revoke(service.authenticate_client(*_client_credentials(authorization, client_id, client_secret)),
                       token)
    except OAuthError as exc:
        return _json_error(exc)
    return JSONResponse({}, headers=_NO_STORE)
