# Service mesh

The mesh is how services talk to each other. Every service receives a `ServiceMesh` (the interface) and reaches any other service only through it: `self.mesh.meetings.access(meeting_id=1)`. Services never import, instantiate or hold references to each other.

The design separates two concerns that change for different reasons:

| | Responsibility | Changes when… |
|---|---|---|
| **`ServiceMesh`** (`app/mesh/mesh.py`) | *What* is called and *on whose behalf*: service directory, the user (`Principal`), argument validation, `get_tools()` / `execute_tool()` | never per deployment |
| **`Transport`** (`app/mesh/transports.py`) | *How* the call travels: in memory, cached, over HTTP, (gRPC…) | the deployment changes |

Transports share one method, `send(call, mesh)`, and wrap each other. A deployment is a composition of transports, chosen in `app/mesh/factory.py` from configuration. Switching from a monolith to microservices changes configuration only: neither the services nor the mesh change.

## Components

```mermaid
flowchart TB
    subgraph entry["Entry points"]
        REST["REST routers<br/>app/api/*"]
        TOOLS["LLM tools<br/>mesh.get_tools() / execute_tool()"]
        MESHAPI["POST /mesh/{service}/{op}<br/>(calls from other nodes)"]
    end

    subgraph mesh["ServiceMesh (one per request, bound to one user)"]
        M["mesh.&lt;service&gt;.&lt;op&gt;(**kwargs)<br/>registry lookup → validate args → ServiceCall"]
    end

    subgraph chain["Transport chain (composable)"]
        C["CachingTransport<br/>per-user query cache, tag invalidation"]
        R["RoutingTransport<br/>picks a transport per service"]
        L["InProcessTransport<br/>holds the local service instances"]
        H["HttpTransport<br/>POST /mesh/... on another node"]
    end

    subgraph services["Services (app/services)"]
        U[UsersService]
        MT[MeetingsService]
        N[NotesService]
        T[TasksService]
    end

    REST --> M
    TOOLS --> M
    M --> C --> R
    R -- "default" --> L
    R -- "services configured as remote" --> H
    L --> U & MT & N & T
    H -. "HTTP" .-> REMOTE[("another mesh node")]
    services -. "self.mesh.other.op()" .-> M
    MESHAPI --> L
```

The dotted arrow from the services back to the mesh is the key rule: a service that needs another service goes back through the mesh, so the call gets the same validation, caching, user scoping and routing as any other call.

## Classes

```mermaid
classDiagram
    direction LR
    class ServiceMesh {
        +principal: Principal
        +transport: Transport
        +call(service, op, **kwargs)
        +get_tools() list
        +execute_tool(name, args)
    }
    class ServiceCall {
        +spec: OperationSpec
        +args: BaseModel
        +principal: Principal
    }
    class Transport {
        <<abstract>>
        +send(call, mesh)*
    }
    class InProcessTransport {
        +db: Session
        -services: dict
    }
    class CachingTransport {
        +inner: Transport
        +cache: Cache
    }
    class RoutingTransport {
        +default: Transport
        +routes: dict[str, Transport]
    }
    class HttpTransport {
        +client: httpx.Client
    }
    class BaseService {
        <<abstract>>
        +db
        +principal
        +mesh: ServiceMesh
        #_visible()*
    }
    ServiceMesh --> Transport : sends ServiceCall
    Transport <|-- InProcessTransport
    Transport <|-- CachingTransport
    Transport <|-- RoutingTransport
    Transport <|-- HttpTransport
    CachingTransport o-- Transport : wraps
    RoutingTransport o-- Transport : wraps
    InProcessTransport o-- BaseService : holds instances
    BaseService --> ServiceMesh : calls other services
```

* **`InProcessTransport`** is the default implementation. It creates each service on first use, bound to the DB session, the user and the mesh, and keeps the reference for later calls in the same request.
* **`CachingTransport`** and **`RoutingTransport`** are decorators: they add behaviour and delegate to the transport they wrap.
* **`HttpTransport`** is the remote implementation: it serializes the call and sends it to the node that runs that service.

## Declaring operations

