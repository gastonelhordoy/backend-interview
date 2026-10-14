from typing import Optional

from sqlalchemy.orm import Session

import app.services  # noqa: F401  (registers every service with the mesh)
from app.config import settings
from app.mesh.cache import Cache, InMemoryCache
from app.mesh.mesh import Principal, ServiceMesh
from app.mesh.transports import CachingTransport, InProcessTransport, LoopbackTransport, Transport

# Shared by all per-user meshes in this process; entries are keyed by user.
cache = InMemoryCache()


def build_transport(db: Session, mode: str, cache: Cache) -> Transport:
    local = InProcessTransport(db)
    if mode == "local":
        inner = local
    elif mode == "loopback":
        inner = LoopbackTransport(local)
    else:
        raise ValueError(f"Unknown MESH_MODE {mode!r}")
    return CachingTransport(inner, cache, settings.cache_ttl_seconds)


def build_mesh(principal: Principal, db: Session, mode: Optional[str] = None) -> ServiceMesh:
    return ServiceMesh(principal, build_transport(db, mode or settings.mesh_mode, cache))
