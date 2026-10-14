"""Internal endpoint: the server side of ``HttpTransport``. Other mesh nodes (not users)
call it, with a short-lived token whose audience is ``meeting-notes-mesh``; REST and
MCP tokens are rejected. Hidden from OpenAPI and MCP."""
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, Header
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session

from app.auth.security import MESH_AUDIENCE, decode_access_token
from app.db import get_db
from app.errors import AuthenticationError, ServiceError
from app.mesh.factory import serve_call
from app.mesh.mesh import Principal

router = APIRouter(prefix="/mesh", include_in_schema=False)


@router.post("/{service}/{operation}")
def mesh_call(service: str, operation: str, arguments: Dict[str, Any] = Body(default={}),
              authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    try:
        principal = Principal(decode_access_token(_bearer(authorization), audience=MESH_AUDIENCE)["sub"])
        result = serve_call(principal, db, service, operation, arguments)
    except ServiceError as exc:
        return JSONResponse({"error": {"code": exc.code, "message": exc.message}}, status_code=exc.status_code)
    return Response(b'{"result":' + result + b"}", media_type="application/json")


def _bearer(authorization: Optional[str]) -> str:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise AuthenticationError("Missing mesh token")
    return token
