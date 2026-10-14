from typing import List, Optional

from fastapi import APIRouter, Depends, Query

from app import schemas
from app.api.deps import get_mesh
from app.mcp.registry import mcp_tool
from app.mesh.mesh import ServiceMesh

router = APIRouter(prefix="/notes", tags=["notes"])


@router.post("/", response_model=schemas.Note, status_code=201)
@mcp_tool
def create_note(data: schemas.NoteCreate, mesh: ServiceMesh = Depends(get_mesh)):
    """
    Create a new note for a meeting you attend.
    """
    return mesh.notes.create(data=data)


@router.get("/", response_model=List[schemas.Note])
@mcp_tool
def list_notes(meeting_id: Optional[int] = Query(None), mesh: ServiceMesh = Depends(get_mesh)):
    """
    List notes of the meetings you attend, optionally filtered by meeting_id.
    """
    return mesh.notes.list(meeting_id=meeting_id)


@router.get("/{note_id}", response_model=schemas.Note)
@mcp_tool
def get_note(note_id: int, mesh: ServiceMesh = Depends(get_mesh)):
    """
    Get a specific note by ID.
    """
    return mesh.notes.get(note_id=note_id)
