import pytest

from tests.conftest import invite_and_accept


@pytest.fixture
def shared(client, alice, bob):
    """A meeting owned by Alice and attended by Bob, with one note and one task by Alice."""
    meeting = client.post("/meetings/", json={"title": "1:1", "scheduled_time": "2024-01-15T10:00:00"},
                          headers=alice.headers).json()
    invite_and_accept(client, alice, meeting["id"], bob)
    note = client.post("/notes/", json={"content": "secret", "meeting_id": meeting["id"]}, headers=alice.headers).json()
    task = client.post("/tasks/", json={"title": "t", "due_meeting_id": meeting["id"]}, headers=alice.headers).json()
    return {"meeting": meeting["id"], "note": note["id"], "task": task["id"]}


def test_outsider_cannot_see_anything(client, shared, alice, carol):
    for path in (f"/meetings/{shared['meeting']}", f"/notes/{shared['note']}", f"/tasks/{shared['task']}",
                 f"/users/{alice.id}"):
        assert client.get(path, headers=carol.headers).status_code == 404, path

    assert client.get("/meetings/", headers=carol.headers).json() == []
    assert client.get("/notes/", headers=carol.headers).json() == []
    assert client.get("/tasks/", headers=carol.headers).json() == []
    assert [u["id"] for u in client.get("/users/", headers=carol.headers).json()] == [carol.id]
    assert client.get("/notes/", params={"meeting_id": shared["meeting"]}, headers=carol.headers).status_code == 404


def test_outsider_cannot_write_to_meeting(client, shared, carol):
    meeting_id = shared["meeting"]

    assert client.post("/notes/", json={"content": "x", "meeting_id": meeting_id},
                       headers=carol.headers).status_code == 404
    assert client.post("/tasks/", json={"title": "x", "due_meeting_id": meeting_id},
                       headers=carol.headers).status_code == 404
    assert client.put(f"/meetings/{meeting_id}", json={"title": "x"}, headers=carol.headers).status_code == 404
    assert client.delete(f"/tasks/{shared['task']}", headers=carol.headers).status_code == 404


def test_attendee_can_read_and_contribute(client, shared, alice, bob):
    meeting = client.get(f"/meetings/{shared['meeting']}", headers=bob.headers)

    assert meeting.status_code == 200
    assert client.post("/notes/", json={"content": "mine", "meeting_id": shared["meeting"]},
                       headers=bob.headers).status_code == 201
    assert {u["id"] for u in client.get("/users/", headers=bob.headers).json()} == {alice.id, bob.id}


def test_only_owner_can_modify_meeting(client, shared, bob):
    assert client.put(f"/meetings/{shared['meeting']}", json={"title": "x"}, headers=bob.headers).status_code == 403
    assert client.delete(f"/meetings/{shared['meeting']}", headers=bob.headers).status_code == 403


def test_task_modification_rights(client, shared, alice, bob):
    bobs_task = client.post("/tasks/", json={"title": "b", "due_meeting_id": shared["meeting"]},
                            headers=bob.headers).json()

    # Bob can't touch Alice's task; Alice (meeting owner) can touch Bob's; Bob can touch his own.
    assert client.put(f"/tasks/{shared['task']}", json={"status": "completed"}, headers=bob.headers).status_code == 403
    assert client.put(f"/tasks/{bobs_task['id']}", json={"status": "cancelled"},
                      headers=alice.headers).status_code == 200
    assert client.delete(f"/tasks/{bobs_task['id']}", headers=bob.headers).status_code == 204


def test_task_cannot_be_moved_to_a_meeting_you_do_not_attend(client, shared, alice, carol):
    carols = client.post("/meetings/", json={"title": "c", "scheduled_time": "2024-01-15T10:00:00"},
                         headers=carol.headers).json()

    response = client.put(f"/tasks/{shared['task']}", json={"due_meeting_id": carols["id"]}, headers=alice.headers)

    assert response.status_code == 404


def test_removed_attendee_loses_access_immediately(client, shared, alice, bob):
    # Warm Bob's cache first, then revoke his access.
    assert client.get(f"/meetings/{shared['meeting']}", headers=bob.headers).status_code == 200
    assert client.get(f"/notes/{shared['note']}", headers=bob.headers).status_code == 200

    assert client.delete(f"/meetings/{shared['meeting']}/attendees/{bob.id}", headers=alice.headers).status_code == 204

    assert client.get(f"/meetings/{shared['meeting']}", headers=bob.headers).status_code == 404
    assert client.get(f"/notes/{shared['note']}", headers=bob.headers).status_code == 404
    assert [u["id"] for u in client.get("/users/", headers=bob.headers).json()] == [bob.id]


def test_tools_respect_privacy(client, shared, carol):
    response = client.post("/tools/meetings_get", json={"meeting_id": shared["meeting"]}, headers=carol.headers)

    assert response.status_code == 404
