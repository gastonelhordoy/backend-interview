import json

import pytest


def rpc(client, user, method, params=None, request_id=1):
    response = client.post("/mcp", json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}},
                           headers=user.mcp_headers)
    assert response.status_code == 200, response.text
    return response.json()


def call_tool(client, user, name, arguments):
    result = rpc(client, user, "tools/call", {"name": name, "arguments": arguments})["result"]
    text = result["content"][0]["text"]
    return result["isError"], (text if result["isError"] or not text.startswith(("{", "[")) else json.loads(text))


def test_initialize_negotiates_protocol_version(client, alice):
    result = rpc(client, alice, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                               "clientInfo": {"name": "test", "version": "0"}})["result"]

    assert result["protocolVersion"] == "2025-06-18"
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert rpc(client, alice, "initialize", {"protocolVersion": "1999-01-01"})["result"]["protocolVersion"] == "2025-11-25"


def test_notifications_are_accepted_without_body(client, alice):
    response = client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                           headers=alice.mcp_headers)

    assert response.status_code == 202


def test_tools_list_exposes_decorated_endpoints(client, alice):
    tools = {t["name"]: t for t in rpc(client, alice, "tools/list")["result"]["tools"]}

    assert set(tools) == {
        "create_user", "list_users", "get_me", "update_me", "get_user",
        "create_meeting", "list_meetings", "get_meeting", "update_meeting", "delete_meeting",
        "invite_to_meeting", "list_meeting_invitations", "revoke_invitation", "remove_attendee",
        "list_my_invitations", "accept_invitation", "decline_invitation",
        "create_note", "list_notes", "get_note",
        "create_task", "list_tasks", "get_task", "update_task", "delete_task",
    }
    update_task = tools["update_task"]
    assert update_task["description"].endswith("HTTP: PUT /tasks/{task_id}")
    assert update_task["inputSchema"]["required"] == ["task_id", "data"]
    assert update_task["inputSchema"]["properties"]["data"] == {"$ref": "#/$defs/TaskUpdate"}
    assert set(tools["list_tasks"]["inputSchema"]["properties"]) == {"meeting_id", "status"}
    assert tools["list_tasks"]["annotations"]["readOnlyHint"] is True
    assert tools["delete_meeting"]["annotations"]["destructiveHint"] is True


def test_tool_calls_round_trip_through_the_api(client, alice):
    is_error, meeting = call_tool(client, alice, "create_meeting",
                                  {"data": {"title": "Planning", "scheduled_time": "2024-01-15T10:00:00"}})
    assert not is_error and meeting["owner_id"] == alice.id

    is_error, task = call_tool(client, alice, "create_task", {"data": {"title": "t", "due_meeting_id": meeting["id"]}})
    assert not is_error

    # Partial update: only status is sent, the title must survive.
    is_error, updated = call_tool(client, alice, "update_task", {"task_id": task["id"], "data": {"status": "completed"}})
    assert not is_error and (updated["status"], updated["title"]) == ("completed", "t")

    is_error, tasks = call_tool(client, alice, "list_tasks", {"status": "completed"})
    assert [t["id"] for t in tasks] == [task["id"]]

    is_error, text = call_tool(client, alice, "delete_task", {"task_id": task["id"]})
    assert not is_error and text == "HTTP 204: done"


def test_tool_calls_run_as_the_signed_in_user(client, alice, bob):
    _, meeting = call_tool(client, alice, "create_meeting",
                           {"data": {"title": "Private", "scheduled_time": "2024-01-15T10:00:00"}})

    is_error, me = call_tool(client, bob, "get_me", {})
    assert not is_error and me["id"] == bob.id

    is_error, text = call_tool(client, bob, "get_meeting", {"meeting_id": meeting["id"]})
    assert is_error and text.startswith("HTTP 404")


def test_invalid_arguments_are_reported_as_tool_errors(client, alice):
    is_error, text = call_tool(client, alice, "get_meeting", {"meeting_id": "abc", "extra": 1})

    assert is_error and text.startswith("Invalid arguments")


@pytest.mark.parametrize("method, params, code", [
    ("tools/call", {"name": "nope", "arguments": {}}, -32602),
    ("resources/list", {}, -32601),
])
def test_protocol_errors(client, alice, method, params, code):
    assert rpc(client, alice, method, params)["error"]["code"] == code


def test_rejects_foreign_origins(client, alice):
    response = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                           headers={"Origin": "https://evil.example", **alice.mcp_headers})

    assert response.status_code == 403
    assert client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                       headers={"Origin": "http://localhost:6274", **alice.mcp_headers}).status_code == 200
