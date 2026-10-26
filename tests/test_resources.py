"""Write every resource through the API (and so through the mesh and services), then read it back."""
from tests.conftest import invite_and_accept


def create_meeting(client, user, **overrides):
    payload = {"title": "Sprint Planning", "scheduled_time": "2024-01-15T10:00:00", **overrides}
    response = client.post("/meetings/", json=payload, headers=user.headers)
    assert response.status_code == 201, response.text
    return response.json()


def test_meeting_round_trip(client, alice, bob):
    created = create_meeting(client, alice, description="Q1")
    invite_and_accept(client, alice, created["id"], bob)

    fetched = client.get(f"/meetings/{created['id']}", headers=alice.headers).json()

    assert fetched["title"] == "Sprint Planning"
    assert fetched["description"] == "Q1"
    assert fetched["owner_id"] == alice.id
    assert [a["id"] for a in fetched["attendees"]] == [alice.id, bob.id]
    assert fetched["notes"] == [] and fetched["tasks"] == []
    assert [m["id"] for m in client.get("/meetings/", headers=alice.headers).json()] == [created["id"]]


def test_meeting_update(client, alice):
    meeting = create_meeting(client, alice)

    response = client.put(f"/meetings/{meeting['id']}", json={"title": "Renamed"}, headers=alice.headers)

    assert response.status_code == 200
    fetched = client.get(f"/meetings/{meeting['id']}", headers=alice.headers).json()
    assert fetched["title"] == "Renamed"
    assert fetched["scheduled_time"] == "2024-01-15T10:00:00"
    assert [a["id"] for a in fetched["attendees"]] == [alice.id]


def test_meeting_update_rejects_null_for_required_field(client, alice):
    meeting = create_meeting(client, alice)

    response = client.put(f"/meetings/{meeting['id']}", json={"title": None}, headers=alice.headers)

    assert response.status_code == 422


def test_note_round_trip(client, alice):
    meeting = create_meeting(client, alice)

    created = client.post("/notes/", json={"content": "Decided X", "meeting_id": meeting["id"]}, headers=alice.headers)

    assert created.status_code == 201
    note = client.get(f"/notes/{created.json()['id']}", headers=alice.headers).json()
    assert note["content"] == "Decided X"
    assert note["author_id"] == alice.id
    assert client.get("/notes/", params={"meeting_id": meeting["id"]}, headers=alice.headers).json() == [note]
    assert client.get(f"/meetings/{meeting['id']}", headers=alice.headers).json()["notes"] == [note]


def test_task_round_trip_and_filters(client, alice):
    first = create_meeting(client, alice)
    second = create_meeting(client, alice, title="Retro")
    created = client.post("/tasks/", json={"title": "Ship it", "due_meeting_id": first["id"]}, headers=alice.headers)
    assert created.status_code == 201
    task_id = created.json()["id"]

    updated = client.put(f"/tasks/{task_id}", json={"status": "completed", "due_meeting_id": second["id"]},
                         headers=alice.headers)

    assert updated.status_code == 200
    task = client.get(f"/tasks/{task_id}", headers=alice.headers).json()
    assert (task["status"], task["due_meeting_id"], task["created_by_id"]) == ("completed", second["id"], alice.id)
    assert client.get("/tasks/", params={"status": "completed"}, headers=alice.headers).json() == [task]
    assert client.get("/tasks/", params={"status": "pending"}, headers=alice.headers).json() == []
    assert client.get("/tasks/", params={"meeting_id": first["id"]}, headers=alice.headers).json() == []


def test_task_rejects_invalid_status(client, alice):
    meeting = create_meeting(client, alice)

    response = client.post("/tasks/", json={"title": "t", "due_meeting_id": meeting["id"], "status": "done"},
                           headers=alice.headers)

    assert response.status_code == 422


def test_task_delete(client, alice):
    meeting = create_meeting(client, alice)
    task = client.post("/tasks/", json={"title": "t", "due_meeting_id": meeting["id"]}, headers=alice.headers).json()

    assert client.delete(f"/tasks/{task['id']}", headers=alice.headers).status_code == 204
    assert client.get(f"/tasks/{task['id']}", headers=alice.headers).status_code == 404


def test_deleting_meeting_removes_its_notes_and_tasks(client, alice):
    meeting = create_meeting(client, alice)
    note = client.post("/notes/", json={"content": "n", "meeting_id": meeting["id"]}, headers=alice.headers).json()
    task = client.post("/tasks/", json={"title": "t", "due_meeting_id": meeting["id"]}, headers=alice.headers).json()

    assert client.delete(f"/meetings/{meeting['id']}", headers=alice.headers).status_code == 204

    assert client.get(f"/meetings/{meeting['id']}", headers=alice.headers).status_code == 404
    assert client.get(f"/notes/{note['id']}", headers=alice.headers).status_code == 404
    assert client.get(f"/tasks/{task['id']}", headers=alice.headers).status_code == 404
