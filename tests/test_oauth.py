from urllib.parse import parse_qs, urlsplit

import pytest

from tests.conftest import REDIRECT_URI, authorize, oauth_login, pkce_pair, register_oauth_client

PING = {"jsonrpc": "2.0", "id": 1, "method": "ping"}


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def refresh(client, oauth_client, refresh_token, **extra):
    return client.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": refresh_token,
                                             "client_id": oauth_client["client_id"], **extra})


def redirect_params(response):
    assert response.status_code == 302, response.text
    location = response.headers["location"]
    assert location.startswith(REDIRECT_URI)
    return {k: v[0] for k, v in parse_qs(urlsplit(location).query).items()}


# Discovery

def test_unauthenticated_mcp_request_points_to_the_metadata(client):
    response = client.post("/mcp", json=PING)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == (
        'Bearer resource_metadata="http://testserver/.well-known/oauth-protected-resource/mcp", scope="mcp"')


def test_discovery_metadata(client):
    resource = client.get("/.well-known/oauth-protected-resource/mcp").json()
    server = client.get("/.well-known/oauth-authorization-server").json()

    assert resource["resource"] == "http://testserver/mcp"
    assert resource["authorization_servers"] == ["http://testserver"]
    assert server["issuer"] == "http://testserver"
    assert server["code_challenge_methods_supported"] == ["S256"]
    assert server["registration_endpoint"] == "http://testserver/oauth/register"
    assert set(server["grant_types_supported"]) == {"authorization_code", "refresh_token"}


# Client registration

@pytest.mark.parametrize("redirect_uri", ["http://evil.example/cb", "javascript:alert(1)", "https://x/cb#frag"])
def test_registration_rejects_unsafe_redirect_uris(client, redirect_uri):
    response = client.post("/oauth/register", json={"redirect_uris": [redirect_uri]})

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_redirect_uri"


def test_registration_defaults_to_a_confidential_client(client):
    registered = client.post("/oauth/register", json={"redirect_uris": [REDIRECT_URI]}).json()

    assert registered["token_endpoint_auth_method"] == "client_secret_basic"
    assert registered["client_secret"]


# Login page

def test_login_page_renders_client_and_escapes_input(client):
    oauth_client = register_oauth_client(client, client_name="<b>Agent</b>")
    _, challenge = pkce_pair()

    page = client.get("/oauth/authorize", params={
        "client_id": oauth_client["client_id"], "redirect_uri": REDIRECT_URI, "response_type": "code",
        "state": '"><script>', "code_challenge": challenge, "code_challenge_method": "S256"})

    assert page.status_code == 200
    assert page.headers["x-frame-options"] == "DENY"
    assert "&lt;b&gt;Agent&lt;/b&gt;" in page.text
    assert '"><script>' not in page.text


def test_unregistered_redirect_uri_is_never_redirected_to(client):
    oauth_client = register_oauth_client(client)

    page = client.get("/oauth/authorize", params={"client_id": oauth_client["client_id"],
                                                  "redirect_uri": "https://evil.example/cb", "response_type": "code"},
                      follow_redirects=False)

    assert page.status_code == 400
    assert "location" not in page.headers


def test_loopback_redirect_uri_may_use_another_port(client, alice):
    oauth_client = register_oauth_client(client)
    _, challenge = pkce_pair()

    response = authorize(client, oauth_client, alice.email, challenge=challenge,
                         redirect_uri="http://localhost:50123/callback")

    assert response.headers["location"].startswith("http://localhost:50123/callback?code=")


def test_missing_pkce_is_redirected_as_error(client, alice):
    oauth_client = register_oauth_client(client)

    params = redirect_params(authorize(client, oauth_client, alice.email, challenge=None))

    assert params["error"] == "invalid_request"
    assert params["state"] == "xyz"


def test_wrong_password_re_renders_the_page(client, alice):
    oauth_client = register_oauth_client(client)
    _, challenge = pkce_pair()

    response = authorize(client, oauth_client, alice.email, password="wrong-password", challenge=challenge)

    assert response.status_code == 401
    assert "Incorrect email or password" in response.text
    assert "location" not in response.headers


def test_cancel_returns_access_denied(client, alice):
    oauth_client = register_oauth_client(client)
    _, challenge = pkce_pair()

    params = redirect_params(authorize(client, oauth_client, alice.email, challenge=challenge, action="deny"))

    assert params["error"] == "access_denied"
    assert params["iss"] == "http://testserver"


# Token endpoint

def _code(client, oauth_client, email):
    verifier, challenge = pkce_pair()
    params = redirect_params(authorize(client, oauth_client, email, challenge=challenge))
    return params["code"], verifier


