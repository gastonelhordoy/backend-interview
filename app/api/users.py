from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app import schemas
from app.api.deps import get_mesh
from app.auth.service import AuthService
from app.db import get_db
from app.mcp.registry import mcp_tool
from app.mesh.mesh import ServiceMesh

router = APIRouter(prefix="/users", tags=["users"])


@router.post("/", response_model=schemas.User, status_code=201)
@mcp_tool
def create_user(data: schemas.UserCreate, db: Session = Depends(get_db)):
    """
    Register a new user (public). Log in afterwards with `POST /auth/token`.
    """
    return AuthService(db).register(data)


@router.get("/", response_model=List[schemas.User])
@mcp_tool
def list_users(mesh: ServiceMesh = Depends(get_mesh)):
    """
    List yourself and the users you share a meeting with.
    """
    return mesh.users.list()


@router.get("/me", response_model=schemas.User)
@mcp_tool
def get_me(mesh: ServiceMesh = Depends(get_mesh)):
    """
    Get the authenticated user.
    """
    return mesh.users.me()


@router.get("/{user_id}", response_model=schemas.User)
@mcp_tool
def get_user(user_id: int, mesh: ServiceMesh = Depends(get_mesh)):
    """
    Get a specific user by ID (only yourself or users you share a meeting with).
    """
    return mesh.users.get(user_id=user_id)
