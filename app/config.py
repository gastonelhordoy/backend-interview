import os
import secrets
from dataclasses import dataclass, field
from typing import Dict


def _env(name: str, default: str) -> str:
    return os.getenv(name) or default


def _service_urls(value: str) -> Dict[str, str]:
    """``"notes=http://notes-svc:8000, tasks=http://notes-svc:8000"`` → ``{"notes": ..., "tasks": ...}``."""
    urls = {}
    for item in filter(None, (part.strip() for part in value.split(","))):
        service, sep, url = item.partition("=")
        if not sep or not service.strip() or not url.strip():
            raise ValueError(f"MESH_REMOTE_SERVICES: expected service=url, got {item!r}")
        urls[service.strip()] = url.strip().rstrip("/")
    return urls


@dataclass
class Settings:
    """Runtime configuration, read from environment variables at import time."""

    database_url: str = field(default_factory=lambda: _env("DATABASE_URL", "sqlite:///./meetings.db"))
    # Without SECRET_KEY a random key is generated: safe, but tokens die on restart.
    secret_key: str = field(default_factory=lambda: _env("SECRET_KEY", secrets.token_urlsafe(32)))
    access_token_ttl_minutes: int = field(default_factory=lambda: int(_env("ACCESS_TOKEN_TTL_MINUTES", "60")))
    password_hash_iterations: int = field(default_factory=lambda: int(_env("PASSWORD_HASH_ITERATIONS", "600000")))
    # Services reached over HTTP instead of in-process (see app/mesh/factory.py).
    # Empty (the default): every service runs in this process.
    mesh_remote_services: Dict[str, str] = field(
        default_factory=lambda: _service_urls(_env("MESH_REMOTE_SERVICES", "")))
    mesh_remote_timeout_seconds: float = field(
        default_factory=lambda: float(_env("MESH_REMOTE_TIMEOUT_SECONDS", "10")))
    # MCP OAuth: short access tokens (only valid at /mcp), long rotating refresh tokens.
    mcp_access_token_ttl_minutes: int = field(default_factory=lambda: int(_env("MCP_ACCESS_TOKEN_TTL_MINUTES", "30")))
    mcp_refresh_token_ttl_days: int = field(default_factory=lambda: int(_env("MCP_REFRESH_TOKEN_TTL_DAYS", "30")))
    # Public origin used in OAuth metadata and token audiences; defaults to the request's.
    public_base_url: str = field(default_factory=lambda: _env("PUBLIC_BASE_URL", "").rstrip("/"))
    cache_ttl_seconds: float = field(default_factory=lambda: float(_env("CACHE_TTL_SECONDS", "300")))


settings = Settings()
