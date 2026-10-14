# Architecture

Detailed documents:

* [Service mesh](mesh.md): the mesh interface, transports, how to switch them, the microservices example, caching (with diagrams).
* [MCP support](mcp.md): `@mcp_tool`, the MCP server, OAuth 2.1 (with diagrams).

## Layers

```mermaid
flowchart TB
    http["HTTP: app/api/*<br/>thin routers: parse request, call the mesh, return schema"]
    mcp["MCP: POST /mcp<br/>replays tool calls as REST requests"]
    mesh["ServiceMesh: app/mesh<br/>one per request, bound to the user · validate args → ServiceCall"]
    chain["Transport chain<br/>Caching → Routing → InProcess | Http"]
    svc["Services: app/services<br/>one per resource, BaseService subclasses, own their tables"]
    db[("SQLAlchemy models on SQLite")]
    mcp --> http
    http -- "Depends(get_mesh)" --> mesh --> chain --> svc --> db
    svc -. "self.mesh.other.op()" .-> mesh
```

Request flow for `GET /meetings/1` as Alice:

1. `get_current_principal` validates the JWT → `Principal(user_id=alice)`.
2. `get_mesh` builds `ServiceMesh(alice, CachingTransport(InProcessTransport(db)))` (or with a `RoutingTransport` to remote nodes, see [mesh.md](mesh.md#changing-the-transport)).
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

## Mesh transports and caching

See [mesh.md](mesh.md). In short: the `ServiceMesh` is the interface services receive; *how* a call travels is a chain of `Transport`s (`CachingTransport`, `RoutingTransport`, `InProcessTransport`, `HttpTransport`) composed in `app/mesh/factory.py` from `MESH_REMOTE_SERVICES`. The query cache is `InMemoryCache` or, with `CACHE_URL=redis://...`, a `RedisCache` shared by every node. `InProcessTransport` is the default and holds the local service instances; `HttpTransport` reaches services running on another node through `POST /mesh/{service}/{operation}`. `docker-compose.microservices.yml` runs the app as two nodes.

## MCP server and OAuth

See [mcp.md](mcp.md). Endpoints marked `@mcp_tool` are published at `POST /mcp` (Streamable HTTP, stateless); tool calls are replayed as in-process REST requests for the signed-in user. `/mcp` only accepts access tokens from the OAuth 2.1 server in `app/oauth/` (discovery, dynamic registration, PKCE, rotating refresh tokens with reuse detection).

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
