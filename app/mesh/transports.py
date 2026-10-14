"""Transports deliver a ``ServiceCall`` to a service. They share one interface and wrap
each other, so the communication mode is chosen by composition (see ``factory.py``)::

    CachingTransport(RoutingTransport(default=InProcessTransport(db),
                                      routes={"users": GrpcTransport("users:50051")}))
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Dict, Optional

from sqlalchemy.orm import Session

from app.errors import ERRORS_BY_CODE, ServiceError
from app.mesh.cache import Cache
from app.mesh.mesh import Principal, ServiceCall

if TYPE_CHECKING:
    from app.mesh.mesh import ServiceMesh


class Transport(ABC):
    @abstractmethod
    def send(self, call: ServiceCall, mesh: ServiceMesh) -> Any:
        """Deliver ``call`` and return the operation's result. ``mesh`` is the caller's
        mesh; in-process services use it for their own downstream calls."""


class InProcessTransport(Transport):
    """Instantiates the service locally, bound to the caller's DB session and mesh."""

    def __init__(self, db: Session):
        self.db = db

    def send(self, call: ServiceCall, mesh: ServiceMesh) -> Any:
        service = call.spec.service_cls(db=self.db, principal=call.principal, mesh=mesh)
        return getattr(service, call.spec.name)(**call.kwargs)


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


class LoopbackTransport(Transport):
    """Runs calls in-process but pushes request, result and errors through JSON, exactly
    as a network transport would. Used to prove services are wire-safe."""

    def __init__(self, inner: Transport):
        self.inner = inner

    def send(self, call: ServiceCall, mesh: ServiceMesh) -> Any:
        spec = call.spec
        request = json.dumps({
            "service": spec.service,
            "operation": spec.name,
            "user_id": call.principal.user_id,  # a real transport would forward the bearer token
            # exclude_unset keeps partial updates partial: omitted fields must not become nulls.
            "args": json.loads(call.args.model_dump_json(exclude_unset=True)),
        })
        response = json.loads(self._serve(request, mesh))
        if "error" in response:
            raise ERRORS_BY_CODE.get(response["error"]["code"], ServiceError)(response["error"]["message"])
        return spec.result_adapter.validate_python(response["result"])

    def _serve(self, raw_request: str, mesh: ServiceMesh) -> str:
        """The "server side": rebuild the call from the wire, run it, serialize the result."""
        request = json.loads(raw_request)
        spec = mesh.registry.operation(request["service"], request["operation"])
        call = ServiceCall(spec, spec.args_model.model_validate(request["args"]), Principal(request["user_id"]))
        try:
            result = self.inner.send(call, mesh)
        except ServiceError as exc:
            return json.dumps({"error": {"code": exc.code, "message": exc.message}})
        return json.dumps({"result": json.loads(spec.result_adapter.dump_json(result))})


class GrpcTransport(Transport):
    """Placeholder for production: forward ``ServiceCall``s to a remote mesh node.

    The wire format would be what ``LoopbackTransport`` already produces (service,
    operation, JSON args, caller credentials); the server side rebuilds the call with
    the registry and runs it through its own ``InProcessTransport``.
    """

    def __init__(self, target: str):
        self.target = target

    def send(self, call: ServiceCall, mesh: ServiceMesh) -> Any:
        raise NotImplementedError("gRPC transport is not implemented; use MESH_MODE=local or loopback")
