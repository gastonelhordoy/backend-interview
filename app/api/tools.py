from typing import Any, Dict, List

from fastapi import APIRouter, Body, Depends

from app.api.deps import get_mesh
from app.mesh.mesh import ServiceMesh

router = APIRouter(prefix="/tools", tags=["tools"])


@router.get("/")
def list_tools(mesh: ServiceMesh = Depends(get_mesh)) -> List[Dict[str, Any]]:
    """
    Tool definitions (Anthropic format) for every operation exposed by the mesh.
    """
    return mesh.get_tools()


@router.post("/{tool_name}")
def call_tool(tool_name: str, arguments: Dict[str, Any] = Body(default={}),
              mesh: ServiceMesh = Depends(get_mesh)) -> Dict[str, Any]:
    """
    Execute a tool call as the authenticated user, as an LLM agent loop would.
    """
    return {"result": mesh.execute_tool(tool_name, arguments)}
