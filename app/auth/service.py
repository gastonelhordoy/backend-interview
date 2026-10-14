from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import models, schemas
from app.auth.security import create_access_token, hash_password, verify_password
from app.errors import AuthenticationError, ConflictError

# Verified against when the email is unknown, so response time doesn't reveal accounts.
_DUMMY_HASH = hash_password("dummy-password")


class AuthService:
    """Registration and login. Not a mesh service: it runs before a user is known."""

    def __init__(self, db: Session):
        self.db = db

    def register(self, data: schemas.UserCreate) -> schemas.User:
        email = data.email.lower()
        if self.db.query(models.User.id).filter(models.User.email == email).first():
            raise ConflictError("A user with this email already exists")
        user = models.User(name=data.name, email=email, hashed_password=hash_password(data.password))
        self.db.add(user)
        try:
            self.db.commit()
        except IntegrityError:  # concurrent registration with the same email
            self.db.rollback()
            raise ConflictError("A user with this email already exists") from None
        self.db.refresh(user)
        return schemas.User.model_validate(user)

    def authenticate(self, email: str, password: str) -> models.User:
        user = self.db.query(models.User).filter(models.User.email == email.lower()).first()
        if not verify_password(password, user.hashed_password if user else _DUMMY_HASH) or user is None:
            raise AuthenticationError("Incorrect email or password")
        return user

    def login(self, email: str, password: str) -> schemas.Token:
        return schemas.Token(access_token=create_access_token(self.authenticate(email, password).id))
