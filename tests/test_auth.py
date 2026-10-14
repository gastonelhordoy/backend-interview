def test_register_does_not_expose_password(client):
    response = client.post("/users/", json={"name": "Dave", "email": "Dave@Example.com", "password": "long-enough"})

    assert response.status_code == 201
    assert response.json() == {"id": response.json()["id"], "name": "Dave", "email": "dave@example.com"}


def test_register_rejects_duplicate_email_case_insensitively(client, alice):
    response = client.post("/users/", json={"name": "Imposter", "email": "ALICE@example.com", "password": "long-enough"})

    assert response.status_code == 409


def test_register_rejects_short_password(client):
    response = client.post("/users/", json={"name": "Dave", "email": "dave@example.com", "password": "short"})

    assert response.status_code == 422


def test_login_with_wrong_password_fails(client, alice):
    response = client.post("/auth/token", data={"username": "alice@example.com", "password": "wrong-password"})

    assert response.status_code == 401


def test_login_with_unknown_email_fails(client):
    response = client.post("/auth/token", data={"username": "nobody@example.com", "password": "whatever-pass"})

    assert response.status_code == 401


def test_protected_endpoints_require_a_valid_token(client):
    assert client.get("/meetings/").status_code == 401
    assert client.get("/meetings/", headers={"Authorization": "Bearer not-a-token"}).status_code == 401


def test_me_returns_authenticated_user(client, alice):
    response = client.get("/users/me", headers=alice.headers)

    assert response.status_code == 200
    assert response.json()["id"] == alice.id