```python
@service("tasks")                          # registers the class with the mesh
class TasksService(BaseService):
    @query(depends_on=("meetings",))       # read: cached, tagged "tasks" + "meetings"
    def list(self, meeting_id: Optional[int] = None) -> List[schemas.Task]:
        """List tasks due at the meetings you attend."""   # → LLM tool description
        ...

    @command                               # write: invalidates the "tasks" tag
    def create(self, data: schemas.TaskCreate) -> schemas.Task: ...
```

From each signature the registry builds an input model (validation and the LLM tool `input_schema`) and a result adapter (cache and wire serialization). `@query(tool=False)` keeps internal operations, such as `meetings.access`, routable between services but hidden from `get_tools()`.

## A call, end to end (everything in memory)

`GET /meetings/1` as Alice, default configuration:

```mermaid
sequenceDiagram
    autonumber
    participant API as GET /meetings/1
    participant Mesh as ServiceMesh(alice)
    participant Cache as CachingTransport
    participant Local as InProcessTransport
    participant Meet as MeetingsService
    participant Notes as NotesService

    API->>Mesh: mesh.meetings.get(meeting_id=1)
    Mesh->>Mesh: validate args → ServiceCall
    Mesh->>Cache: send(call)
    Cache->>Cache: miss on user:alice:meetings.get:{"meeting_id":1}
    Cache->>Local: send(call)
    Local->>Meet: get(meeting_id=1)  (instance created once, then reused)
    Meet->>Meet: _visible(): only meetings Alice attends
    Meet->>Mesh: mesh.notes.list(meeting_id=1)
    Mesh->>Cache: send(call)
    Cache->>Local: send(call)
    Local->>Notes: list(meeting_id=1)
    Notes->>Mesh: mesh.meetings.access(meeting_id=1)
    Mesh-->>Notes: allowed (served by the same MeetingsService instance)
    Notes-->>Meet: [Note, ...]
    Meet-->>Cache: MeetingWithDetails
    Cache->>Cache: store JSON, tags: meetings, users, notes, tasks
    Cache-->>API: MeetingWithDetails
```

## Changing the transport

### 1. By configuration: run some services elsewhere

`MESH_REMOTE_SERVICES` lists the services this process reaches over HTTP, as `service=url` pairs. Services not listed run in memory.

| `MESH_REMOTE_SERVICES` | Composition built by `factory.build_transport` |
|---|---|
| *(empty, default)* | `Caching → InProcess` |
| `notes=http://notes-svc:8000,tasks=http://notes-svc:8000` | `Caching → Routing(default=InProcess, notes=Http(notes-svc), tasks=Http(notes-svc))` |

Every node runs the same image and exposes `POST /mesh/{service}/{operation}`, so any node can serve any service: the configuration decides who serves what. Related settings: `MESH_REMOTE_TIMEOUT_SECONDS` (10). All nodes must share `SECRET_KEY`.

### 2. In code: compose transports by hand

```python
from app.mesh.transports import CachingTransport, HttpTransport, InProcessTransport, RoutingTransport

transport = CachingTransport(
    RoutingTransport(
        default=InProcessTransport(db),
        routes={"notes": HttpTransport(httpx.Client(base_url="http://notes-svc:8000"))},
    ),
    cache=RedisCache.from_url("redis://redis:6379/0"),   # any Cache implementation
    ttl_seconds=300,
)
mesh = ServiceMesh(principal, transport)
```

Tests use this to insert a `RecordingTransport` that records which calls cross the mesh (`tests/test_mesh.py`).

### 3. A new communication mode (e.g. gRPC)

Implement one class and route services to it:

```python
class GrpcTransport(Transport):
    def __init__(self, channel):
        self.stub = MeshStub(channel)

    def send(self, call: ServiceCall, mesh: ServiceMesh) -> Any:
        reply = self.stub.Call(MeshRequest(
            service=call.spec.service, operation=call.spec.name,
            args_json=call.args.model_dump_json(exclude_unset=True),
            token=create_access_token(call.principal.user_id, audience=MESH_AUDIENCE, ttl_minutes=1)))
        if reply.error_code:
            raise ERRORS_BY_CODE[reply.error_code](reply.error_message)
        return call.spec.result_adapter.validate_json(reply.result_json)
```

