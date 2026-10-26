from __future__ import annotations

from datetime import datetime
from typing import ClassVar, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

from app.models import InvitationStatus, TaskStatus


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
    """Your own account."""

    id: int
    share_name: bool


class UserUpdate(PartialUpdate):
    non_nullable_fields: ClassVar[Tuple[str, ...]] = ("share_name",)

    share_name: Optional[bool] = Field(
        default=None, description="Show your name to co-attendees and to people who invite you. Your email is always shown.")


class UserProfile(BaseModel):
    """Another user as you see them: the email always, the name only if they chose to share it."""

    id: int
    email: EmailStr
    name: Optional[str] = None


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


# Meeting schemas
class MeetingBase(BaseModel):
    title: str = Field(min_length=1)
    description: Optional[str] = None
    scheduled_time: datetime


class MeetingCreate(MeetingBase):
    invitee_emails: List[EmailStr] = Field(
        default=[], description="Emails to invite. Invitees attend once they accept; the creator always attends.")


class MeetingUpdate(PartialUpdate):
    non_nullable_fields: ClassVar[Tuple[str, ...]] = ("title", "scheduled_time")

    title: Optional[str] = Field(default=None, min_length=1)
    description: Optional[str] = None
    scheduled_time: Optional[datetime] = None


class Meeting(MeetingBase, ORMModel):
    id: int
    owner_id: int
    created_at: datetime
    attendees: List[UserProfile] = []


# Invitation schemas
class InvitationCreate(BaseModel):
    emails: List[EmailStr] = Field(min_length=1, description="Emails to invite to the meeting.")


class Invitation(ORMModel):
    """An invitation as the meeting owner sees it."""

    id: int
    meeting_id: int
    email: str
    status: InvitationStatus
    created_at: datetime
    responded_at: Optional[datetime] = None


class ReceivedInvitation(BaseModel):
    """An invitation as the invitee sees it: enough to decide, nothing more (no description,
    attendees, notes or tasks until accepted)."""

    id: int
    meeting_id: int
    title: str
    scheduled_time: datetime
    invited_by: UserProfile
    status: InvitationStatus
    created_at: datetime


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
