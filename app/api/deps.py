from fastapi import Depends
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_principal
from app.db import get_db
from app.mesh.factory import build_mesh
from app.mesh.mesh import Principal, ServiceMesh


def get_mesh(principal: Principal = Depends(get_current_principal), db: Session = Depends(get_db)) -> ServiceMesh:
    """The authenticated user's service mesh, built per request."""
    return build_mesh(principal, db)
