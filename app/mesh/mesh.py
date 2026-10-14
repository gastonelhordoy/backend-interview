from __future__ import annotations

import functools
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, List, Mapping

from pydantic import BaseModel

from app.mesh.registry import OperationSpec, ServiceRegistry, default_registry

if TYPE_CHECKING:
    from app.mesh.transports import Transport


@dataclass(frozen=True)
class Principal:
    """The authenticated user every call in a mesh is made on behalf of."""

    user_id: int


@dataclass(frozen=True)
class ServiceCall:
    """One routed call. ``args`` is the validated input model, so transports can
    serialize it (``args.model_dump_json()``) or unpack it (``kwargs``)."""

    spec: OperationSpec
    args: BaseModel
    principal: Principal

    @property
    def kwargs(self) -> Dict[str, Any]:
        # Not model_dump(): nested models must stay models for the service method.
        return {name: getattr(self.args, name) for name in self.args.model_fields}


class ServiceMesh:
    """A per-user gateway to all services: the interface every service is given.

    Every call (from the API, from an LLM tool call, or from one service to another)
    goes through ``call`` → validation → ``transport.send``. The mesh decides *what* is
    called and on whose behalf; the transport decides *how* the call travels
    (in-process, cached, over HTTP...). Services never hold references to each other
    and cannot act as anyone but ``principal``.
    """

    def __init__(self, principal: Principal, transport: Transport, registry: ServiceRegistry = default_registry):
        self.principal = principal
        self.transport = transport
        self.registry = registry

    def call(self, service: str, operation: str, /, **kwargs: Any) -> Any:
        return self._dispatch(self.registry.operation(service, operation), kwargs)

    def __getattr__(self, name: str) -> ServiceProxy:
        # Only reached for unknown attributes: ``mesh.meetings.get(meeting_id=1)``.
        if name.startswith("_") or name not in self.registry:
            raise AttributeError(name)
        return ServiceProxy(self, name)

    def get_tools(self) -> List[Dict[str, Any]]:
        """Tool definitions for every exposed operation, ready to pass to an LLM."""
        return [spec.tool_definition() for spec in self.registry.tools()]

    def execute_tool(self, tool_name: str, arguments: Mapping[str, Any]) -> Any:
        """Run an LLM tool call as this user and return a JSON-compatible result."""
        spec = self.registry.tool(tool_name)
        return spec.result_adapter.dump_python(self._dispatch(spec, arguments), mode="json")

    def _dispatch(self, spec: OperationSpec, arguments: Mapping[str, Any]) -> Any:
        return self.transport.send(ServiceCall(spec, spec.validate_args(arguments), self.principal), self)


class ServiceProxy:
    def __init__(self, mesh: ServiceMesh, service: str):
        self._mesh = mesh
        self._service = service

    def __getattr__(self, operation: str):
        if operation.startswith("_"):
            raise AttributeError(operation)
        return functools.partial(self._mesh.call, self._service, operation)
