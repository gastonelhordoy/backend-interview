from __future__ import annotations

from typing import List

from app import models, schemas
from app.errors import NotFoundError
from app.mesh.registry import query, service
from app.services.base import BaseService


@service("users")
class UsersService(BaseService):
    """Read access to user profiles. A user can see themselves and the people they
    share at least one meeting with. Registration lives in ``app.auth`` because it
    happens before there is a user to build a mesh for."""

    model = models.User

    def _visible(self):
        visible_ids = {self.user_id, *self.mesh.meetings.co_attendee_ids()}
        return self.db.query(models.User).filter(models.User.id.in_(visible_ids))

    @query
    def me(self) -> schemas.User:
        """Return the profile of the current user."""
        return schemas.User.model_validate(self.db.get(models.User, self.user_id))

    @query(depends_on=("meetings",))
    def list(self) -> List[schemas.User]:
        """List the users visible to you: yourself and everyone who shares a meeting with you."""
        return [schemas.User.model_validate(u) for u in self._visible().order_by(models.User.id)]

    @query(depends_on=("meetings",))
    def get(self, user_id: int) -> schemas.User:
        """Get a user you share a meeting with (or yourself) by id."""
        return schemas.User.model_validate(self._get_visible(user_id))

    @query(tool=False)
    def ensure_exist(self, user_ids: List[int]) -> List[int]:
        """Check that every id is a registered user. Reveals existence only, never profiles;
        used to validate meeting invitations."""
        found = {row.id for row in self.db.query(models.User.id).filter(models.User.id.in_(user_ids))}
        missing = sorted(set(user_ids) - found)
        if missing:
            raise NotFoundError(f"Users not found: {missing}")
        return sorted(found)
