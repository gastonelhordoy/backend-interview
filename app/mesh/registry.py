"""Service registration and operation metadata.

``@service`` registers a class with the mesh; ``@query`` / ``@command`` mark the methods
the mesh may route to. From each marked method's signature the registry derives:

* an input model: validates every call, and doubles as the LLM tool ``input_schema``;
* a result adapter: (de)serializes results for the cache and for remote transports.
"""
from __future__ import annotations

import inspect
import typing
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Tuple, Type

from pydantic import BaseModel, ConfigDict, TypeAdapter, create_model

from app.errors import NotFoundError

QUERY = "query"
COMMAND = "command"
_MARKER = "__mesh_operation__"


@dataclass(frozen=True)
class OperationOptions:
    kind: str
    depends_on: Tuple[str, ...] = ()
    invalidates: Tuple[str, ...] = ()
    tool: bool = True
    description: Optional[str] = None


def query(fn: Optional[Callable] = None, *, depends_on: Iterable[str] = (), tool: bool = True,
          description: Optional[str] = None):
    """Mark a read-only operation. Its results are cached per user and tagged with the
    owning service plus ``depends_on`` (other services whose writes can change them).
    ``tool=False`` keeps internal operations out of ``get_tools()``."""
    return _mark(fn, OperationOptions(QUERY, depends_on=tuple(depends_on), tool=tool, description=description))


def command(fn: Optional[Callable] = None, *, invalidates: Iterable[str] = (), tool: bool = True,
            description: Optional[str] = None):
    """Mark a state-changing operation. On success it invalidates cached results tagged
    with the owning service plus ``invalidates``."""
    return _mark(fn, OperationOptions(COMMAND, invalidates=tuple(invalidates), tool=tool, description=description))


def _mark(fn, options: OperationOptions):
    def apply(f):
        setattr(f, _MARKER, options)
        return f
    return apply(fn) if fn is not None else apply


@dataclass(frozen=True)
class OperationSpec:
    service: str
    name: str
    service_cls: type
    options: OperationOptions
    description: str
    args_model: Type[BaseModel]
    result_adapter: TypeAdapter

    @property
    def is_query(self) -> bool:
        return self.options.kind == QUERY

    @property
    def tool_name(self) -> str:
        return f"{self.service}_{self.name}"

    @property
    def cache_tags(self) -> Tuple[str, ...]:
        return (self.service, *self.options.depends_on)

    @property
    def invalidation_tags(self) -> Tuple[str, ...]:
        return (self.service, *self.options.invalidates)

    def tool_definition(self) -> Dict[str, Any]:
        """Tool definition in the Anthropic Messages API format."""
        return {
            "name": self.tool_name,
            "description": self.description,
            "input_schema": self.args_model.model_json_schema(),
        }


def _build_spec(service_name: str, cls: type, attr: str, fn: Callable, options: OperationOptions) -> OperationSpec:
    hints = typing.get_type_hints(fn)
    fields: Dict[str, Any] = {}
    for param in list(inspect.signature(fn).parameters.values())[1:]:  # skip self
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            raise TypeError(f"{cls.__name__}.{attr}: *args/**kwargs cannot be routed through the mesh")
        default = ... if param.default is param.empty else param.default
        fields[param.name] = (hints.get(param.name, Any), default)

    return OperationSpec(
        service=service_name,
        name=attr,
        service_cls=cls,
        options=options,
        description=options.description or inspect.getdoc(fn) or f"{service_name}.{attr}",
        args_model=create_model(f"{cls.__name__}_{attr}_input", __config__=ConfigDict(extra="forbid"), **fields),
        result_adapter=TypeAdapter(hints.get("return", Any)),
    )


class ServiceRegistry:
    def __init__(self):
        self._services: Dict[str, Dict[str, OperationSpec]] = {}

    def register(self, name: str, cls: type) -> None:
        if name in self._services:
            raise ValueError(f"Service {name!r} is already registered")
        operations = {}
        for attr in dir(cls):
            member = getattr(cls, attr, None)
            options = getattr(member, _MARKER, None)
            if options is not None:
                operations[attr] = _build_spec(name, cls, attr, member, options)
        self._services[name] = operations

    def __contains__(self, service: str) -> bool:
        return service in self._services

    def operation(self, service: str, name: str) -> OperationSpec:
        try:
            return self._services[service][name]
        except KeyError:
            raise NotFoundError(f"Unknown operation {service}.{name}") from None

    def operations(self) -> Iterator[OperationSpec]:
        for operations in self._services.values():
            yield from operations.values()

    def tools(self) -> List[OperationSpec]:
        return [spec for spec in self.operations() if spec.options.tool]

    def tool(self, tool_name: str) -> OperationSpec:
        for spec in self.tools():
            if spec.tool_name == tool_name:
                return spec
        raise NotFoundError(f"Unknown tool {tool_name}")


default_registry = ServiceRegistry()


def service(name: str, registry: ServiceRegistry = default_registry):
    """Class decorator: register a service with the mesh under ``name``.

    Every ``@query``/``@command`` method becomes routable via
    ``mesh.call(name, op, ...)`` / ``mesh.<name>.<op>(...)``, and (unless ``tool=False``)
    is listed by ``mesh.get_tools()`` as ``<name>_<op>``.
    """
    def decorator(cls):
        cls.service_name = name
        registry.register(name, cls)
        return cls
    return decorator
