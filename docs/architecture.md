# Architecture

## Layers

```
HTTP (app/api/*)          thin FastAPI routers: parse request, call the mesh, return schema
   │  Depends(get_mesh)   one mesh per request, bound to the authenticated user
   ▼
ServiceMesh (app/mesh)    validate args → ServiceCall → transport pipeline
   │
   ▼  Transport pipeline (composable)
CachingTransport ─► [LoopbackTransport] ─► [RoutingTransport] ─► InProcessTransport
   │
   ▼
Services (app/services)   one per resource, BaseService subclasses, own their tables
   │
   ▼
SQLAlchemy models (app/models.py) on SQLite
```

Request flow for `GET /meetings/1` as Alice:

1. `get_current_principal` validates the JWT → `Principal(user_id=alice)`.
2. `get_mesh` builds `ServiceMesh(alice, CachingTransport(InProcessTransport(db)))`.
3. The router calls `mesh.meetings.get(meeting_id=1)`. The mesh validates the arguments against the operation's input model and sends a `ServiceCall` down the transport pipeline.
4. `MeetingsService.get` loads the meeting through `_visible()`, so it only finds it if Alice attends. Then it calls `mesh.notes.list(...)` and `mesh.tasks.list(...)` through the *same* mesh. Those services ask `mesh.meetings.access(...)` before returning anything.

## Services

* `BaseService` holds the DB session, the `principal` and the `mesh`, plus shared helpers (`_get_visible`, `_apply`, `_save`, `_delete`).
* Every service implements `_visible()`: a query restricted to rows the principal may read. Every read goes through it. That gives each service a single place where privacy is enforced.
* Services never import each other. Anything outside their own tables goes through `self.mesh.<service>.<op>(...)`. The meetings service owns attendance (`meeting_users`), so it is the authority the other services ask about access, through internal operations (`access`, `accessible_ids`, `co_attendee_ids`).
* Services return Pydantic schemas, never ORM objects. That makes results safe to cache, safe to send over a wire, and unable to lazy-load data the user shouldn't see.
* Registration and login (`app/auth/service.py`) are deliberately **not** mesh services: they run before a user exists, and the mesh is per-user.

## Decorators and LLM tools

```python
@service("tasks")                         # registers the class with the mesh
class TasksService(BaseService):
    @query(depends_on=("meetings",))      # read: cacheable, tagged
    def list(self, meeting_id: Optional[int] = None, status: Optional[TaskStatus] = None) -> List[schemas.Task]:
        """List tasks due at the meetings you attend..."""   # becomes the tool description

    @command                              # write: invalidates cached reads
    def create(self, data: schemas.TaskCreate) -> schemas.Task: ...

    @query(tool=False)                    # routable between services, hidden from LLMs
    def ...
```

At registration, the method signature becomes a Pydantic input model and the return annotation becomes a `TypeAdapter`. The same metadata serves four purposes:

| Use | Built from |
|---|---|
| Argument validation on every call | input model |
| `mesh.get_tools()`: `{name, description, input_schema}` (Anthropic tool format) | input model JSON schema + docstring |
| `mesh.execute_tool(name, args)`: runs an LLM tool call as the user, returns JSON | input model + result adapter |
| Cache and wire (de)serialization | result adapter |

`GET /tools/` and `POST /tools/{name}` expose these over HTTP.

## Mesh transports

Every transport implements `send(call, mesh)`, and each one either delivers the call or wraps another transport:

| Transport | Role |
|---|---|
| `InProcessTransport` | Instantiates the service locally with the caller's DB session and mesh |
| `CachingTransport` | Caches query results per user; commands invalidate tags |
| `RoutingTransport` | Sends each service's calls to its own transport (e.g. some remote) |
| `LoopbackTransport` | Runs in-process, but serializes request, result and errors to JSON as a network would |
| `GrpcTransport` | Placeholder showing where a remote transport plugs in (not implemented) |

`MESH_MODE` selects the composition (`app/mesh/factory.py`): `local` = cache → in-process; `loopback` = cache → loopback → in-process. The whole API test suite runs in both modes. This already caught one real wire bug: partial updates lost their "unset" fields when serialized. A production deployment would be, for example:

```python
CachingTransport(RoutingTransport(default=InProcessTransport(db),
                                  routes={"users": GrpcTransport("users:50051")}), RedisCache(...), ttl)
```

