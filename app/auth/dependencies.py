from fastapi import Depends
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app import models
from app.auth.security import decode_access_token
from app.db import get_db
from app.errors import AuthenticationError
from app.mesh.mesh import Principal

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token")


def get_current_principal(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> Principal:
    user_id = decode_access_token(token)["sub"]
    if db.get(models.User, user_id) is None:
        raise AuthenticationError("Invalid or expired token")
    return Principal(user_id=user_id)
