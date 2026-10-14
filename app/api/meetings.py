from typing import List

from fastapi import APIRouter, Depends, Response

from app import schemas
from app.api.deps import get_mesh
from app.mcp.registry import mcp_tool
from app.mesh.mesh import ServiceMesh

router = APIRouter(prefix="/meetings", tags=["meetings"])


@router.post("/", response_model=schemas.Meeting, status_code=201)
@mcp_tool
def create_meeting(data: schemas.MeetingCreate, mesh: ServiceMesh = Depends(get_mesh)):
    """
    Create a new meeting with attendees. The creator owns it and always attends.
    """
    return mesh.meetings.create(data=data)


@router.get("/", response_model=List[schemas.Meeting])
@mcp_tool
def list_meetings(mesh: ServiceMesh = Depends(get_mesh)):
    """
    List the meetings you attend.
    """
    return mesh.meetings.list()


@router.get("/{meeting_id}", response_model=schemas.MeetingWithDetails)
@mcp_tool
def get_meeting(meeting_id: int, mesh: ServiceMesh = Depends(get_mesh)):
    """
    Get a specific meeting with all details (attendees, notes, tasks).
    """
    return mesh.meetings.get(meeting_id=meeting_id)


@router.put("/{meeting_id}", response_model=schemas.Meeting)
@mcp_tool
def update_meeting(meeting_id: int, data: schemas.MeetingUpdate, mesh: ServiceMesh = Depends(get_mesh)):
    """
    Update a meeting (owner only).
    """
    return mesh.meetings.update(meeting_id=meeting_id, data=data)


@router.delete("/{meeting_id}", status_code=204)
@mcp_tool
def delete_meeting(meeting_id: int, mesh: ServiceMesh = Depends(get_mesh)):
    """
    Delete a meeting and its notes and tasks (owner only).
    """
    mesh.meetings.delete(meeting_id=meeting_id)
    return Response(status_code=204)
