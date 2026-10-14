from __future__ import annotations

from typing import List

from app import models, schemas
from app.errors import PermissionDeniedError
from app.mesh.registry import command, query, service
from app.services.base import BaseService

Meeting = models.Meeting
MeetingAttendee = models.MeetingAttendee


@service("meetings")
class MeetingsService(BaseService):
    """Meetings are visible to their attendees only; only the owner (creator) may
    change or delete them. This service owns attendance and is therefore the authority
    other services ask (via the mesh) about meeting access."""

    model = Meeting

    def _visible(self):
        return self.db.query(Meeting).join(MeetingAttendee).filter(MeetingAttendee.user_id == self.user_id)

    def _get_owned(self, meeting_id: int) -> Meeting:
        meeting = self._get_visible(meeting_id)
        if meeting.owner_id != self.user_id:
            raise PermissionDeniedError("Only the meeting owner can modify this meeting")
        return meeting

    def _resolve_attendees(self, attendee_ids: List[int]) -> List[int]:
        """The owner plus the invited users, after checking (via the users service) they exist."""
        invited = sorted(set(attendee_ids) - {self.user_id})
        if invited:
            self.mesh.users.ensure_exist(user_ids=invited)
        return [self.user_id, *invited]

    # Public operations

    @query(depends_on=("users",))
    def list(self) -> List[schemas.Meeting]:
        """List the meetings you attend, ordered by scheduled time."""
        meetings = self._visible().order_by(Meeting.scheduled_time, Meeting.id)
        return [schemas.Meeting.model_validate(m) for m in meetings]

    @query(depends_on=("users", "notes", "tasks"))
    def get(self, meeting_id: int) -> schemas.MeetingWithDetails:
        """Get a meeting you attend, with its attendees, notes and tasks."""
        meeting = schemas.Meeting.model_validate(self._get_visible(meeting_id))
        return schemas.MeetingWithDetails(
            **meeting.model_dump(),
            notes=self.mesh.notes.list(meeting_id=meeting_id),
            tasks=self.mesh.tasks.list(meeting_id=meeting_id),
        )

    @command
    def create(self, data: schemas.MeetingCreate) -> schemas.Meeting:
        """Schedule a meeting. You become its owner and an attendee."""
        attendee_ids = self._resolve_attendees(data.attendee_ids)
        meeting = Meeting(**data.model_dump(exclude={"attendee_ids"}), owner_id=self.user_id)
        meeting.attendee_links = [MeetingAttendee(user_id=uid) for uid in attendee_ids]
        return schemas.Meeting.model_validate(self._save(meeting))

    @command
    def update(self, meeting_id: int, data: schemas.MeetingUpdate) -> schemas.Meeting:
        """Update a meeting you own. ``attendee_ids`` replaces the attendee list."""
        meeting = self._get_owned(meeting_id)
        changes = data.model_dump(exclude_unset=True)
        attendee_ids = changes.pop("attendee_ids", None)
        if attendee_ids is not None:
            current = {link.user_id: link for link in meeting.attendee_links}
            meeting.attendee_links = [
                current.get(uid) or MeetingAttendee(user_id=uid) for uid in self._resolve_attendees(attendee_ids)
            ]
        self._apply(meeting, changes)
        return schemas.Meeting.model_validate(self._save(meeting))

    @command
    def delete(self, meeting_id: int) -> None:
        """Delete a meeting you own, together with its notes and tasks."""
        self._delete(self._get_owned(meeting_id))

    # Internal operations, used by other services through the mesh

    @query(tool=False)
    def access(self, meeting_id: int) -> schemas.MeetingAccess:
        """Raise NotFound unless the current user attends the meeting."""
        meeting = self._get_visible(meeting_id)
        return schemas.MeetingAccess(meeting_id=meeting.id, owner_id=meeting.owner_id,
                                     is_owner=meeting.owner_id == self.user_id)

    @query(tool=False)
    def accessible_ids(self) -> List[int]:
        """Ids of all meetings the current user attends."""
        rows = self.db.query(MeetingAttendee.meeting_id).filter(MeetingAttendee.user_id == self.user_id)
        return sorted(row.meeting_id for row in rows)

    @query(tool=False)
    def co_attendee_ids(self) -> List[int]:
        """Ids of every user attending at least one meeting with the current user."""
        mine = self.db.query(MeetingAttendee.meeting_id).filter(MeetingAttendee.user_id == self.user_id)
        rows = self.db.query(MeetingAttendee.user_id).filter(MeetingAttendee.meeting_id.in_(mine)).distinct()
        return sorted(row.user_id for row in rows)
