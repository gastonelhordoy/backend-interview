from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api import auth, invitations, meetings, mesh, notes, tasks, tools, users
from app.db import create_tables
from app.errors import AuthenticationError, ServiceError
from app.mcp import server as mcp_server
from app.oauth import router as oauth


@asynccontextmanager
async def lifespan(_app: FastAPI):
    create_tables()
    yield


app = FastAPI(
    title="Meeting Notes API",
    description="API for managing meetings, notes, and tasks",
    version="1.0.0",
    lifespan=lifespan,
)

for module in (auth, users, meetings, invitations, notes, tasks, tools):
    app.include_router(module.router)
app.include_router(mcp_server.router)
app.include_router(oauth.router)
app.include_router(mesh.router)  # internal: server side of HttpTransport


@app.exception_handler(ServiceError)
async def handle_service_error(_request: Request, exc: ServiceError):
    headers = {"WWW-Authenticate": "Bearer"} if isinstance(exc, AuthenticationError) else None
    return JSONResponse({"detail": exc.message}, status_code=exc.status_code, headers=headers)


@app.get("/")
async def root():
    """
    Root endpoint to verify the API is running.
    """
    return {
        "message": "Welcome to the Meeting Notes API",
        "version": "1.0.0",
        "endpoints": {
            "auth": "/auth/token",
            "users": "/users/",
            "meetings": "/meetings/",
            "invitations": "/invitations/",
            "notes": "/notes/",
            "tasks": "/tasks/",
            "tools": "/tools/",
            "mcp": "/mcp",
        }
    }
