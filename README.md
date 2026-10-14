# backend-inteview

A FastAPI application for managing meetings, notes, and tasks.

## Your task

Implement all endpoints through a service layer and write it to the database, then assert that you can read it again.

To do this:

- Add authentication. Do this however you would like (external libraries are allowed). Keep it as simple as possible though.
- Create a service per resource that all implement a base service class.
- Put all of the services into a service mesh. Call's between each service should route through the mesh.
- The service mesh is user specific, and should should support caching.
- The mesh should support local development, and cloud development, and should be composable in order to accomplish this. Ie, local development is in memory, but in production the service mesh might allow gRPC connections between services seamlessly in production. Note you do not need to support gRPC for this task - but design this in such a way that the communication mode between services is easily interchangeable.
- Create a decorator that can be placed on each service. This decorator allows us to call service_mesh.get_tools(), which can be passed onwards to an LLM.
- The service mesh (and the services within in) should only be able to access resources that only the user making the request.

## Features

- **Users**: Create and manage users who attend meetings
- **Meetings**: Schedule meetings with attendees
- **Notes**: Add notes to meetings
- **Tasks**: Create tasks due at specific meetings

## Implementation

See [docs/architecture.md](docs/architecture.md) for the design: services, service mesh, transports, caching, LLM tools, privacy rules, authentication and model changes.

## Setup

### Docker (API + SQLite)

```bash
docker compose up --build
```

