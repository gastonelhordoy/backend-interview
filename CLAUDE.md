# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt   # Python 3.10
.venv/bin/uvicorn main:app --reload        # http://localhost:8000, Swagger at /docs
.venv/bin/pytest                           # full suite
.venv/bin/pytest tests/test_privacy.py::test_outsider_cannot_see_anything   # single test (runs in both mesh modes)
.venv/bin/pytest -k "http"                 # only the variants with every service behind HttpTransport
docker compose up --build                  # API :8000 + sqlite-web UI :8080, DB on the sqlite-data volume
SECRET_KEY=x docker compose -f docker-compose.yml -f docker-compose.microservices.yml up --build   # two mesh nodes
```

No linter/formatter is configured. Tests use an in-memory SQLite DB, set via env vars in `tests/conftest.py` *before* `app` is imported. The API `client` fixture is parametrized over mesh modes (`local`, `http`), so every API test runs twice. In `http` mode every service is routed through `HttpTransport` to the same app, in-process (`remote_mesh` fixture).

## Architecture

This is an interview exercise. The README's "Your task" section lists the requirements. Design docs: `docs/architecture.md` (overview), `docs/mesh.md` (mesh and transports), `docs/mcp.md` (MCP and OAuth); they use Mermaid diagrams. Keep them in sync with design changes.

Request path: `app/api/*` router → `Depends(get_mesh)` (per-request, per-user `ServiceMesh`) → `mesh.<service>.<op>(**kwargs)` → transport chain (`Caching → Routing → InProcess | Http`) → `BaseService` subclass in `app/services/`.

Key rules when changing code:

- **Services never import or instantiate each other.** Cross-resource logic goes through `self.mesh.<service>.<op>(...)`. The meetings service owns attendance (`MeetingAttendee` / `meeting_users`) and answers access questions for the others through internal ops `access`, `accessible_ids` and `co_attendee_ids`.
- **Privacy lives in each service's `_visible()` query.** All reads go through it (`_get_visible`). Invisible resources raise `NotFoundError` (404), never 403. `PermissionDeniedError` (403) is only for visible-but-not-modifiable.
- **Operations are declared with decorators** from `app/mesh/registry.py`: `@service("name")` on the class, and `@query(depends_on=...)` or `@command(invalidates=...)` on methods. The registry builds a Pydantic input model from the signature and a `TypeAdapter` from the return annotation, so **operations need full type hints, keyword-friendly params and a docstring** (the docstring becomes the LLM tool description). `tool=False` hides internal ops from `get_tools()`/`execute_tool()`. New service modules must be imported in `app/services/__init__.py` to register.
- **Services return Pydantic schemas, not ORM objects.** Results must survive JSON round-trips: the cache and `HttpTransport` serialize them. If something works in `local` mode but fails in `http`, it isn't wire-safe.
- **Cache invalidation is tag-based and global across users.** A query is tagged with its service plus `depends_on`; a command clears its service tag plus `invalidates`. When a query's result depends on another service's data, add that service to `depends_on`. Otherwise users will see stale or revoked data.
- Transports (`app/mesh/transports.py`) compose by wrapping each other; `app/mesh/factory.py` picks the composition from `MESH_REMOTE_SERVICES` (services listed there go over HTTP, the rest run in-process). `InProcessTransport` keeps one instance per service per mesh. The server side of `HttpTransport` is `POST /mesh/{service}/{operation}` (`app/api/mesh.py` → `factory.serve_call`), which only accepts mesh tokens (`aud=meeting-notes-mesh`). A new communication mode is a new `Transport`; services and mesh don't change.
- Domain errors carry `code` (wire) and `status_code` (HTTP); remote errors are re-raised by code.
- Registration and login (`app/auth/service.py`) are intentionally outside the mesh, because there is no user yet. Everything else authenticated goes through the mesh.
- Endpoint functions marked `@mcp_tool` (`app/mcp/registry.py`) are published as MCP tools by the server at `POST /mcp` (`app/mcp/server.py`). Tool schemas are derived from the FastAPI route. `/mcp` only accepts OAuth access tokens from `app/oauth/` (audience = MCP resource URL). `tools/call` replays the call as an in-process HTTP request with a one-minute REST token for that user. New endpoints therefore need a docstring (it becomes the tool description) and need `@mcp_tool` if agents should reach them. This is separate from the mesh's `get_tools()`, which lists service operations.
- `app/oauth/` is an OAuth 2.1 authorization server (discovery, dynamic registration, login page, PKCE, rotating refresh tokens with reuse detection). It exists only for MCP clients; REST clients keep using `POST /auth/token`. Tests drive the real flow through `oauth_login()` / `ApiUser.mcp_headers` in `tests/conftest.py`.
- Domain errors live in `app/errors.py`.

Schema changes: there are no migrations. Tables are created at startup, so delete `meetings.db` (or `docker compose down -v`) after changing models.
