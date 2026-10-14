from __future__ import annotations

from typing import List, Optional

from app import models, schemas
from app.mesh.registry import command, query, service
from app.services.base import BaseService

Note = models.Note


@service("notes")
class NotesService(BaseService):
    """Notes are visible to, and can be written by, the attendees of their meeting."""

    model = Note

    def _visible(self):
        return self.db.query(Note).filter(Note.meeting_id.in_(self.mesh.meetings.accessible_ids()))

    @query(depends_on=("meetings",))
    def list(self, meeting_id: Optional[int] = None) -> List[schemas.Note]:
        """List notes from the meetings you attend, optionally for a single meeting."""
        notes = self._visible()
        if meeting_id is not None:
            self.mesh.meetings.access(meeting_id=meeting_id)
            notes = notes.filter(Note.meeting_id == meeting_id)
        return [schemas.Note.model_validate(n) for n in notes.order_by(Note.created_at, Note.id)]

    @query(depends_on=("meetings",))
    def get(self, note_id: int) -> schemas.Note:
        """Get a note from a meeting you attend."""
        return schemas.Note.model_validate(self._get_visible(note_id))

    @command
    def create(self, data: schemas.NoteCreate) -> schemas.Note:
        """Add a note to a meeting you attend."""
        self.mesh.meetings.access(meeting_id=data.meeting_id)
        return schemas.Note.model_validate(self._save(Note(**data.model_dump(), author_id=self.user_id)))
