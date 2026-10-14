import httpx
import pytest
from fastapi.testclient import TestClient

from app import schemas
from app.auth.security import create_access_token
from app.auth.service import AuthService
from app.config import _service_urls
from app.errors import NotFoundError, PermissionDeniedError, ServiceUnavailableError
from app.main import app
from app.mesh.cache import InMemoryCache
from app.mesh.factory import build_mesh
from app.mesh.mesh import Principal, ServiceMesh
from app.mesh.transports import CachingTransport, HttpTransport, InProcessTransport, RoutingTransport, Transport


class RecordingTransport(Transport):
    def __init__(self, inner):
        self.inner = inner
        self.calls = []

    def send(self, call, mesh):
        self.calls.append(f"{call.spec.service}.{call.spec.name}")
        return self.inner.send(call, mesh)


def register(db, name):
    user = AuthService(db).register(schemas.UserCreate(name=name, email=f"{name}@example.com", password="password1"))
    return Principal(user.id)


@pytest.fixture
def alice(db):
    return register(db, "alice")


@pytest.fixture
def bob(db):
    return register(db, "bob")


def new_meeting(**overrides):
    return schemas.MeetingCreate(title="Standup", scheduled_time="2024-01-15T10:00:00", **overrides)


def test_get_tools_lists_public_operations_only(db, alice):
    tools = {tool["name"]: tool for tool in build_mesh(alice, db).get_tools()}

    assert set(tools) == {
        "users_me", "users_list", "users_get",
        "meetings_list", "meetings_get", "meetings_create", "meetings_update", "meetings_delete",
        "notes_list", "notes_get", "notes_create",
        "tasks_list", "tasks_get", "tasks_create", "tasks_update", "tasks_delete",
    }
    assert set(tools["tasks_list"]["input_schema"]["properties"]) == {"meeting_id", "status"}
    assert tools["tasks_list"]["description"].startswith("List tasks")
    assert tools["meetings_get"]["input_schema"]["required"] == ["meeting_id"]


def test_execute_tool_returns_json(db, alice):
    mesh = build_mesh(alice, db)
    created = mesh.execute_tool("meetings_create",
                                {"data": {"title": "Standup", "scheduled_time": "2024-01-15T10:00:00"}})

    assert created["title"] == "Standup"
    assert created["scheduled_time"] == "2024-01-15T10:00:00"
    assert mesh.execute_tool("meetings_get", {"meeting_id": created["id"]})["id"] == created["id"]


def test_internal_operations_are_not_tools(db, alice):
    with pytest.raises(NotFoundError):
        build_mesh(alice, db).execute_tool("meetings_access", {"meeting_id": 1})


def test_queries_are_cached_per_user_and_invalidated_by_commands(db, alice, bob):
    recorder = RecordingTransport(InProcessTransport(db))
    cache = InMemoryCache()
    meshes = {p: ServiceMesh(p, CachingTransport(recorder, cache, ttl_seconds=60)) for p in (alice, bob)}

    meshes[alice].meetings.list()
    meshes[alice].meetings.list()
    assert recorder.calls.count("meetings.list") == 1

    meshes[bob].meetings.list()
    assert recorder.calls.count("meetings.list") == 2  # other user, other cache entry

    meshes[alice].meetings.create(data=new_meeting(attendee_ids=[bob.user_id]))
    assert [m.title for m in meshes[bob].meetings.list()] == ["Standup"]  # bob's entry was invalidated
    assert recorder.calls.count("meetings.list") == 3


def test_services_call_each_other_through_the_mesh(db, alice):
    recorder = RecordingTransport(InProcessTransport(db))
    mesh = ServiceMesh(alice, recorder)
    meeting = mesh.meetings.create(data=new_meeting())

    recorder.calls.clear()
    mesh.meetings.get(meeting_id=meeting.id)

    assert recorder.calls[0] == "meetings.get"
    assert {"notes.list", "tasks.list", "meetings.access", "meetings.accessible_ids"} <= set(recorder.calls)


