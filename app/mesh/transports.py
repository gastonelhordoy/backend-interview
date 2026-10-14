"""Transports deliver a ``ServiceCall`` to a service. They share one interface and wrap
each other, so the communication mode is chosen by composition (see ``factory.py``)::

    CachingTransport(RoutingTransport(default=InProcessTransport(db),
                                      routes={"notes": HttpTransport(client_for_notes_node)}))

Adding a communication mode (gRPC, a message queue...) means writing one ``Transport``
and routing services to it; neither the mesh nor the services change.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple

import httpx
from sqlalchemy.orm import Session

from app.auth.security import MESH_AUDIENCE, create_access_token
from app.errors import ERRORS_BY_CODE, ServiceError, ServiceUnavailableError
from app.mesh.cache import Cache
from app.mesh.mesh import ServiceCall

if TYPE_CHECKING:
    from app.mesh.mesh import ServiceMesh
    from app.services.base import BaseService


class Transport(ABC):
    @abstractmethod
    def send(self, call: ServiceCall, mesh: ServiceMesh) -> Any:
        """Deliver ``call`` and return the operation's result. ``mesh`` is the caller's
        mesh; in-process services use it for their own downstream calls."""


class InProcessTransport(Transport):
    """The default: services live in this process.

    Holds a reference to each service it has served: the instance is created on first
    use, bound to the DB session, the caller's principal and mesh, and reused for every
    later call through this transport (one transport per mesh, i.e. per request).
    """

    def __init__(self, db: Session):
        self.db = db
        self._services: Dict[Tuple[str, int], BaseService] = {}

    def send(self, call: ServiceCall, mesh: ServiceMesh) -> Any:
        return getattr(self.service(call, mesh), call.spec.name)(**call.kwargs)

    def service(self, call: ServiceCall, mesh: ServiceMesh) -> BaseService:
        key = (call.spec.service, call.principal.user_id)
        if key not in self._services:
            self._services[key] = call.spec.service_cls(db=self.db, principal=call.principal, mesh=mesh)
        return self._services[key]


class CachingTransport(Transport):
    """Caches query results per user; commands invalidate the tags they affect.

    Entries are stored as JSON (via the operation's result adapter), so callers get a
    fresh object on every hit and the backing ``Cache`` can be out of process.
    """

    def __init__(self, inner: Transport, cache: Cache, ttl_seconds: float):
        self.inner = inner
        self.cache = cache
        self.ttl_seconds = ttl_seconds

    def send(self, call: ServiceCall, mesh: ServiceMesh) -> Any:
        spec = call.spec
        if not spec.is_query:
            result = self.inner.send(call, mesh)
            self.cache.invalidate_tags(spec.invalidation_tags)
            return result

        key = f"user:{call.principal.user_id}:{spec.service}.{spec.name}:{call.args.model_dump_json()}"
        cached = self.cache.get(key)
        if cached is not None:
            return spec.result_adapter.validate_json(cached)
        result = self.inner.send(call, mesh)
        self.cache.set(key, spec.result_adapter.dump_json(result).decode(), tags=spec.cache_tags,
                       ttl_seconds=self.ttl_seconds)
        return result


class RoutingTransport(Transport):
    """Sends each service's calls to its own transport (e.g. some services remote)."""

    def __init__(self, default: Transport, routes: Optional[Dict[str, Transport]] = None):
        self.default = default
        self.routes = routes or {}

    def send(self, call: ServiceCall, mesh: ServiceMesh) -> Any:
        return self.routes.get(call.spec.service, self.default).send(call, mesh)


class HttpTransport(Transport):
    """Calls a service running in another process (a microservice) over HTTP.

    Wire format, served by ``app/api/mesh.py`` on the remote node::

        POST /mesh/<service>/<operation>
        Authorization: Bearer <JWT, aud=meeting-notes-mesh, sub=<user id>, 1 minute>
        <operation arguments as JSON>

        200 {"result": <operation result as JSON>}
        4xx/5xx {"error": {"code": "not_found", "message": "..."}}

    The caller's identity travels as a short-lived token signed with the shared
    ``SECRET_KEY``, so the remote node runs the operation as the same user. Domain errors
    are re-raised by ``code``; network failures become ``ServiceUnavailableError``.
    """

    def __init__(self, client: httpx.Client):
        self.client = client  # its base_url is the remote node

    def send(self, call: ServiceCall, mesh: ServiceMesh) -> Any:
        spec = call.spec
        token = create_access_token(call.principal.user_id, audience=MESH_AUDIENCE, ttl_minutes=1)
        try:
            response = self.client.post(
                f"/mesh/{spec.service}/{spec.name}",
                # exclude_unset keeps partial updates partial: omitted fields must not become nulls.
                content=call.args.model_dump_json(exclude_unset=True),
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            )
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ServiceUnavailableError(f"Service {spec.service!r} is unavailable") from exc

        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            error = body["error"]
            raise ERRORS_BY_CODE.get(error.get("code"), ServiceError)(error.get("message", "Remote error"))
        if response.status_code != 200 or not isinstance(body, dict) or "result" not in body:
            raise ServiceUnavailableError(f"Service {spec.service!r} answered HTTP {response.status_code}")
        return spec.result_adapter.validate_python(body["result"])
