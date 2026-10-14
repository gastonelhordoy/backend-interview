from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api import auth, meetings, notes, tasks, tools, users
from app.db import create_tables
from app.errors import (AuthenticationError, ConflictError, InvalidRequestError, NotFoundError,
                        PermissionDeniedError, ServiceError)
from app.mcp import server as mcp_server
from app.oauth import router as oauth

_HTTP_STATUS = {
    NotFoundError: 404,
    PermissionDeniedError: 403,
    ConflictError: 409,
    InvalidRequestError: 422,
    AuthenticationError: 401,
}


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

for module in (auth, users, meetings, notes, tasks, tools):
    app.include_router(module.router)
app.include_router(mcp_server.router)
app.include_router(oauth.router)


@app.exception_handler(ServiceError)
async def handle_service_error(_request: Request, exc: ServiceError):
    status = next((code for cls, code in _HTTP_STATUS.items() if isinstance(exc, cls)), 400)
    headers = {"WWW-Authenticate": "Bearer"} if isinstance(exc, AuthenticationError) else None
    return JSONResponse({"detail": exc.message}, status_code=status, headers=headers)


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
            "notes": "/notes/",
            "tasks": "/tasks/",
            "tools": "/tools/",
            "mcp": "/mcp",
        }
    }
