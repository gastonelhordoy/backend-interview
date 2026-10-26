"""Joining meetings by email invitation, and what each user shares with the others."""
import pytest

from tests.conftest import invite_and_accept


def create_meeting(client, user, **overrides):
    payload = {"title": "Planning", "scheduled_time": "2024-01-15T10:00:00", **overrides}
    response = client.post("/meetings/", json=payload, headers=user.headers)
    assert response.status_code == 201, response.text
    return response.json()


def received(client, user):
    return client.get("/invitations/", headers=user.headers).json()


def test_invitation_grants_nothing_until_accepted(client, alice, bob):
    meeting = create_meeting(client, alice, invitee_emails=[bob.email])

    assert [a["id"] for a in meeting["attendees"]] == [alice.id]
    assert client.get(f"/meetings/{meeting['id']}", headers=bob.headers).status_code == 404
    [invitation] = received(client, bob)
    assert (invitation["meeting_id"], invitation["title"], invitation["status"]) == (meeting["id"], "Planning", "pending")
    assert invitation["invited_by"] == {"id": alice.id, "email": alice.email, "name": None}
    assert "description" not in invitation and "attendees" not in invitation

    accepted = client.post(f"/invitations/{invitation['id']}/accept", headers=bob.headers)

    assert accepted.status_code == 200 and accepted.json()["status"] == "accepted"
    fetched = client.get(f"/meetings/{meeting['id']}", headers=bob.headers).json()
    assert [a["id"] for a in fetched["attendees"]] == [alice.id, bob.id]
    assert received(client, bob) == []
    [owner_view] = client.get(f"/meetings/{meeting['id']}/invitations", headers=alice.headers).json()
    assert (owner_view["email"], owner_view["status"]) == (bob.email, "accepted")


def test_declined_invitation_is_visible_to_owner_only_as_a_status(client, alice, bob):
    meeting = create_meeting(client, alice, invitee_emails=[bob.email])
    [invitation] = received(client, bob)

    assert client.post(f"/invitations/{invitation['id']}/decline", headers=bob.headers).json()["status"] == "declined"

    assert client.get(f"/meetings/{meeting['id']}", headers=bob.headers).status_code == 404
    assert client.get(f"/users/{alice.id}", headers=bob.headers).status_code == 404
    assert client.get(f"/users/{bob.id}", headers=alice.headers).status_code == 404
    assert [i["status"] for i in client.get(f"/meetings/{meeting['id']}/invitations", headers=alice.headers).json()] \
        == ["declined"]
    assert client.post(f"/invitations/{invitation['id']}/accept", headers=bob.headers).status_code == 404


def test_inviting_by_id_or_sequential_ids_grants_nothing(client, alice, bob):
    """The attack that motivated invitations: listing ids no longer adds anyone to a meeting."""
    meeting = create_meeting(client, alice, attendee_ids=[bob.id])

    assert [a["id"] for a in meeting["attendees"]] == [alice.id]
    assert client.get(f"/users/{bob.id}", headers=alice.headers).status_code == 404
    assert client.get("/meetings/", headers=bob.headers).json() == []


def test_inviting_does_not_reveal_who_is_registered(client, alice, bob):
    meeting = create_meeting(client, alice)

    response = client.post(f"/meetings/{meeting['id']}/invitations",
                           json={"emails": [bob.email, "nobody@example.com"]}, headers=alice.headers)

    assert response.status_code == 201
    assert [(i["email"], i["status"]) for i in response.json()] == [(bob.email, "pending"),
                                                                      ("nobody@example.com", "pending")]
    assert set(response.json()[0]) == {"id", "meeting_id", "email", "status", "created_at", "responded_at"}


def test_invitations_are_deduplicated_and_skip_the_owner(client, alice, bob):
    meeting = create_meeting(client, alice, invitee_emails=["BOB@example.com", bob.email, alice.email])

    again = client.post(f"/meetings/{meeting['id']}/invitations", json={"emails": [bob.email]}, headers=alice.headers)

    assert [i["email"] for i in again.json()] == [bob.email]


def test_only_the_owner_manages_invitations(client, alice, bob, carol):
    meeting = create_meeting(client, alice)
    invite_and_accept(client, alice, meeting["id"], bob)
    path = f"/meetings/{meeting['id']}/invitations"

    assert client.post(path, json={"emails": [carol.email]}, headers=bob.headers).status_code == 403
    assert client.get(path, headers=bob.headers).status_code == 403
    assert client.post(path, json={"emails": [carol.email]}, headers=carol.headers).status_code == 404
    assert client.get(path, headers=carol.headers).status_code == 404


