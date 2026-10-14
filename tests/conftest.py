import base64
import hashlib
import os
import secrets
from urllib.parse import parse_qs, urlsplit

# Must be set before the app is imported: in-memory DB and cheap password hashing.
os.environ["DATABASE_URL"] = "sqlite://"
os.environ["PASSWORD_HASH_ITERATIONS"] = "1000"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.mesh import factory  # noqa: E402
from app.mesh.factory import cache  # noqa: E402
from app.mesh.registry import default_registry  # noqa: E402
from app.models import Base  # noqa: E402

MESH_NODE_URL = "http://mesh-node.test"


@pytest.fixture(autouse=True)
def _fresh_state():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    cache.clear()
    yield


@pytest.fixture
def remote_mesh(monkeypatch):
    """Route *every* service through HttpTransport. The "remote node" is this same app,
    reached in-process over real HTTP requests, so each cross-service call really goes
    through serialization, the /mesh endpoint and the mesh token."""
    services = sorted({spec.service for spec in default_registry.operations()})
    monkeypatch.setattr(settings, "mesh_remote_services", {name: MESH_NODE_URL for name in services})
    monkeypatch.setattr(factory, "http_client", lambda base_url: TestClient(app, base_url=base_url))


@pytest.fixture(params=["local", "http"])
def client(request):
    """API client; every API test runs once per mesh mode to prove they behave the same."""
    if request.param == "http":
        request.getfixturevalue("remote_mesh")
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def db():
    session = SessionLocal()
    yield session
    session.close()


REDIRECT_URI = "http://localhost:33418/callback"


def pkce_pair():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def register_oauth_client(client: TestClient, **metadata) -> dict:
    payload = {"client_name": "Test Agent", "redirect_uris": [REDIRECT_URI],
               "grant_types": ["authorization_code", "refresh_token"], "token_endpoint_auth_method": "none",
               **metadata}
    response = client.post("/oauth/register", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def authorize(client: TestClient, oauth_client: dict, email: str, password: str = "secret-password",
              challenge: str = None, **extra):
    """Submit the login page as a browser would; returns the redirect response."""
    form = {"client_id": oauth_client["client_id"], "redirect_uri": REDIRECT_URI, "response_type": "code",
            "state": "xyz", "code_challenge": challenge, "code_challenge_method": "S256", "scope": "mcp",
            "email": email, "password": password, "action": "approve", **extra}
    return client.post("/oauth/authorize", data=form, follow_redirects=False)


def oauth_login(client: TestClient, email: str, oauth_client: dict = None) -> dict:
    """Run the whole authorization-code + PKCE flow and return the token response."""
    oauth_client = oauth_client or register_oauth_client(client)
    verifier, challenge = pkce_pair()
    redirect = authorize(client, oauth_client, email, challenge=challenge)
    assert redirect.status_code == 302, redirect.text
    code = parse_qs(urlsplit(redirect.headers["location"]).query)["code"][0]
    response = client.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI,
        "code_verifier": verifier, "client_id": oauth_client["client_id"]})
    assert response.status_code == 200, response.text
    return response.json()


class ApiUser:
    def __init__(self, client: TestClient, name: str):
        self.client = client
        self.email = f"{name.lower()}@example.com"
        response = client.post("/users/", json={"name": name, "email": self.email, "password": "secret-password"})
        assert response.status_code == 201, response.text
        self.id = response.json()["id"]
        token = client.post("/auth/token", data={"username": self.email, "password": "secret-password"})
        assert token.status_code == 200, token.text
        self.headers = {"Authorization": f"Bearer {token.json()['access_token']}"}
        self._mcp_headers = None

    @property
    def mcp_headers(self) -> dict:
        """Headers with an MCP access token, obtained through the OAuth flow."""
        if self._mcp_headers is None:
            tokens = oauth_login(self.client, self.email)
            self._mcp_headers = {"Authorization": f"Bearer {tokens['access_token']}"}
        return self._mcp_headers


@pytest.fixture
def alice(client):
    return ApiUser(client, "Alice")


@pytest.fixture
def bob(client):
    return ApiUser(client, "Bob")


@pytest.fixture
def carol(client):
    return ApiUser(client, "Carol")
