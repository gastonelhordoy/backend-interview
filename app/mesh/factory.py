"""Builds each user's mesh and picks the transport composition from configuration.

* ``MESH_REMOTE_SERVICES`` empty (default): ``Caching → InProcess``. Everything in memory.
* ``MESH_REMOTE_SERVICES=notes=http://notes-svc:8000,...``:
  ``Caching → Routing(default=InProcess, notes=Http(notes-svc), ...)``.

The query cache comes from ``CACHE_URL``: in-memory by default, Redis when nodes must
share it.

``serve_call`` is the other end of ``HttpTransport``: it runs one operation that a remote
node sent to this process.
"""
from typing import Any, Dict, Mapping, Optional

import httpx
from sqlalchemy.orm import Session

import app.services  # noqa: F401  (registers every service with the mesh)
from app.config import settings
from app.mesh.cache import Cache, InMemoryCache, RedisCache
from app.mesh.mesh import Principal, ServiceCall, ServiceMesh
from app.mesh.registry import default_registry
from app.mesh.transports import CachingTransport, HttpTransport, InProcessTransport, RoutingTransport, Transport


def build_cache(url: str) -> Cache:
    """``CACHE_URL`` empty: in-memory, per process. ``redis://...``: shared by every node."""
    if not url:
        return InMemoryCache()
    if url.startswith(("redis://", "rediss://", "unix://")):
        return RedisCache.from_url(url)
    raise ValueError(f"CACHE_URL: unsupported scheme in {url!r}")


# Shared by all per-user meshes in this process; entries are keyed by user.
cache = build_cache(settings.cache_url)

_http_clients: Dict[str, httpx.Client] = {}


def http_client(base_url: str) -> httpx.Client:
    """One pooled HTTP client per remote node, shared by every request in this process."""
    if base_url not in _http_clients:
        _http_clients[base_url] = httpx.Client(base_url=base_url, timeout=settings.mesh_remote_timeout_seconds)
    return _http_clients[base_url]


def build_transport(db: Session, remote_services: Optional[Mapping[str, str]] = None) -> Transport:
    remote = settings.mesh_remote_services if remote_services is None else remote_services
    local: Transport = InProcessTransport(db)
    routes = {service: HttpTransport(http_client(url)) for service, url in remote.items()}
    inner = RoutingTransport(default=local, routes=routes) if routes else local
    return CachingTransport(inner, cache, settings.cache_ttl_seconds)


def build_mesh(principal: Principal, db: Session, remote_services: Optional[Mapping[str, str]] = None) -> ServiceMesh:
    return ServiceMesh(principal, build_transport(db, remote_services))


def serve_call(principal: Principal, db: Session, service: str, operation: str, arguments: Any) -> bytes:
    """Run an operation received from another node, in this process, and return the
    JSON-encoded result. The operation always runs locally (whatever the routes say,
    that is why it was sent here); its own downstream calls use this node's mesh."""
    spec = default_registry.operation(service, operation)
    call = ServiceCall(spec, spec.validate_args(arguments), principal)
    local = CachingTransport(InProcessTransport(db), cache, settings.cache_ttl_seconds)
    return spec.result_adapter.dump_json(local.send(call, build_mesh(principal, db)))
