import enum
from datetime import datetime

from sqlalchemy import JSON, Column, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


class TaskStatus(str, enum.Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class User(Base):
    __tablename__ = 'users'

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    email = Column(String, unique=True, nullable=False, index=True)  # stored lower-cased
    hashed_password = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Relationships
    meeting_links = relationship('MeetingAttendee', back_populates='user', cascade='all, delete-orphan')
    meetings = relationship('Meeting', secondary='meeting_users', viewonly=True)


class MeetingAttendee(Base):
    """Attendance of a user at a meeting. The source of truth for meeting visibility.

    A mapped class (rather than a bare association table) so the meetings service can
    manage attendance by id without loading ``User`` rows, and so per-attendee data
    (RSVP, role) can be added later.
    """
    __tablename__ = 'meeting_users'

    meeting_id = Column(Integer, ForeignKey('meetings.id', ondelete='CASCADE'), primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), primary_key=True, index=True)

    # Relationships
    meeting = relationship('Meeting', back_populates='attendee_links')
    user = relationship('User', back_populates='meeting_links')


class Meeting(Base):
    __tablename__ = 'meetings'

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)
    description = Column(Text)
    scheduled_time = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    owner_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)

    # Relationships
    owner = relationship('User')
    attendee_links = relationship('MeetingAttendee', back_populates='meeting', cascade='all, delete-orphan')
    attendees = relationship('User', secondary='meeting_users', viewonly=True, order_by='User.id')
    notes = relationship('Note', back_populates='meeting', cascade='all, delete-orphan')
    tasks = relationship('Task', back_populates='due_meeting', cascade='all, delete-orphan')


class Note(Base):
    __tablename__ = 'notes'

    id = Column(Integer, primary_key=True, index=True)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    meeting_id = Column(Integer, ForeignKey('meetings.id', ondelete='CASCADE'), nullable=False, index=True)
    author_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)

    # Relationships
    meeting = relationship('Meeting', back_populates='notes')
    author = relationship('User')


class Task(Base):
    __tablename__ = 'tasks'

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)
    description = Column(Text)
    status = Column(
        Enum(TaskStatus, native_enum=False, length=20, values_callable=lambda e: [m.value for m in e]),
        default=TaskStatus.PENDING,
        nullable=False,
    )
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    due_meeting_id = Column(Integer, ForeignKey('meetings.id', ondelete='CASCADE'), nullable=False, index=True)
    created_by_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)

    # Relationships
    due_meeting = relationship('Meeting', back_populates='tasks')
    created_by = relationship('User')


# OAuth 2.1 (authorization for the MCP endpoint)

class OAuthClient(Base):
    """A client registered through dynamic client registration (RFC 7591)."""
    __tablename__ = 'oauth_clients'

    id = Column(String, primary_key=True)  # client_id
    client_name = Column(String)
    redirect_uris = Column(JSON, nullable=False)
    grant_types = Column(JSON, nullable=False)
    token_endpoint_auth_method = Column(String, nullable=False)
    hashed_secret = Column(String)  # None for public clients
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class OAuthAuthorizationCode(Base):
    """Single-use code from the login page, bound to a PKCE challenge."""
    __tablename__ = 'oauth_authorization_codes'

    code_hash = Column(String, primary_key=True)
    client_id = Column(String, ForeignKey('oauth_clients.id', ondelete='CASCADE'), nullable=False)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    redirect_uri = Column(String, nullable=False)
    code_challenge = Column(String, nullable=False)
    scope = Column(String, nullable=False)
    resource = Column(String, nullable=False)
    expires_at = Column(DateTime, nullable=False)


class OAuthGrant(Base):
    """One authorization of a client by a user. Every refresh token rotated from the same
    login belongs to it; revoking it invalidates them and their access tokens (``sid``)."""
    __tablename__ = 'oauth_grants'

    id = Column(String, primary_key=True)
    client_id = Column(String, ForeignKey('oauth_clients.id', ondelete='CASCADE'), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    scope = Column(String, nullable=False)
    resource = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    revoked_at = Column(DateTime)


class OAuthRefreshToken(Base):
    __tablename__ = 'oauth_refresh_tokens'

    token_hash = Column(String, primary_key=True)
    grant_id = Column(String, ForeignKey('oauth_grants.id', ondelete='CASCADE'), nullable=False, index=True)
    expires_at = Column(DateTime, nullable=False)
    used_at = Column(DateTime)  # set on rotation; presenting it again means it leaked