The server side calls `factory.serve_call(principal, db, service, operation, args)`, the same function the HTTP endpoint uses. The rules every remote transport follows:

* **Arguments** are sent with `exclude_unset=True`, so partial updates stay partial.
* **Results** are serialized and parsed with the operation's `result_adapter`. That is why services return Pydantic schemas, never ORM objects.
* **Errors** travel as `{code, message}` and are re-raised as the same `ServiceError` subclass (`ERRORS_BY_CODE`). A 404 on the remote node is a `NotFoundError` on the caller.
* **Identity** travels as a credential, never as a bare user id (next section).

## Remote calls: the microservices example

`docker-compose.microservices.yml` splits the app into two nodes of the same image:

```mermaid
flowchart LR
    client(["Client / MCP agent"])

    subgraph net["compose network"]
        subgraph api["api :8000 (public)"]
            apiMesh["mesh<br/>local: users, meetings<br/>remote: notes, tasks"]
        end
        subgraph svc["notes-svc (internal)"]
            svcMesh["mesh<br/>local: notes, tasks<br/>remote: users, meetings"]
        end
        db[("SQLite volume<br/>(shared in this demo)")]
        redis[("Redis<br/>shared query cache")]
    end

    client -- "REST / MCP" --> apiMesh
    apiMesh -- "POST /mesh/notes/list<br/>POST /mesh/tasks/list" --> svcMesh
    svcMesh -- "POST /mesh/meetings/access" --> apiMesh
    apiMesh --- db
    svcMesh --- db
    apiMesh -.- redis
    svcMesh -.- redis
```

```bash
SECRET_KEY=$(openssl rand -hex 32) \
  docker compose -f docker-compose.yml -f docker-compose.microservices.yml up --build
```

`GET /meetings/1` now crosses the network twice, and the services involved are the same code as in the monolith:

```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant A as api: MeetingsService
    participant AH as api: HttpTransport
    participant N as notes-svc: /mesh endpoint → NotesService
    participant NH as notes-svc: HttpTransport
    participant AM as api: /mesh endpoint → MeetingsService

    C->>A: GET /meetings/1 (Bearer user token)
    A->>AH: mesh.notes.list(meeting_id=1)
    AH->>N: POST /mesh/notes/list {"meeting_id":1}<br/>Bearer mesh token (sub=alice, aud=mesh, 1 min)
    N->>NH: mesh.meetings.access(meeting_id=1)
    NH->>AM: POST /mesh/meetings/access<br/>Bearer mesh token (sub=alice)
    AM-->>NH: 200 {"result": ...}  or  404 {"error":{"code":"not_found"}}
    NH-->>N: result / NotFoundError
    N-->>AH: 200 {"result": [notes]}
    AH-->>A: List[Note]
    A-->>C: meeting with notes and tasks
```

### Identity between nodes

* `HttpTransport` signs a JWT for the caller with `aud=meeting-notes-mesh` and a one-minute lifetime. The receiving node runs the operation as that same user, so the privacy rules apply on every node.
* `/mesh` accepts only that audience. REST and MCP tokens are rejected (401), and no user-facing endpoint issues mesh tokens.
* Internal operations (`meetings.access`, `accessible_ids`…) are reachable through `/mesh`, because other nodes need them. In production, keep `/mesh` on an internal network or port, or add mTLS between nodes.

### Simplifications of the example

* **Shared database.** Both nodes use the same SQLite volume. Real microservices would each own their tables (the services already only touch their own tables, so the split is possible).
* **Cache.** The cache sits on the caller's side of the chain, so both nodes cache. They share one Redis (`CACHE_URL`), which lets a write on either node invalidate entries cached by both. See [Distributed cache](#distributed-cache-redis).
* **Failures.** A refused connection, timeout or malformed answer becomes `ServiceUnavailableError` (HTTP 503). There are no retries or circuit breakers.

## Caching

```mermaid
flowchart LR
    q["@query call"] --> key["key = user:&lt;id&gt;:&lt;service&gt;.&lt;op&gt;:&lt;args json&gt;"]
    key --> hit{"in cache?"}
    hit -- yes --> ret["parse JSON with result_adapter → fresh object"]
    hit -- no --> run["inner.send(call)"] --> store["store JSON<br/>tags = service + depends_on"]
    c["@command call"] --> exec["inner.send(call)"] --> inv["invalidate tags = service + invalidates<br/>(for all users)"]
```

