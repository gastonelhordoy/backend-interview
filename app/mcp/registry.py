"""Expose HTTP endpoints as MCP tools.

``@mcp_tool`` marks an endpoint function. Path, method and parameters are not repeated
in the decorator: they are read from the FastAPI route the function is registered on,
so the tool always matches what the API actually accepts.
"""
from __future__ import annotations

import inspect
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple, Type
from urllib.parse import quote

from fastapi import FastAPI
from fastapi.dependencies.utils import get_flat_dependant
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, create_model

_MARKER = "__mcp_tool__"


@dataclass(frozen=True)
class ToolOptions:
    name: Optional[str]
    title: Optional[str]
    description: Optional[str]


def mcp_tool(fn: Optional[Callable] = None, *, name: Optional[str] = None, title: Optional[str] = None,
             description: Optional[str] = None):
    """Publish an endpoint as an MCP tool. Defaults: name = function name,
    description = docstring."""
    def apply(f):
        setattr(f, _MARKER, ToolOptions(name, title, description))
        return f
    return apply(fn) if fn is not None else apply


@dataclass(frozen=True)
class EndpointTool:
    name: str
    title: Optional[str]
    description: str
    method: str
    path: str
    args_model: Type[BaseModel]
    path_params: Tuple[str, ...]
    query_params: Tuple[str, ...]
    body_params: Tuple[str, ...]
    embed_body: bool

    def definition(self) -> Dict[str, Any]:
        """Tool definition as returned by MCP ``tools/list``."""
        definition = {
            "name": self.name,
            "description": f"{self.description}\n\nHTTP: {self.method} {self.path}",
            "inputSchema": self.args_model.model_json_schema(),
            "annotations": {
                "readOnlyHint": self.method == "GET",
                "destructiveHint": self.method == "DELETE",
                "idempotentHint": self.method in ("GET", "PUT", "DELETE"),
            },
        }
        if self.title:
            definition["title"] = self.title
        return definition

    def build_request(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Validate tool arguments and turn them into ``httpx`` request kwargs.
        Raises ``pydantic.ValidationError`` for bad arguments."""
        args = self.args_model.model_validate(arguments)
        # exclude_unset: omitted fields of partial updates must not be sent as nulls.
        values = args.model_dump(mode="json", exclude_unset=True)
        url = re.sub(r"{(\w+)}", lambda m: quote(str(getattr(args, m.group(1))), safe=""), self.path)
        request: Dict[str, Any] = {"method": self.method, "url": url}
        query = {k: values[k] for k in self.query_params if values.get(k) is not None}
        if query:
            request["params"] = query
        if self.body_params:
            request["json"] = ({k: values.get(k) for k in self.body_params} if self.embed_body
                               else values.get(self.body_params[0], {}))
        return request


def _build_tool(route: APIRoute, options: ToolOptions) -> EndpointTool:
    name = options.name or route.endpoint.__name__
    dependant = get_flat_dependant(route.dependant, skip_repeats=True)
    fields: Dict[str, Any] = {}
    for param in (*dependant.path_params, *dependant.query_params, *dependant.body_params):
        default = ... if param.required else param.default
        fields[param.alias] = (param.field_info.annotation, Field(default, description=param.field_info.description))
    method = sorted(route.methods)[0]
    return EndpointTool(
        name=name,
        title=options.title,
        description=options.description or inspect.getdoc(route.endpoint) or name,
        method=method,
        path=route.path,
        args_model=create_model(f"{name}_input", __config__=ConfigDict(extra="forbid"), **fields),
        path_params=tuple(p.alias for p in dependant.path_params),
        query_params=tuple(p.alias for p in dependant.query_params),
        body_params=tuple(p.alias for p in dependant.body_params),
        embed_body=len(dependant.body_params) > 1 or bool(getattr(dependant.body_params[0].field_info, "embed", False))
        if dependant.body_params else False,
    )


def endpoint_tools(app: FastAPI) -> Dict[str, EndpointTool]:
    """All ``@mcp_tool`` endpoints of ``app`` by tool name (built once, after routing is final)."""
    tools = getattr(app.state, "mcp_tools", None)
    if tools is None:
        tools = {}
        for route in app.routes:
            options = getattr(getattr(route, "endpoint", None), _MARKER, None)
            if not isinstance(route, APIRoute) or options is None:
                continue
            tool = _build_tool(route, options)
            if tool.name in tools:
                raise ValueError(f"Duplicate MCP tool name {tool.name!r}")
            tools[tool.name] = tool
        app.state.mcp_tools = tools
    return tools


def tool_definitions(app: FastAPI) -> List[Dict[str, Any]]:
    return [tool.definition() for tool in endpoint_tools(app).values()]
