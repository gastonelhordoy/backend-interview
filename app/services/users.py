from __future__ import annotations

from typing import List

from app import models, schemas
from app.mesh.registry import command, query, service
from app.services.base import BaseService


@service("users")
class UsersService(BaseService):
    """Read access to user profiles. A user can see themselves, the people they share at
    least one meeting with, and the owners of meetings they have a pending invitation to.
    Others only ever see a ``UserProfile``: the email, plus the name if its owner chose to
    share it. Registration lives in ``app.auth`` because it happens before there is a user
    to build a mesh for."""

    model = models.User

    def _visible(self):
        visible_ids = {self.user_id, *self.mesh.meetings.co_attendee_ids(), *self.mesh.meetings.pending_inviter_ids()}
        return self.db.query(models.User).filter(models.User.id.in_(visible_ids))

    def _profile(self, user: models.User) -> schemas.UserProfile:
        shows_name = user.share_name or user.id == self.user_id
        return schemas.UserProfile(id=user.id, email=user.email, name=user.name if shows_name else None)

    @query
    def me(self) -> schemas.User:
        """Return the profile of the current user, including your sharing settings."""
        return schemas.User.model_validate(self.db.get(models.User, self.user_id))

    @command
    def update_me(self, data: schemas.UserUpdate) -> schemas.User:
        """Change your sharing settings: whether co-attendees and people who invite you see your name."""
        user = self.db.get(models.User, self.user_id)
        self._apply(user, data.model_dump(exclude_unset=True))
        return schemas.User.model_validate(self._save(user))

    @query(depends_on=("meetings",))
    def list(self) -> List[schemas.UserProfile]:
        """List the users visible to you: yourself, everyone who shares a meeting with you,
        and the owners of meetings you are invited to."""
        return [self._profile(u) for u in self._visible().order_by(models.User.id)]

    @query(depends_on=("meetings",))
    def get(self, user_id: int) -> schemas.UserProfile:
        """Get a user visible to you by id (email, plus name if they share it)."""
        return self._profile(self._get_visible(user_id))