* Entries are per user, so one user's cached result is never served to another.
* A query is tagged with its service plus `depends_on`. `notes.list` depends on `meetings`, because attendance decides which notes are visible.
* Invalidation is global across users: Alice editing a meeting changes what Bob may see, and removing an attendee revokes their cached reads at once.
* Known limitation: a read that races a concurrent write can cache a stale value until the TTL expires (`CACHE_TTL_SECONDS`, default 300). Versioned tags would close this gap.
* The backend is the `Cache` interface: `InMemoryCache` (default, one process) or `RedisCache` (shared), chosen by `CACHE_URL`.

### Distributed cache (Redis)

`InMemoryCache` lives inside one process. As soon as there is more than one process (mesh nodes, or several uvicorn workers), each has its own copy. A command on one node then can't invalidate what another node cached, and that node keeps serving stale results until the TTL expires. Stale results here are a privacy problem: a user removed from a meeting would keep reading its notes. `RedisCache` (`app/mesh/cache.py`) implements the same `Cache` interface on Redis, shared by every node.

Enable it with one setting; nothing else changes:

```bash
CACHE_URL=redis://redis:6379/0      # empty (default): InMemoryCache
```

```mermaid
flowchart LR
    subgraph a["api node"]
        ca["CachingTransport"]
    end
    subgraph n["notes-svc node"]
        cn["CachingTransport"]
    end
    subgraph r["Redis"]
        e1["mesh-cache:entry:user:2:meetings.accessible_ids:{}<br/>STRING, TTL"]
        t1["mesh-cache:tag:meetings<br/>SET of entry keys, TTL"]
    end
    cn -- "query: GET / SET EX + SADD tag" --> r
    ca -- "command meetings.update:<br/>MULTI SMEMBERS tag + DEL tag EXEC,<br/>then DEL entries" --> r
```

**Data layout.** Each entry is a string with the entry's TTL (`SET … EX`). Each tag is a set holding the keys of its entries. Every key starts with `mesh-cache:`, so the cache can share a Redis with other data.

**Invalidation.** `invalidate_tags` reads and deletes each tag set inside one `MULTI/EXEC` transaction, then deletes the entries it found. An entry cached concurrently lands in a new, empty set, so the invalidation doesn't lose it.

**Expiry.** Tag sets receive the entry TTL on every add. Every entry shares the same TTL (`CACHE_TTL_SECONDS`), so a set outlives its entries and expires on its own if nobody invalidates it. Keys of already-expired entries can stay in a set until then; deleting them is harmless.

**Failure policy.** If Redis is down:
* A failed read is logged and treated as a miss, so the operation still runs.
* A failed write is logged, and the result isn't cached.
* A failed invalidation **raises**. Silently keeping stale entries could serve data the user may no longer see.

**Redis configuration.** Use `maxmemory-policy noeviction`, as in the compose example. An `*-lru` policy could evict a tag set and lose invalidations. With `noeviction`, a full Redis only stops new entries from being cached; deletes still work.

The example walked through above was checked against the compose stack:
1. Ben lists notes. `notes-svc` caches his `meetings.accessible_ids` in Redis.
2. Ann removes Ben from the meeting on `api`.
3. Ben's next list is empty, and the meeting returns 404 for him.

With per-process caches, step 3 would still show the notes. `tests/test_cache.py` covers both implementations against the same contract (Redis through `fakeredis`), sharing between nodes, the outage policy, and that cross-node scenario.

## Testing the transports

* Every API test runs twice (`tests/conftest.py`): once in memory (`local`) and once with **every** service behind `HttpTransport` (`http`). In `http` mode the "remote node" is the same app reached through real HTTP requests in-process, so each cross-service call goes through serialization, `/mesh` and the mesh token. If something works in `local` but fails in `http`, it isn't wire-safe.
* `tests/test_cache.py` runs the same contract against `InMemoryCache` and `RedisCache`.
* `tests/test_mesh.py` covers instance reuse, routing, results and errors across HTTP, partial updates, unreachable nodes and token audiences.
