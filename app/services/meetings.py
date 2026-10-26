from __future__ import annotations

from datetime import datetime
from typing import Dict, Iterable, List, Optional

from app import models, schemas
from app.errors import ConflictError, InvalidRequestError, NotFoundError, PermissionDeniedError
from app.mesh.registry import command, query, service
from app.services.base import BaseService

Meeting = models.Meeting
MeetingAttendee = models.MeetingAttendee
MeetingInvitation = models.MeetingInvitation
InvitationStatus = models.InvitationStatus


@service("meetings")
class MeetingsService(BaseService):
    """Meetings are visible to their attendees only; only the owner (creator) may
    change or delete them. This service owns attendance and invitations, and is therefore
    the authority other services ask (via the mesh) about meeting access.

    People join by invitation: the owner invites emails, and the invitee becomes an
    attendee only after accepting. Invitations are never resolved to users on the
    inviting side, so they reveal nothing about who is registered."""

    model = Meeting

    def _visible(self):
        return self.db.query(Meeting).join(MeetingAttendee).filter(MeetingAttendee.user_id == self.user_id)

    def _get_owned(self, meeting_id: int) -> Meeting:
        meeting = self._get_visible(meeting_id)
        if meeting.owner_id != self.user_id:
            raise PermissionDeniedError("Only the meeting owner can modify this meeting")
        return meeting

    def _my_email(self) -> str:
        return self.mesh.users.me().email.lower()

    def _new_invitations(self, meeting: Meeting, emails: Iterable[str]) -> List[MeetingInvitation]:
        """Invitations for the emails not invited yet (lower-cased, without the owner's own)."""
        skip = {invitation.email for invitation in meeting.invitations} | {self._my_email()}
        return [MeetingInvitation(email=email) for email in sorted({e.lower() for e in emails} - skip)]

    def _to_schema(self, meeting: Meeting,
                   profiles: Optional[Dict[int, schemas.UserProfile]] = None) -> schemas.Meeting:
        """Attendee profiles come from the users service, so each shows only what its owner shares."""
        if profiles is None:
            profiles = self._profiles()
        attendees = [profiles[link.user_id] for link in meeting.attendee_links if link.user_id in profiles]
        return schemas.Meeting.model_validate(meeting).model_copy(update={"attendees": attendees})

    def _profiles(self) -> Dict[int, schemas.UserProfile]:
        return {profile.id: profile for profile in self.mesh.users.list()}

    def _received(self):
        """Invitations addressed to the current user's email."""
        return self.db.query(MeetingInvitation).filter(MeetingInvitation.email == self._my_email())

    def _get_pending_received(self, invitation_id: int) -> MeetingInvitation:
        invitation = self._received().filter(MeetingInvitation.id == invitation_id,
                                             MeetingInvitation.status == InvitationStatus.PENDING).first()
        if invitation is None:
            raise NotFoundError(f"Invitation {invitation_id} not found")
        return invitation

    def _respond(self, invitation: MeetingInvitation, status: InvitationStatus) -> schemas.Invitation:
        invitation.status = status
        invitation.responded_at = datetime.utcnow()
        return schemas.Invitation.model_validate(self._save(invitation))

    # Public operations

    @query(depends_on=("users",))
    def list(self) -> List[schemas.Meeting]:
        """List the meetings you attend, ordered by scheduled time."""
        profiles = self._profiles()
        meetings = self._visible().order_by(Meeting.scheduled_time, Meeting.id)
        return [self._to_schema(m, profiles) for m in meetings]

    @query(depends_on=("users", "notes", "tasks"))
    def get(self, meeting_id: int) -> schemas.MeetingWithDetails:
        """Get a meeting you attend, with its attendees, notes and tasks."""
        meeting = self._to_schema(self._get_visible(meeting_id))
        return schemas.MeetingWithDetails(
            **meeting.model_dump(),
            notes=self.mesh.notes.list(meeting_id=meeting_id),
            tasks=self.mesh.tasks.list(meeting_id=meeting_id),
        )

    @command
    def create(self, data: schemas.MeetingCreate) -> schemas.Meeting:
        """Schedule a meeting. You become its owner and an attendee; ``invitee_emails`` are
        invited and attend once they accept."""
        meeting = Meeting(**data.model_dump(exclude={"invitee_emails"}), owner_id=self.user_id)
        meeting.attendee_links = [MeetingAttendee(user_id=self.user_id)]
        meeting.invitations = self._new_invitations(meeting, data.invitee_emails)
        return self._to_schema(self._save(meeting))

    @command
    def update(self, meeting_id: int, data: schemas.MeetingUpdate) -> schemas.Meeting:
        """Update the title, description or time of a meeting you own."""
        meeting = self._get_owned(meeting_id)
        self._apply(meeting, data.model_dump(exclude_unset=True))
        return self._to_schema(self._save(meeting))

    @command
    def delete(self, meeting_id: int) -> None:
        """Delete a meeting you own, together with its notes, tasks and invitations."""
        self._delete(self._get_owned(meeting_id))

    @command
    def invite(self, meeting_id: int, data: schemas.InvitationCreate) -> List[schemas.Invitation]:
        """Invite people to a meeting you own, by email. They attend only once they accept.
        Emails already invited keep their invitation. Returns all the meeting's invitations."""
        meeting = self._get_owned(meeting_id)
        meeting.invitations.extend(self._new_invitations(meeting, data.emails))
        self._save(meeting)
        return self.list_invitations(meeting_id=meeting_id)

    @query
    def list_invitations(self, meeting_id: int) -> List[schemas.Invitation]:
        """List the invitations of a meeting you own, with their status."""
        self._get_owned(meeting_id)
        invitations = self.db.query(MeetingInvitation).filter(MeetingInvitation.meeting_id == meeting_id)
        return [schemas.Invitation.model_validate(i) for i in invitations.order_by(MeetingInvitation.id)]

    @command
    def revoke_invitation(self, meeting_id: int, invitation_id: int) -> None:
        """Withdraw a pending or declined invitation to a meeting you own."""
        self._get_owned(meeting_id)
        invitation = self.db.query(MeetingInvitation).filter(MeetingInvitation.meeting_id == meeting_id,
                                                             MeetingInvitation.id == invitation_id).first()
        if invitation is None:
            raise NotFoundError(f"Invitation {invitation_id} not found")
        if invitation.status == InvitationStatus.ACCEPTED:
            raise ConflictError("The invitation was accepted; remove the attendee instead")
        self._delete(invitation)

    @command
    def remove_attendee(self, meeting_id: int, user_id: int) -> None:
        """Remove an attendee from a meeting you own, or leave a meeting by passing your own id.
        Access ends immediately; the person can be invited again."""
        meeting = self._get_visible(meeting_id)
        if user_id == meeting.owner_id:
            raise InvalidRequestError("The owner cannot leave the meeting; delete it instead")
        if user_id != self.user_id and meeting.owner_id != self.user_id:
            raise PermissionDeniedError("Only the meeting owner can remove other attendees")
        link = next((link for link in meeting.attendee_links if link.user_id == user_id), None)
        if link is None:
            raise NotFoundError(f"User {user_id} does not attend meeting {meeting_id}")
        meeting.attendee_links.remove(link)
        meeting.invitations = [i for i in meeting.invitations if i.user_id != user_id]
        self.db.commit()

    @query(depends_on=("users",))
    def received_invitations(self) -> List[schemas.ReceivedInvitation]:
        """List your pending invitations: which meeting, when, and who invited you."""
        invitations = self._received().filter(MeetingInvitation.status == InvitationStatus.PENDING)
        return [
            schemas.ReceivedInvitation(
                id=i.id, meeting_id=i.meeting_id, title=i.meeting.title, scheduled_time=i.meeting.scheduled_time,
                invited_by=self.mesh.users.get(user_id=i.meeting.owner_id), status=i.status, created_at=i.created_at,
            )
            for i in invitations.order_by(MeetingInvitation.id)
        ]

    @command
    def accept_invitation(self, invitation_id: int) -> schemas.Invitation:
        """Accept one of your pending invitations: you become an attendee of the meeting."""
        invitation = self._get_pending_received(invitation_id)
        invitation.user_id = self.user_id
        if all(link.user_id != self.user_id for link in invitation.meeting.attendee_links):
            invitation.meeting.attendee_links.append(MeetingAttendee(user_id=self.user_id))
        return self._respond(invitation, InvitationStatus.ACCEPTED)

    @command
    def decline_invitation(self, invitation_id: int) -> schemas.Invitation:
        """Decline one of your pending invitations. The owner sees it as declined."""
        return self._respond(self._get_pending_received(invitation_id), InvitationStatus.DECLINED)

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

    @query(tool=False, depends_on=("users",))
    def pending_inviter_ids(self) -> List[int]:
        """Ids of the owners of meetings the current user has a pending invitation to."""
        rows = (self.db.query(Meeting.owner_id).join(MeetingInvitation)
                .filter(MeetingInvitation.email == self._my_email(),
                        MeetingInvitation.status == InvitationStatus.PENDING).distinct())
        return sorted(row.owner_id for row in rows)