A remote transport must forward the caller's credentials (the bearer token), not a bare user id. `LoopbackTransport` sends the id only because it never leaves the process.

### Caching

* The key is `user:<id>:<service>.<op>:<args json>`, so entries are never shared between users.
* Each query result is tagged with its own service plus `depends_on`. For example, `notes.list` depends on `meetings`, because attendance decides which notes are visible.
* A successful command invalidates its service's tag (plus `invalidates`) **for all users**. This is required because Alice editing a meeting changes what Bob may see.
* The cache backend is the `Cache` interface. `InMemoryCache` is per process; use a shared backend such as Redis when running several workers.
* Known limitation: a read that races a concurrent write can cache a stale value until the TTL expires (`CACHE_TTL_SECONDS`, default 300). Versioned tags would close this gap.

## MCP server

There are two tool layers, at different levels:

| | Mesh tools (`/tools`) | MCP tools (`/mcp`) |
|---|---|---|
| Declared with | `@query` / `@command` on service methods | `@mcp_tool` on endpoint functions |
| Granularity | Service operations (`meetings_get`) | HTTP endpoints (`get_meeting`) |
| Consumer | Code embedding an LLM in-process (`mesh.get_tools()` / `execute_tool`) | External agents and chats over the MCP protocol |

`@mcp_tool` only marks the function. `endpoint_tools(app)` walks `app.routes` once and, for each marked `APIRoute`, reads the path, method and the path/query/body parameters FastAPI already parsed (`get_flat_dependant`). From those it builds one Pydantic input model per tool: that model is the `inputSchema` and validates incoming arguments. Nothing in the decorator can drift from the real route.

`app/mcp/server.py` is a small, stateless implementation of the MCP Streamable HTTP transport. It handles `POST /mcp` with single JSON-RPC messages and JSON responses (no SSE, no sessions), and implements `initialize`, `ping`, `tools/list` and `tools/call`. The official `mcp` SDK wasn't used because it requires `pydantic>=2.12`, while the project pins 2.5.3. Behaviour was checked against the official SDK client.

`tools/call` doesn't call the endpoint function directly. It replays the call as an HTTP request against the same app, in-process through `httpx.ASGITransport`. The request is authenticated with a one-minute REST token minted for the MCP token's user, because the MCP token itself isn't valid for the REST API. Validation, the service mesh and the privacy rules therefore apply exactly as they do for any REST client. HTTP errors (403/404/422) come back as tool results with `isError: true`, so the model can see and correct them.

* The body is sent with `exclude_unset`, so partial updates stay partial (the same rule as `LoopbackTransport`).
* Requests carrying a non-local `Origin` header are rejected (DNS-rebinding protection, required by the MCP spec).
* Every request, `tools/list` included, needs an MCP access token (next section).

## MCP authorization (OAuth 2.1)

`app/oauth/` is a small authorization server that implements the [MCP authorization spec](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization). MCP clients get their own sign-in flow and their own long-lived session, without a long-lived bearer token ever existing.

```
client ──POST /mcp──────────────────────► 401  WWW-Authenticate: Bearer resource_metadata=".../oauth-protected-resource/mcp"
client ──GET  /.well-known/oauth-protected-resource/mcp ─► {resource: <base>/mcp, authorization_servers: [<base>]}
client ──GET  /.well-known/oauth-authorization-server ───► endpoints, S256, grant types
client ──POST /oauth/register ───────────► client_id (+ secret for confidential clients)
browser ─GET  /oauth/authorize?…&code_challenge=… ─► login page ─POST─► 302 redirect_uri?code=…&state=…&iss=…
client ──POST /oauth/token (code + code_verifier) ─► access token (aud=<base>/mcp, 30 min) + refresh token (30 days)
client ──POST /oauth/token (refresh_token) ────────► new access token + new refresh token (old one is now spent)
```