def test_routing_transport_sends_services_to_their_transport(db, alice):
    users_route = RecordingTransport(InProcessTransport(db))
    mesh = ServiceMesh(alice, RoutingTransport(default=InProcessTransport(db), routes={"users": users_route}))

    mesh.meetings.list()
    mesh.users.me()

    assert users_route.calls == ["users.me"]


def test_in_process_transport_keeps_one_instance_per_service(db, alice):
    transport = InProcessTransport(db)
    mesh = ServiceMesh(alice, transport)
    mesh.meetings.create(data=new_meeting())

    mesh.meetings.list()
    mesh.notes.list()
    first = dict(transport._services)
    mesh.meetings.list()

    assert set(first) == {("meetings", alice.user_id), ("notes", alice.user_id)}
    assert transport._services == first  # same instances, not new ones per call
    assert first[("notes", alice.user_id)].mesh is mesh


def remote_node():
    """The app itself, reached over real (in-process) HTTP: a stand-in for a microservice."""
    return HttpTransport(TestClient(app, base_url="http://mesh-node.test"))


def test_http_transport_preserves_results_and_errors(db, alice, bob):
    alice_mesh = ServiceMesh(alice, RoutingTransport(InProcessTransport(db), routes={"meetings": remote_node()}))
    bob_mesh = ServiceMesh(bob, remote_node())
    meeting = alice_mesh.meetings.create(data=new_meeting(attendee_ids=[bob.user_id]))

    details = bob_mesh.meetings.get(meeting_id=meeting.id)

    assert isinstance(details, schemas.MeetingWithDetails)
    assert [user.id for user in details.attendees] == [alice.user_id, bob.user_id]
    with pytest.raises(PermissionDeniedError):
        bob_mesh.meetings.delete(meeting_id=meeting.id)
    with pytest.raises(NotFoundError):
        bob_mesh.meetings.get(meeting_id=999)


def test_http_transport_keeps_partial_updates_partial(db, alice):
    mesh = ServiceMesh(alice, remote_node())
    meeting = mesh.meetings.create(data=new_meeting(description="Daily"))

    updated = mesh.meetings.update(meeting_id=meeting.id, data=schemas.MeetingUpdate(title="Retro"))

    assert (updated.title, updated.description) == ("Retro", "Daily")


def test_http_transport_reports_unreachable_nodes(alice):
    def refuse(request):
        raise httpx.ConnectError("connection refused", request=request)

    client = httpx.Client(base_url="http://notes-svc:8000", transport=httpx.MockTransport(refuse))
    mesh = ServiceMesh(alice, HttpTransport(client))

    with pytest.raises(ServiceUnavailableError):
        mesh.notes.list()


@pytest.mark.parametrize("audience", ["meeting-notes-api", "http://testserver/mcp"])
def test_mesh_endpoint_only_accepts_mesh_tokens(alice, audience):
    token = create_access_token(alice.user_id, audience=audience)

    response = TestClient(app).post("/mesh/meetings/list", json={}, headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


def test_mesh_endpoint_is_not_public(client):
    assert "/mesh/{service}/{operation}" not in client.get("/openapi.json").json()["paths"]


def test_remote_services_setting_is_parsed():
    assert _service_urls(" notes=http://notes-svc:8000/, tasks=http://notes-svc:8000 ") == {
        "notes": "http://notes-svc:8000", "tasks": "http://notes-svc:8000"}
    assert _service_urls("") == {}
    with pytest.raises(ValueError):
        _service_urls("notes")


def test_cache_expires_entries():
    now = [0.0]
    cache = InMemoryCache(clock=lambda: now[0])
    cache.set("k", "v", tags=["t"], ttl_seconds=10)

    assert cache.get("k") == "v"
    now[0] = 11
    assert cache.get("k") is None