def test_pending_invitees_are_hidden_from_other_attendees(client, alice, bob, carol):
    meeting = create_meeting(client, alice)
    invite_and_accept(client, alice, meeting["id"], bob)
    client.post(f"/meetings/{meeting['id']}/invitations", json={"emails": [carol.email]}, headers=alice.headers)

    fetched = client.get(f"/meetings/{meeting['id']}", headers=bob.headers).json()

    assert [a["id"] for a in fetched["attendees"]] == [alice.id, bob.id]
    assert client.get(f"/users/{carol.id}", headers=bob.headers).status_code == 404
    assert client.get(f"/users/{bob.id}", headers=carol.headers).status_code == 404


def test_users_cannot_act_on_invitations_addressed_to_others(client, alice, bob, carol):
    create_meeting(client, alice, invitee_emails=[bob.email])
    [invitation] = received(client, bob)

    assert received(client, carol) == []
    assert client.post(f"/invitations/{invitation['id']}/accept", headers=carol.headers).status_code == 404
    assert client.post(f"/invitations/{invitation['id']}/decline", headers=carol.headers).status_code == 404


def test_name_is_hidden_from_others_until_shared(client, alice, bob):
    meeting = create_meeting(client, alice)
    invite_and_accept(client, alice, meeting["id"], bob)

    assert client.get(f"/users/{bob.id}", headers=alice.headers).json() == {"id": bob.id, "email": bob.email,
                                                                              "name": None}
    assert client.get(f"/users/{bob.id}", headers=bob.headers).json()["name"] == "Bob"
    assert client.get("/users/me", headers=bob.headers).json()["share_name"] is False

    updated = client.put("/users/me", json={"share_name": True}, headers=bob.headers)

    assert updated.status_code == 200 and updated.json()["share_name"] is True
    assert client.get(f"/users/{bob.id}", headers=alice.headers).json()["name"] == "Bob"
    attendees = client.get(f"/meetings/{meeting['id']}", headers=alice.headers).json()["attendees"]
    assert attendees == [{"id": alice.id, "email": alice.email, "name": "Alice"},
                         {"id": bob.id, "email": bob.email, "name": "Bob"}]
    assert client.get("/meetings/", headers=alice.headers).json()[0]["attendees"] == attendees


def test_share_name_cannot_be_null(client, alice):
    assert client.put("/users/me", json={"share_name": None}, headers=alice.headers).status_code == 422


def test_attendee_can_leave_and_be_invited_again(client, alice, bob):
    meeting = create_meeting(client, alice)
    invite_and_accept(client, alice, meeting["id"], bob)

    assert client.delete(f"/meetings/{meeting['id']}/attendees/{bob.id}", headers=bob.headers).status_code == 204

    assert client.get(f"/meetings/{meeting['id']}", headers=bob.headers).status_code == 404
    assert client.get(f"/meetings/{meeting['id']}/invitations", headers=alice.headers).json() == []
    invite_and_accept(client, alice, meeting["id"], bob)
    assert client.get(f"/meetings/{meeting['id']}", headers=bob.headers).status_code == 200


@pytest.mark.parametrize("remover, target, status", [("bob", "carol", 403), ("alice", "alice", 422),
                                                      ("alice", "dave", 404)])
def test_attendee_removal_rules(client, alice, bob, carol, remover, target, status):
    users = {"alice": alice, "bob": bob, "carol": carol}
    meeting = create_meeting(client, alice)
    invite_and_accept(client, alice, meeting["id"], bob, carol)
    target_id = users[target].id if target in users else 999

    response = client.delete(f"/meetings/{meeting['id']}/attendees/{target_id}", headers=users[remover].headers)

    assert response.status_code == status


def test_owner_can_revoke_pending_but_not_accepted_invitations(client, alice, bob, carol):
    meeting = create_meeting(client, alice, invitee_emails=[carol.email])
    invite_and_accept(client, alice, meeting["id"], bob)
    by_email = {i["email"]: i for i in client.get(f"/meetings/{meeting['id']}/invitations",
                                                  headers=alice.headers).json()}
    path = f"/meetings/{meeting['id']}/invitations"

    assert client.delete(f"{path}/{by_email[bob.email]['id']}", headers=alice.headers).status_code == 409
    assert client.delete(f"{path}/{by_email[carol.email]['id']}", headers=alice.headers).status_code == 204
    assert received(client, carol) == []


def test_invitations_are_tools(client, alice, bob):
    meeting = create_meeting(client, alice)

    invited = client.post("/tools/meetings_invite", json={"meeting_id": meeting["id"], "data": {"emails": [bob.email]}},
                          headers=alice.headers)

    assert invited.status_code == 200
    [invitation] = client.post("/tools/meetings_received_invitations", json={}, headers=bob.headers).json()["result"]
    assert invitation["invited_by"] == {"id": alice.id, "email": alice.email, "name": None}
