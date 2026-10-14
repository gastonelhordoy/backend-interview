from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar, Dict

from sqlalchemy.orm import Query, Session

from app.errors import NotFoundError
from app.mesh.mesh import Principal, ServiceMesh


class BaseService(ABC):
    """Base class for every mesh service (one per resource).

    A service instance is short-lived and bound to one user: it only knows its DB
    session, the ``principal`` it acts for, and the ``mesh`` it must use to reach any
    other service. Privacy is enforced in one place per service: ``_visible()``.
    """

    service_name: ClassVar[str]  # set by @service
    model: ClassVar[type]  # ORM model of the resource this service owns

    def __init__(self, *, db: Session, principal: Principal, mesh: ServiceMesh):
        self.db = db
        self.principal = principal
        self.mesh = mesh

    @property
    def user_id(self) -> int:
        return self.principal.user_id

    @abstractmethod
    def _visible(self) -> Query:
        """Query over ``model`` restricted to the rows the principal may read."""

    def _get_visible(self, obj_id: int):
        obj = self._visible().filter(self.model.id == obj_id).first()
        if obj is None:
            # Same error whether it doesn't exist or isn't visible: no existence leaks.
            raise NotFoundError(f"{self.model.__name__} {obj_id} not found")
        return obj

    @staticmethod
    def _apply(obj, changes: Dict[str, Any]) -> None:
        for name, value in changes.items():
            setattr(obj, name, value)

    def _save(self, obj):
        self.db.add(obj)
        self.db.commit()
        self.db.refresh(obj)
        return obj

    def _delete(self, obj) -> None:
        self.db.delete(obj)
        self.db.commit()
