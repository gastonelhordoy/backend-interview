"""OAuth 2.1 authorization server for the MCP endpoint (MCP authorization spec).

Authorization code + PKCE (S256 only), dynamic client registration (RFC 7591),
resource indicators (RFC 8707) and rotating refresh tokens with reuse detection.
Access tokens are short JWTs whose audience is the MCP resource URL; refresh tokens are
opaque and only their hashes are stored.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Mapping, Optional
from urllib.parse import urlencode, urlsplit

from sqlalchemy.orm import Session

from app import models
from app.auth.security import create_access_token, decode_access_token, hash_opaque_token, new_opaque_token
from app.auth.service import AuthService
from app.config import settings
from app.errors import AuthenticationError
from app.mesh.mesh import Principal

MCP_SCOPE = "mcp"
SUPPORTED_GRANT_TYPES = ("authorization_code", "refresh_token")
SUPPORTED_AUTH_METHODS = ("none", "client_secret_post", "client_secret_basic")
CODE_TTL = timedelta(minutes=5)
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
_PRIVATE_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*\.[a-z0-9+.-]+$")  # e.g. com.example.app (RFC 8252 §7.1)
_VERIFIER = re.compile(r"^[A-Za-z0-9\-._~]{43,128}$")


class OAuthError(Exception):
    """Error reported to the client as ``{"error": ..., "error_description": ...}``."""

    def __init__(self, error: str, description: str, status_code: int = 400):
        super().__init__(description)
        self.error = error
        self.description = description
        self.status_code = status_code


class RedirectableOAuthError(OAuthError):
    """Authorization error that is sent back to the (already validated) redirect URI."""

    def __init__(self, redirect_uri: str, state: Optional[str], error: str, description: str):
        super().__init__(error, description)
        self.redirect_uri = redirect_uri
        self.state = state


@dataclass(frozen=True)
class AuthorizationRequest:
    client: models.OAuthClient
    redirect_uri: str
    state: Optional[str]
    code_challenge: str
    scope: str
    resource: str


def _now() -> datetime:
    return datetime.utcnow()


def _valid_redirect_uri(uri: str) -> bool:
    parts = urlsplit(uri)
    if parts.fragment or not parts.scheme:
        return False
    if parts.scheme == "https":
        return bool(parts.netloc)
    if parts.scheme == "http":
        return parts.hostname in _LOOPBACK_HOSTS
    return bool(_PRIVATE_SCHEME.match(parts.scheme))


def _redirect_matches(registered: str, requested: str) -> bool:
    if registered == requested:
        return True
    # Native apps listen on an ephemeral loopback port: ignore the port (RFC 8252 §7.3).
    a, b = urlsplit(registered), urlsplit(requested)
    return (a.scheme == b.scheme == "http" and a.hostname in _LOOPBACK_HOSTS and a.hostname == b.hostname
            and a.path == b.path and a.query == b.query)


def _pkce_matches(verifier: str, challenge: str) -> bool:
    digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return hmac.compare_digest(digest, challenge)


def with_query(uri: str, params: Mapping[str, Optional[str]]) -> str:
    query = urlencode({k: v for k, v in params.items() if v is not None})
    return f"{uri}{'&' if urlsplit(uri).query else '?'}{query}"


class OAuthService:
    def __init__(self, db: Session, issuer: str):
        self.db = db
        self.issuer = issuer
        self.resource = f"{issuer}/mcp"

    # Discovery

    def protected_resource_metadata(self) -> Dict[str, Any]:
        """RFC 9728: tells MCP clients which authorization server protects /mcp."""
        return {
            "resource": self.resource,
            "authorization_servers": [self.issuer],
            "scopes_supported": [MCP_SCOPE],
            "bearer_methods_supported": ["header"],
            "resource_name": "Meeting Notes API (MCP)",
        }

    def authorization_server_metadata(self) -> Dict[str, Any]:
        """RFC 8414."""
        return {
            "issuer": self.issuer,
            "authorization_endpoint": f"{self.issuer}/oauth/authorize",
            "token_endpoint": f"{self.issuer}/oauth/token",
            "registration_endpoint": f"{self.issuer}/oauth/register",
            "revocation_endpoint": f"{self.issuer}/oauth/revoke",
            "scopes_supported": [MCP_SCOPE],
            "response_types_supported": ["code"],
            "grant_types_supported": list(SUPPORTED_GRANT_TYPES),
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": list(SUPPORTED_AUTH_METHODS),
            "revocation_endpoint_auth_methods_supported": list(SUPPORTED_AUTH_METHODS),
            "authorization_response_iss_parameter_supported": True,
        }

    # Client registration (RFC 7591)

    def register_client(self, metadata: Mapping[str, Any]) -> Dict[str, Any]:
        redirect_uris = metadata.get("redirect_uris")
        if (not isinstance(redirect_uris, list) or not redirect_uris
                or not all(isinstance(u, str) and _valid_redirect_uri(u) for u in redirect_uris)):
            raise OAuthError("invalid_redirect_uri",
                             "redirect_uris must be https, http on a loopback host, or a private-use scheme")
        method = metadata.get("token_endpoint_auth_method", "client_secret_basic")  # RFC 7591 default
        grant_types = metadata.get("grant_types", ["authorization_code"])
        response_types = metadata.get("response_types", ["code"])
        if method not in SUPPORTED_AUTH_METHODS:
            raise OAuthError("invalid_client_metadata", f"Unsupported token_endpoint_auth_method {method!r}")
        if not isinstance(grant_types, list) or not set(grant_types) <= set(SUPPORTED_GRANT_TYPES):
            raise OAuthError("invalid_client_metadata", f"Supported grant_types: {list(SUPPORTED_GRANT_TYPES)}")
        if response_types != ["code"]:
            raise OAuthError("invalid_client_metadata", "Only response_types ['code'] is supported")

        secret = new_opaque_token() if method != "none" else None
        client = models.OAuthClient(
            id=uuid.uuid4().hex,
            client_name=str(metadata.get("client_name") or "")[:200] or None,
            redirect_uris=redirect_uris,
            grant_types=grant_types,
            token_endpoint_auth_method=method,
            hashed_secret=hash_opaque_token(secret) if secret else None,
        )
        self.db.add(client)
        self.db.commit()

        response = {
            "client_id": client.id,
            "client_id_issued_at": int(time.time()),
            "client_name": client.client_name,
            "redirect_uris": redirect_uris,
            "grant_types": grant_types,
            "response_types": response_types,
            "token_endpoint_auth_method": method,
            "scope": MCP_SCOPE,
        }
        if secret:
            response.update(client_secret=secret, client_secret_expires_at=0)
        return response

    def authenticate_client(self, client_id: Optional[str], client_secret: Optional[str]) -> models.OAuthClient:
        client = self.db.get(models.OAuthClient, client_id) if client_id else None
        if client is None:
            raise OAuthError("invalid_client", "Unknown client", status_code=401)
        if client.hashed_secret and not (
                client_secret and hmac.compare_digest(hash_opaque_token(client_secret), client.hashed_secret)):
            raise OAuthError("invalid_client", "Invalid client credentials", status_code=401)
        return client

    # Authorization endpoint

    def validate_authorization_request(self, params: Mapping[str, Optional[str]]) -> AuthorizationRequest:
        """Validate the client and redirect URI first: until both are trusted, errors must
        be shown to the user and never redirected (open-redirect protection)."""
        client = self.db.get(models.OAuthClient, params.get("client_id") or "")
        if client is None:
            raise OAuthError("invalid_client", "Unknown client_id")
        redirect_uri = params.get("redirect_uri")
        if redirect_uri is None and len(client.redirect_uris) == 1:
            redirect_uri = client.redirect_uris[0]
        if not redirect_uri or not any(_redirect_matches(r, redirect_uri) for r in client.redirect_uris):
            raise OAuthError("invalid_request", "redirect_uri is not registered for this client")

        state = params.get("state")

        def fail(error: str, description: str):
            raise RedirectableOAuthError(redirect_uri, state, error, description)

        if params.get("response_type") != "code":
            fail("unsupported_response_type", "response_type must be 'code'")
        if not params.get("code_challenge") or params.get("code_challenge_method") != "S256":
            fail("invalid_request", "PKCE with code_challenge_method=S256 is required")
        scope = params.get("scope") or MCP_SCOPE
        if set(scope.split()) - {MCP_SCOPE}:
            fail("invalid_scope", f"Supported scopes: {MCP_SCOPE}")
        resource = params.get("resource") or self.resource
        if resource.rstrip("/") != self.resource:
            fail("invalid_target", f"Unknown resource; this server protects {self.resource}")

        return AuthorizationRequest(client, redirect_uri, state, params["code_challenge"], MCP_SCOPE, self.resource)

    def approve(self, request: AuthorizationRequest, email: str, password: str) -> str:
        """Check the user's credentials and return the redirect URL carrying a new code."""
        user = AuthService(self.db).authenticate(email, password)  # raises AuthenticationError
        self.db.query(models.OAuthAuthorizationCode).filter(
            models.OAuthAuthorizationCode.expires_at < _now()).delete()
        code = new_opaque_token()
        self.db.add(models.OAuthAuthorizationCode(
            code_hash=hash_opaque_token(code), client_id=request.client.id, user_id=user.id,
            redirect_uri=request.redirect_uri, code_challenge=request.code_challenge, scope=request.scope,
            resource=request.resource, expires_at=_now() + CODE_TTL,
        ))
        self.db.commit()
        return with_query(request.redirect_uri, {"code": code, "state": request.state, "iss": self.issuer})

    def deny_redirect(self, request: AuthorizationRequest) -> str:
        return with_query(request.redirect_uri, {"error": "access_denied", "state": request.state,
                                                 "iss": self.issuer})

    # Token endpoint

    def exchange_code(self, client: models.OAuthClient, code: Optional[str], redirect_uri: Optional[str],
                      code_verifier: Optional[str], resource: Optional[str]) -> Dict[str, Any]:
        stored = self.db.get(models.OAuthAuthorizationCode, hash_opaque_token(code or ""))
        if stored is None or stored.client_id != client.id:
            raise OAuthError("invalid_grant", "Invalid authorization code")
        self.db.delete(stored)  # single use, whatever happens next
        self.db.commit()
        if stored.expires_at < _now():
            raise OAuthError("invalid_grant", "Authorization code expired")
        if redirect_uri != stored.redirect_uri:
            raise OAuthError("invalid_grant", "redirect_uri does not match the authorization request")
        if not code_verifier or not _VERIFIER.match(code_verifier) or not _pkce_matches(code_verifier,
                                                                                      stored.code_challenge):
            raise OAuthError("invalid_grant", "PKCE verification failed")
        self._check_resource(resource, stored.resource)

        grant = models.OAuthGrant(id=uuid.uuid4().hex, client_id=client.id, user_id=stored.user_id,
                                  scope=stored.scope, resource=stored.resource)
        self.db.add(grant)
        return self._issue_tokens(grant)

    def refresh(self, client: models.OAuthClient, refresh_token: Optional[str], scope: Optional[str],
                resource: Optional[str]) -> Dict[str, Any]:
        if "refresh_token" not in client.grant_types:
            raise OAuthError("unauthorized_client", "Client is not registered for refresh_token")
        stored = self.db.get(models.OAuthRefreshToken, hash_opaque_token(refresh_token or ""))
        grant = self.db.get(models.OAuthGrant, stored.grant_id) if stored else None
        if stored is None or grant is None or grant.client_id != client.id:
            raise OAuthError("invalid_grant", "Invalid refresh token")
        if grant.revoked_at is not None:
            raise OAuthError("invalid_grant", "Refresh token revoked")
        if stored.used_at is not None:
            # A rotated token came back: it leaked. Kill the whole grant (RFC 9700 §4.14.2).
            grant.revoked_at = _now()
            self.db.commit()
            raise OAuthError("invalid_grant", "Refresh token reuse detected; grant revoked")
        if stored.expires_at < _now():
            raise OAuthError("invalid_grant", "Refresh token expired")
        if scope and set(scope.split()) - set(grant.scope.split()):
            raise OAuthError("invalid_scope", "Cannot widen the scope on refresh")
        self._check_resource(resource, grant.resource)

        stored.used_at = _now()
        return self._issue_tokens(grant)

    def revoke(self, client: models.OAuthClient, token: Optional[str]) -> None:
        """RFC 7009. Revokes the grant behind a refresh token; unknown tokens are ignored."""
        stored = self.db.get(models.OAuthRefreshToken, hash_opaque_token(token or ""))
        grant = self.db.get(models.OAuthGrant, stored.grant_id) if stored else None
        if grant is not None and grant.client_id == client.id and grant.revoked_at is None:
            grant.revoked_at = _now()
            self.db.commit()

    def _check_resource(self, requested: Optional[str], granted: str) -> None:
        if requested and requested.rstrip("/") != granted:
            raise OAuthError("invalid_target", "Token was not granted for this resource")

    def _issue_tokens(self, grant: models.OAuthGrant) -> Dict[str, Any]:
        refresh_token = new_opaque_token()
        self.db.add(models.OAuthRefreshToken(
            token_hash=hash_opaque_token(refresh_token), grant_id=grant.id,
            expires_at=_now() + timedelta(days=settings.mcp_refresh_token_ttl_days),
        ))
        self.db.commit()
        ttl = settings.mcp_access_token_ttl_minutes
        access_token = create_access_token(grant.user_id, audience=grant.resource, ttl_minutes=ttl,
                                           claims={"sid": grant.id, "client_id": grant.client_id,
                                                   "scope": grant.scope})
        return {"access_token": access_token, "token_type": "Bearer", "expires_in": ttl * 60,
                "refresh_token": refresh_token, "scope": grant.scope}

    # Resource server side (/mcp)

    def verify_access_token(self, token: str) -> Principal:
        """Accept only MCP tokens issued for this resource whose grant is still active."""
        claims = decode_access_token(token, audience=self.resource)
        grant = self.db.get(models.OAuthGrant, claims.get("sid") or "")
        if grant is None or grant.revoked_at is not None or grant.user_id != claims["sub"]:
            raise AuthenticationError("Token has been revoked")
        return Principal(user_id=grant.user_id)

