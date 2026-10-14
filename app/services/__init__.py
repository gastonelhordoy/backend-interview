"""Importing this package registers every service with the mesh."""
from app.services.meetings import MeetingsService
from app.services.notes import NotesService
from app.services.tasks import TasksService
from app.services.users import UsersService

__all__ = ["MeetingsService", "NotesService", "TasksService", "UsersService"]
