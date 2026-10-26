from typing import List

from fastapi import APIRouter, Depends

from app import schemas
from app.api.deps import get_mesh
from app.mcp.registry import mcp_tool
from app.mesh.mesh import ServiceMesh

router = APIRouter(prefix="/invitations", tags=["invitations"])


@router.get("/", response_model=List[schemas.ReceivedInvitation])
@mcp_tool
def list_my_invitations(mesh: ServiceMesh = Depends(get_mesh)):
    """
    List your pending meeting invitations: which meeting, when, and who invited you.
    """
    return mesh.meetings.received_invitations()


@router.post("/{invitation_id}/accept", response_model=schemas.Invitation)
@mcp_tool
def accept_invitation(invitation_id: int, mesh: ServiceMesh = Depends(get_mesh)):
    """
    Accept a pending invitation. You become an attendee of the meeting.
    """
    return mesh.meetings.accept_invitation(invitation_id=invitation_id)


@router.post("/{invitation_id}/decline", response_model=schemas.Invitation)
@mcp_tool
def decline_invitation(invitation_id: int, mesh: ServiceMesh = Depends(get_mesh)):
    """
    Decline a pending invitation.
    """
    return mesh.meetings.decline_invitation(invitation_id=invitation_id)
