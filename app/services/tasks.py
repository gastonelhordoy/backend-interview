from __future__ import annotations

from typing import List, Optional

from app import models, schemas
from app.errors import PermissionDeniedError
from app.mesh.registry import command, query, service
from app.services.base import BaseService

Task = models.Task


@service("tasks")
class TasksService(BaseService):
    """Tasks are visible to the attendees of the meeting they are due at. Any attendee
    can create one; only its creator or the meeting owner can change or delete it."""

    model = Task

    def _visible(self):
        return self.db.query(Task).filter(Task.due_meeting_id.in_(self.mesh.meetings.accessible_ids()))

    def _get_modifiable(self, task_id: int) -> Task:
        task = self._get_visible(task_id)
        access = self.mesh.meetings.access(meeting_id=task.due_meeting_id)
        if task.created_by_id != self.user_id and not access.is_owner:
            raise PermissionDeniedError("Only the task creator or the meeting owner can modify this task")
        return task

    @query(depends_on=("meetings",))
    def list(self, meeting_id: Optional[int] = None, status: Optional[models.TaskStatus] = None) -> List[schemas.Task]:
        """List tasks due at the meetings you attend, optionally filtered by meeting and/or status."""
        tasks = self._visible()
        if meeting_id is not None:
            self.mesh.meetings.access(meeting_id=meeting_id)
            tasks = tasks.filter(Task.due_meeting_id == meeting_id)
        if status is not None:
            tasks = tasks.filter(Task.status == status)
        return [schemas.Task.model_validate(t) for t in tasks.order_by(Task.created_at, Task.id)]

    @query(depends_on=("meetings",))
    def get(self, task_id: int) -> schemas.Task:
        """Get a task due at a meeting you attend."""
        return schemas.Task.model_validate(self._get_visible(task_id))

    @command
    def create(self, data: schemas.TaskCreate) -> schemas.Task:
        """Create a task due at a meeting you attend."""
        self.mesh.meetings.access(meeting_id=data.due_meeting_id)
        return schemas.Task.model_validate(self._save(Task(**data.model_dump(), created_by_id=self.user_id)))

    @command
    def update(self, task_id: int, data: schemas.TaskUpdate) -> schemas.Task:
        """Update a task (e.g. change its status or move it to another meeting you attend)."""
        task = self._get_modifiable(task_id)
        changes = data.model_dump(exclude_unset=True)
        if changes.get("due_meeting_id", task.due_meeting_id) != task.due_meeting_id:
            self.mesh.meetings.access(meeting_id=changes["due_meeting_id"])
        self._apply(task, changes)
        return schemas.Task.model_validate(self._save(task))

    @command
    def delete(self, task_id: int) -> None:
        """Delete a task you created, or any task of a meeting you own."""
        self._delete(self._get_modifiable(task_id))