def _exchange(client, oauth_client, code, verifier, **extra):
    return client.post("/oauth/token", data={"grant_type": "authorization_code", "code": code,
                                             "redirect_uri": REDIRECT_URI, "code_verifier": verifier,
                                             "client_id": oauth_client["client_id"], **extra})


def test_code_exchange_issues_mcp_only_tokens(client, alice):
    tokens = oauth_login(client, alice.email)

    assert tokens["token_type"] == "Bearer"
    assert tokens["expires_in"] == 30 * 60
    assert tokens["scope"] == "mcp"
    assert client.post("/mcp", json=PING, headers=bearer(tokens["access_token"])).status_code == 200
    # Audiences are separate: MCP tokens don't work on the REST API, REST tokens don't work on /mcp.
    assert client.get("/meetings/", headers=bearer(tokens["access_token"])).status_code == 401
    assert client.post("/mcp", json=PING, headers=alice.headers).status_code == 401


def test_code_requires_the_right_pkce_verifier_and_is_single_use(client, alice):
    oauth_client = register_oauth_client(client)
    code, verifier = _code(client, oauth_client, alice.email)

    wrong = _exchange(client, oauth_client, code, pkce_pair()[0])
    retry = _exchange(client, oauth_client, code, verifier)

    assert wrong.json()["error"] == "invalid_grant"
    assert retry.json()["error"] == "invalid_grant"  # consumed by the failed attempt


def test_code_is_bound_to_its_client(client, alice):
    oauth_client, other = register_oauth_client(client), register_oauth_client(client)
    code, verifier = _code(client, oauth_client, alice.email)

    assert _exchange(client, other, code, verifier).json()["error"] == "invalid_grant"


def test_confidential_client_must_authenticate(client, alice):
    oauth_client = register_oauth_client(client, token_endpoint_auth_method="client_secret_post")
    code, verifier = _code(client, oauth_client, alice.email)

    without_secret = _exchange(client, oauth_client, code, verifier)
    assert without_secret.status_code == 401
    assert without_secret.json()["error"] == "invalid_client"

    code, verifier = _code(client, oauth_client, alice.email)
    ok = _exchange(client, oauth_client, code, verifier, client_secret=oauth_client["client_secret"])
    assert ok.status_code == 200


def test_client_secret_basic(client, alice):
    oauth_client = register_oauth_client(client, token_endpoint_auth_method="client_secret_basic")
    code, verifier = _code(client, oauth_client, alice.email)

    response = client.post("/oauth/token", auth=(oauth_client["client_id"], oauth_client["client_secret"]),
                           data={"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI,
                                 "code_verifier": verifier})

    assert response.status_code == 200


# Refresh tokens

def test_refresh_rotates_tokens(client, alice):
    oauth_client = register_oauth_client(client)
    first = oauth_login(client, alice.email, oauth_client)

    second = refresh(client, oauth_client, first["refresh_token"])

    assert second.status_code == 200
    assert second.json()["refresh_token"] != first["refresh_token"]
    assert client.post("/mcp", json=PING, headers=bearer(second.json()["access_token"])).status_code == 200


def test_refresh_token_reuse_revokes_the_whole_grant(client, alice):
    oauth_client = register_oauth_client(client)
    first = oauth_login(client, alice.email, oauth_client)
    second = refresh(client, oauth_client, first["refresh_token"]).json()

    reuse = refresh(client, oauth_client, first["refresh_token"])

    assert reuse.json()["error"] == "invalid_grant"
    assert refresh(client, oauth_client, second["refresh_token"]).json()["error"] == "invalid_grant"
    assert client.post("/mcp", json=PING, headers=bearer(second["access_token"])).status_code == 401


def test_refresh_token_is_bound_to_its_client(client, alice):
    first = oauth_login(client, alice.email)

    assert refresh(client, register_oauth_client(client), first["refresh_token"]).json()["error"] == "invalid_grant"


def test_refresh_requires_the_grant_type_to_be_registered(client, alice):
    oauth_client = register_oauth_client(client, grant_types=["authorization_code"])
    tokens = oauth_login(client, alice.email, oauth_client)

    assert refresh(client, oauth_client, tokens["refresh_token"]).json()["error"] == "unauthorized_client"


def test_revocation_logs_the_integration_out(client, alice):
    oauth_client = register_oauth_client(client)
    tokens = oauth_login(client, alice.email, oauth_client)

    response = client.post("/oauth/revoke", data={"token": tokens["refresh_token"],
                                                  "client_id": oauth_client["client_id"]})

    assert response.status_code == 200
    assert client.post("/mcp", json=PING, headers=bearer(tokens["access_token"])).status_code == 401
    assert refresh(client, oauth_client, tokens["refresh_token"]).json()["error"] == "invalid_grant"