* API: `http://localhost:8000`
* SQLite browser (read-only, [sqlite-web](https://github.com/coleifer/sqlite-web)): `http://localhost:8080`

SQLite is an embedded database, so it has no server process. The database file lives on the `sqlite-data` volume. The API container writes it, and the `db` container serves a UI over the same file. Data persists across restarts; `docker compose down -v` wipes it. Set `SECRET_KEY` (in your shell or in `.env`) so tokens keep working across restarts.

### Local

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/uvicorn main:app --reload
.venv/bin/pytest            # tests run in both mesh modes (local, loopback)
```

Configuration (environment variables): `DATABASE_URL` (default `sqlite:///./meetings.db`), `SECRET_KEY`, `ACCESS_TOKEN_TTL_MINUTES` (60), `MESH_MODE` (`local` | `loopback`), `CACHE_TTL_SECONDS` (300), `PASSWORD_HASH_ITERATIONS` (600000), `MCP_ACCESS_TOKEN_TTL_MINUTES` (30), `MCP_REFRESH_TOKEN_TTL_DAYS` (30), `PUBLIC_BASE_URL` (derived from the request).

## Getting started: create your first user

The database starts empty and there are **no default users**: create one with the public registration endpoint, then log in. The same email and password are used for the REST API, Swagger and the MCP login page.

1. Register (the password needs at least 8 characters; emails are case-insensitive):

   ```bash
   curl -X POST http://localhost:8000/users/ -H "Content-Type: application/json" \
     -d '{"name": "John Doe", "email": "john@example.com", "password": "a-long-password"}'
   ```

   It returns `201` with the new user, without the password. If the email is already registered you get `409`.

2. Log in and keep the token (valid for `ACCESS_TOKEN_TTL_MINUTES`, 60 by default):

   ```bash
   TOKEN=$(curl -s -X POST http://localhost:8000/auth/token \
     -d 'username=john@example.com&password=a-long-password' \
     | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
   ```

   The login form field is called `username`, but it takes the email.

3. Check it works:

   ```bash
   curl http://localhost:8000/users/me -H "Authorization: Bearer $TOKEN"
   ```

**From Swagger UI** (`http://localhost:8000/docs`), do the same without the terminal. Run `POST /users/` with *Try it out*, then click **Authorize** and enter the email as `username` plus the password. Leave `client_id` and `client_secret` empty.

**To try the privacy rules**, register a second user. That user won't see the first user's meetings until they are added as an attendee (`attendee_ids`).

## MCP server (use the API from a chat)

Endpoint functions marked with `@mcp_tool` (`app/mcp/registry.py`) are published as MCP tools at `http://localhost:8000/mcp`. The transport is Streamable HTTP. Name, description, HTTP method, path and parameter schema come from the FastAPI route, so the tools always match the API. Each tool call runs as the user who signed in for that connection.

Connect from Claude Code. No token needed:

```bash
claude mcp add --transport http meetings-api http://localhost:8000/mcp
```

Then run `/mcp` in Claude Code and choose **Authenticate**. Your browser opens the API's login page. Sign in with a user created as described in [Getting started](#getting-started-create-your-first-user). Once you sign in, the client receives its tokens and renews them on its own. Any MCP client that supports OAuth works the same way.

How it works (OAuth 2.1, as the MCP authorization spec defines):

* `/mcp` answers `401` with a `WWW-Authenticate` header pointing at `/.well-known/oauth-protected-resource/mcp`. The client discovers the authorization server, registers itself (`/oauth/register`) and opens `/oauth/authorize` (the login page) with PKCE.
* `/oauth/token` returns:
  * an **access token** valid **only for `/mcp`** (`MCP_ACCESS_TOKEN_TTL_MINUTES`, default 30). REST tokens are rejected at `/mcp` and MCP tokens are rejected by the REST API.
  * a **refresh token** (`MCP_REFRESH_TOKEN_TTL_DAYS`, default 30). It rotates on every use and is stored hashed. Reusing an old one revokes the whole session.
* The session is stored in the database, so it survives restarts. If `SECRET_KEY` isn't set, the client simply refreshes after a restart.
* To sign the integration out, call `POST /oauth/revoke` (RFC 7009) or remove the server from the client.
* Behind a proxy or on another host, set `PUBLIC_BASE_URL` (e.g. `https://api.example.com`). It is the OAuth issuer and the token audience.

To publish another endpoint, add the decorator under the route decorator:

```python
@router.get("/{note_id}", response_model=schemas.Note)
@mcp_tool                      # or @mcp_tool(name=..., title=..., description=...)
def get_note(note_id: int, mesh: ServiceMesh = Depends(get_mesh)):
    """Get a specific note by ID."""   # becomes the tool description
```

## API Documentation

Once the server is running, you can access:
- Interactive API docs (Swagger UI): `http://localhost:8000/docs`
- Alternative API docs (ReDoc): `http://localhost:8000/redoc`
- OpenAPI schema: `http://localhost:8000/openapi.json`

## Authentication

Every endpoint except registration (`POST /users/`) and login (`POST /auth/token`) requires `Authorization: Bearer <token>`. See [Getting started](#getting-started-create-your-first-user) for how to get one. MCP clients use their own OAuth login instead (see [MCP server](#mcp-server-use-the-api-from-a-chat)).

## API Endpoints

### Auth
- `POST /auth/token` - Log in (form fields `username` = email, `password`), returns a bearer token

### Users
- `POST /users/` - Register a new user (public)
- `GET /users/me` - Get the authenticated user
- `GET /users/` - List yourself and users you share a meeting with
- `GET /users/{user_id}` - Get a specific (visible) user

### Meetings
- `POST /meetings/` - Create a new meeting with attendees
- `GET /meetings/` - List the meetings you attend
- `GET /meetings/{meeting_id}` - Get a specific meeting with details
- `PUT /meetings/{meeting_id}` - Update a meeting (owner only)
- `DELETE /meetings/{meeting_id}` - Delete a meeting with its notes and tasks (owner only)

### Notes
- `POST /notes/` - Create a new note for a meeting
- `GET /notes/` - List notes of your meetings (can filter by meeting_id)
- `GET /notes/{note_id}` - Get a specific note

### Tasks
- `POST /tasks/` - Create a new task due at a meeting
- `GET /tasks/` - List tasks of your meetings (can filter by meeting_id or status: `pending`, `completed`, `cancelled`)
- `GET /tasks/{task_id}` - Get a specific task
- `PUT /tasks/{task_id}` - Update a task (change status, reassign to different meeting)
- `DELETE /tasks/{task_id}` - Delete a task (task creator or meeting owner)

### LLM tools
- `GET /tools/` - Tool definitions for every operation exposed by the service mesh (`service_mesh.get_tools()`)
- `POST /tools/{tool_name}` - Execute a tool call as the authenticated user

### MCP
- `POST /mcp` - MCP server (JSON-RPC: `initialize`, `ping`, `tools/list`, `tools/call`); requires an MCP OAuth token

### OAuth (for MCP clients)
- `GET /.well-known/oauth-protected-resource/mcp`, `GET /.well-known/oauth-authorization-server` - Discovery metadata
- `POST /oauth/register` - Dynamic client registration
- `GET/POST /oauth/authorize` - Login page (authorization code + PKCE)
- `POST /oauth/token` - `authorization_code` and `refresh_token` grants
- `POST /oauth/revoke` - Revoke a refresh token and its session

## Example Usage

Assumes `$TOKEN` from [Getting started](#getting-started-create-your-first-user).

### Create a meeting
```bash
curl -X POST http://localhost:8000/meetings/ -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Sprint Planning",
    "description": "Q1 Sprint Planning",
    "scheduled_time": "2024-01-15T10:00:00",
    "attendee_ids": [1, 2]
  }'
```

### Create a task for a meeting
```bash
curl -X POST http://localhost:8000/tasks/ -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Implement feature X",
    "description": "Complete implementation",
    "due_meeting_id": 1
  }'
```

### Add a note to a meeting
```bash
curl -X POST http://localhost:8000/notes/ -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "content": "This is a meeting note",
    "meeting_id": 1
  }'
```

## Project Structure

- `app/main.py` - FastAPI app, routers, error → HTTP mapping (`main.py` at the root re-exports it)
- `app/api/` - Thin HTTP routers; every authenticated endpoint calls the user's service mesh
- `app/auth/` - Password hashing, JWT, registration/login, `get_current_principal`
- `app/mcp/` - `@mcp_tool` endpoint registry and the MCP server mounted at `/mcp`
- `app/oauth/` - OAuth 2.1 authorization server for MCP clients (discovery, registration, login page, tokens)
- `app/mesh/` - Service mesh: `@service`/`@query`/`@command` registry, `ServiceMesh`, transports, cache, factory
- `app/services/` - `BaseService` and one service per resource (users, meetings, notes, tasks)
- `app/models.py` / `app/schemas.py` / `app/db.py` - SQLAlchemy models, Pydantic schemas, engine and sessions
- `tests/` - API, privacy and mesh tests
- `Dockerfile`, `docker-compose.yml` - API + SQLite volume + sqlite-web
