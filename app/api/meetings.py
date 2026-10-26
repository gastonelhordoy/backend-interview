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
    Create a new meeting. The creator owns it and always attends; `invitee_emails` are invited
    and become attendees once they accept.
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
    Update a meeting's title, description or time (owner only). Attendees change through invitations.
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


@router.post("/{meeting_id}/invitations", response_model=List[schemas.Invitation], status_code=201)
@mcp_tool
def invite_to_meeting(meeting_id: int, data: schemas.InvitationCreate, mesh: ServiceMesh = Depends(get_mesh)):
    """
    Invite people to a meeting you own, by email. They become attendees only once they accept.
    Returns all the meeting's invitations.
    """
    return mesh.meetings.invite(meeting_id=meeting_id, data=data)


@router.get("/{meeting_id}/invitations", response_model=List[schemas.Invitation])
@mcp_tool
def list_meeting_invitations(meeting_id: int, mesh: ServiceMesh = Depends(get_mesh)):
    """
    List the invitations of a meeting you own and their status (pending, accepted, declined).
    """
    return mesh.meetings.list_invitations(meeting_id=meeting_id)


@router.delete("/{meeting_id}/invitations/{invitation_id}", status_code=204)
@mcp_tool
def revoke_invitation(meeting_id: int, invitation_id: int, mesh: ServiceMesh = Depends(get_mesh)):
    """
    Withdraw a pending or declined invitation to a meeting you own.
    """
    mesh.meetings.revoke_invitation(meeting_id=meeting_id, invitation_id=invitation_id)
    return Response(status_code=204)


@router.delete("/{meeting_id}/attendees/{user_id}", status_code=204)
@mcp_tool
def remove_attendee(meeting_id: int, user_id: int, mesh: ServiceMesh = Depends(get_mesh)):
    """
    Remove an attendee from a meeting you own, or leave a meeting by passing your own user ID.
    """
    mesh.meetings.remove_attendee(meeting_id=meeting_id, user_id=user_id)
    return Response(status_code=204)
