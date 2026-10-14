from __future__ import annotations

from datetime import datetime
from typing import ClassVar, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

from app.models import TaskStatus


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class PartialUpdate(BaseModel):
    """Base for PUT payloads: omitted fields are left unchanged, explicit nulls are only
    accepted for nullable fields."""

    non_nullable_fields: ClassVar[Tuple[str, ...]] = ()

    @model_validator(mode="after")
    def _reject_nulls(self):
        for name in self.non_nullable_fields:
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        return self


# User schemas
class UserBase(BaseModel):
    name: str = Field(min_length=1)
    email: EmailStr


class UserCreate(UserBase):
    password: str = Field(min_length=8, max_length=128)


class User(UserBase, ORMModel):
    id: int


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


# Meeting schemas
class MeetingBase(BaseModel):
    title: str = Field(min_length=1)
    description: Optional[str] = None
    scheduled_time: datetime


class MeetingCreate(MeetingBase):
    attendee_ids: List[int] = Field(default=[], description="Users to invite; the creator is always an attendee.")


class MeetingUpdate(PartialUpdate):
    non_nullable_fields: ClassVar[Tuple[str, ...]] = ("title", "scheduled_time", "attendee_ids")

    title: Optional[str] = Field(default=None, min_length=1)
    description: Optional[str] = None
    scheduled_time: Optional[datetime] = None
    attendee_ids: Optional[List[int]] = Field(default=None, description="Replaces the attendee list; the owner is always kept.")


class Meeting(MeetingBase, ORMModel):
    id: int
    owner_id: int
    created_at: datetime
    attendees: List[User] = []


class MeetingAccess(BaseModel):
    """What the current user may do with a meeting (internal, used by other services)."""

    meeting_id: int
    owner_id: int
    is_owner: bool


# Note schemas
class NoteBase(BaseModel):
    content: str = Field(min_length=1)


class NoteCreate(NoteBase):
    meeting_id: int


class Note(NoteBase, ORMModel):
    id: int
    meeting_id: int
    author_id: int
    created_at: datetime


# Task schemas
class TaskBase(BaseModel):
    title: str = Field(min_length=1)
    description: Optional[str] = None
    status: TaskStatus = TaskStatus.PENDING


class TaskCreate(TaskBase):
    due_meeting_id: int


class TaskUpdate(PartialUpdate):
    non_nullable_fields: ClassVar[Tuple[str, ...]] = ("title", "status", "due_meeting_id")

    title: Optional[str] = Field(default=None, min_length=1)
    description: Optional[str] = None
    status: Optional[TaskStatus] = None
    due_meeting_id: Optional[int] = None


class Task(TaskBase, ORMModel):
    id: int
    due_meeting_id: int
    created_by_id: int
    created_at: datetime


# MeetingWithDetails must be defined after Note and Task
class MeetingWithDetails(Meeting):
    notes: List[Note] = []
    tasks: List[Task] = []