| Piece | Decision |
|---|---|
| Grants | `authorization_code` with **PKCE S256 required**, plus `refresh_token`. No implicit or password grant. |
| Clients | Dynamic registration (RFC 7591). Supports public (`none`) and confidential (`client_secret_post`/`basic`, secret stored hashed) clients. Redirect URIs must be https, http on loopback (any port, RFC 8252), or a private-use scheme. |
| Login page | Server-rendered HTML (`templates/login.html`), escaped output, `X-Frame-Options: DENY`. Client and redirect URI are validated before anything is shown or redirected (no open redirect); every POST re-validates the hidden fields. |
| Access token | JWT, `aud` = MCP resource URL, `sid` = grant id, TTL `MCP_ACCESS_TOKEN_TTL_MINUTES`. `/mcp` checks the audience and that the grant isn't revoked, so revocation takes effect immediately. |
| Refresh token | Opaque, SHA-256 hashed in `oauth_refresh_tokens`, TTL `MCP_REFRESH_TOKEN_TTL_DAYS`, rotated on every use, bound to its client. **Reuse of a spent token revokes the whole grant** (RFC 9700 §4.14.2). |
| Codes | Opaque, hashed, single-use (consumed even if the exchange fails), 5 minutes, bound to client, redirect URI and PKCE challenge. |
| Audience separation | REST tokens have `aud=meeting-notes-api`; MCP tokens have `aud=<base>/mcp`. Neither is accepted where the other belongs. |
| Revocation | `POST /oauth/revoke` (RFC 7009) revokes the grant: its refresh tokens and live access tokens. |

The issuer, resource and audience URLs come from `PUBLIC_BASE_URL`, or from the request's base URL when it isn't set. The flow was checked end to end against the Docker container with the official MCP SDK OAuth client (`OAuthClientProvider`), with the browser step simulated.

Not implemented: a consent screen separate from login, remembered browser sessions (you sign in each time you authorize a client), Client ID Metadata Documents, rate limiting of the login form, and cleanup of spent refresh tokens.

## Privacy rules

| Resource | Read | Create | Update / delete |
|---|---|---|---|
| User | yourself and users sharing a meeting with you | public registration | — |
| Meeting | attendees | any user (becomes owner + attendee) | owner only |
| Note | attendees of its meeting | attendees | — (no endpoints) |
| Task | attendees of its meeting | attendees | task creator or meeting owner; moving it requires attending the target meeting |

* Anything you can't read returns **404**, never 403, so ids can't be probed. 403 means "you can see it but can't change it".
* Removing an attendee revokes their access immediately, including any cached reads.
* Inviting a user by id reveals only whether that id exists (`users.ensure_exist`), never their profile.

## Authentication

* `POST /users/` registers. Emails are lower-cased; passwords are hashed with PBKDF2-SHA256 (stdlib, 600k iterations).
* `POST /auth/token` (OAuth2 password form, `username` = email) returns an HS256 JWT. Swagger's **Authorize** button works with it.
* Unknown emails still run a hash comparison, so response timing doesn't reveal which accounts exist.
* Set `SECRET_KEY` in any persistent deployment. Without it a random key is generated, and tokens stop working when the server restarts.

## Model changes

| Change | Why |
|---|---|
| `User.hashed_password`, `User.created_at` | Authentication |
| `oauth_clients`, `oauth_authorization_codes`, `oauth_grants`, `oauth_refresh_tokens` | MCP OAuth: registered clients, single-use codes, one grant per sign-in (the revocation unit), rotating refresh tokens. Only hashes of codes, secrets and tokens are stored |
| `meeting_users` mapped as `MeetingAttendee` (same table) | The meetings service manages attendance by id, without loading `User` rows from another service's domain; room for per-attendee data (RSVP, role) |
| `Meeting.owner_id` | Someone has to be allowed to edit or delete; attendees can't all have equal rights |
| `Note.author_id`, `Task.created_by_id` | Accountability, and the task permission rule |
| `TaskStatus` enum (`pending`/`completed`/`cancelled`) | `status` was a free-form string; it is now validated by the API and the DB column |
| Tasks cascade-delete with their meeting; `ON DELETE CASCADE` FKs; `PRAGMA foreign_keys=ON` | `Task.due_meeting_id` is non-nullable, so deleting a meeting with tasks used to fail. SQLite ignores FKs unless the pragma is enabled |
| Indexes on `meeting_id`, `due_meeting_id`, owner/author columns, `meeting_users.user_id` | Every visibility query filters on these columns |
| `PartialUpdate` schemas reject explicit `null` for required fields | `{"title": null}` used to reach the DB as a NOT NULL violation |

Possible next steps, not implemented:
* Alembic migrations. Tables are currently created at startup, so after a schema change an existing `meetings.db` has to be deleted.
* `Task.assignee_id`.
* Timezone-aware datetimes.
* Note editing.
