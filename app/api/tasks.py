from typing import List, Optional

from fastapi import APIRouter, Depends, Query, Response

from app import schemas
from app.api.deps import get_mesh
from app.mcp.registry import mcp_tool
from app.mesh.mesh import ServiceMesh
from app.models import TaskStatus

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.post("/", response_model=schemas.Task, status_code=201)
@mcp_tool
def create_task(data: schemas.TaskCreate, mesh: ServiceMesh = Depends(get_mesh)):
    """
    Create a new task that is due at a meeting you attend.
    """
    return mesh.tasks.create(data=data)


@router.get("/", response_model=List[schemas.Task])
@mcp_tool
def list_tasks(meeting_id: Optional[int] = Query(None), status: Optional[TaskStatus] = Query(None),
               mesh: ServiceMesh = Depends(get_mesh)):
    """
    List tasks of the meetings you attend, optionally filtered by meeting_id or status.
    """
    return mesh.tasks.list(meeting_id=meeting_id, status=status)


@router.get("/{task_id}", response_model=schemas.Task)
@mcp_tool
def get_task(task_id: int, mesh: ServiceMesh = Depends(get_mesh)):
    """
    Get a specific task by ID.
    """
    return mesh.tasks.get(task_id=task_id)


@router.put("/{task_id}", response_model=schemas.Task)
@mcp_tool
def update_task(task_id: int, data: schemas.TaskUpdate, mesh: ServiceMesh = Depends(get_mesh)):
    """
    Update a task (e.g., change status, reassign to different meeting). Task creator or meeting owner only.
    """
    return mesh.tasks.update(task_id=task_id, data=data)


@router.delete("/{task_id}", status_code=204)
@mcp_tool
def delete_task(task_id: int, mesh: ServiceMesh = Depends(get_mesh)):
    """
    Delete a task. Task creator or meeting owner only.
    """
    mesh.tasks.delete(task_id=task_id)
    return Response(status_code=204)
