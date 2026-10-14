# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt   # Python 3.10
.venv/bin/uvicorn main:app --reload        # http://localhost:8000, Swagger at /docs
.venv/bin/pytest                           # full suite
.venv/bin/pytest tests/test_privacy.py::test_outsider_cannot_see_anything   # single test (runs in both mesh modes)
.venv/bin/pytest -k "loopback"             # only the loopback-transport variants
docker compose up --build                  # API :8000 + sqlite-web UI :8080, DB on the sqlite-data volume
```

No linter/formatter is configured. Tests use an in-memory SQLite DB, set via env vars in `tests/conftest.py` *before* `app` is imported. The API `client` fixture is parametrized over `MESH_MODE` (`local`, `loopback`), so every API test runs twice.

## Architecture

This is an interview exercise. The README's "Your task" section lists the requirements; `docs/architecture.md` is the full design document. Keep it in sync with design changes.

Request path: `app/api/*` router → `Depends(get_mesh)` (per-request, per-user `ServiceMesh`) → `mesh.<service>.<op>(**kwargs)` → transport pipeline → `BaseService` subclass in `app/services/`.

Key rules when changing code:

- **Services never import or instantiate each other.** Cross-resource logic goes through `self.mesh.<service>.<op>(...)`. The meetings service owns attendance (`MeetingAttendee` / `meeting_users`) and answers access questions for the others through internal ops `access`, `accessible_ids` and `co_attendee_ids`.
- **Privacy lives in each service's `_visible()` query.** All reads go through it (`_get_visible`). Invisible resources raise `NotFoundError` (404), never 403. `PermissionDeniedError` (403) is only for visible-but-not-modifiable.
- **Operations are declared with decorators** from `app/mesh/registry.py`: `@service("name")` on the class, and `@query(depends_on=...)` or `@command(invalidates=...)` on methods. The registry builds a Pydantic input model from the signature and a `TypeAdapter` from the return annotation, so **operations need full type hints, keyword-friendly params and a docstring** (the docstring becomes the LLM tool description). `tool=False` hides internal ops from `get_tools()`/`execute_tool()`. New service modules must be imported in `app/services/__init__.py` to register.
- **Services return Pydantic schemas, not ORM objects.** Results must survive JSON round-trips: the cache and `LoopbackTransport` serialize them. If something works in `local` mode but fails in `loopback`, it isn't wire-safe.
- **Cache invalidation is tag-based and global across users.** A query is tagged with its service plus `depends_on`; a command clears its service tag plus `invalidates`. When a query's result depends on another service's data, add that service to `depends_on`. Otherwise users will see stale or revoked data.
- Transports (`app/mesh/transports.py`) compose by wrapping each other; `app/mesh/factory.py` picks the composition from `MESH_MODE`.
- Registration and login (`app/auth/service.py`) are intentionally outside the mesh, because there is no user yet. Everything else authenticated goes through the mesh.
- Endpoint functions marked `@mcp_tool` (`app/mcp/registry.py`) are published as MCP tools by the server at `POST /mcp` (`app/mcp/server.py`). Tool schemas are derived from the FastAPI route. `/mcp` only accepts OAuth access tokens from `app/oauth/` (audience = MCP resource URL). `tools/call` replays the call as an in-process HTTP request with a one-minute REST token for that user. New endpoints therefore need a docstring (it becomes the tool description) and need `@mcp_tool` if agents should reach them. This is separate from the mesh's `get_tools()`, which lists service operations.
- `app/oauth/` is an OAuth 2.1 authorization server (discovery, dynamic registration, login page, PKCE, rotating refresh tokens with reuse detection). It exists only for MCP clients; REST clients keep using `POST /auth/token`. Tests drive the real flow through `oauth_login()` / `ApiUser.mcp_headers` in `tests/conftest.py`.
- Domain errors live in `app/errors.py`. The HTTP status mapping is in `app/main.py`; the wire mapping is by `code`.

Schema changes: there are no migrations. Tables are created at startup, so delete `meetings.db` (or `docker compose down -v`) after changing models.
