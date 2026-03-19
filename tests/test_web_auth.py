import importlib
import sys

from fastapi.testclient import TestClient


def load_web_module(monkeypatch, password=None):
    if password is None:
        monkeypatch.delenv("APP_PASSWORD", raising=False)
    else:
        monkeypatch.setenv("APP_PASSWORD", password)
    monkeypatch.setenv("APP_SESSION_SECRET", "test-session-secret")

    sys.modules.pop("web", None)
    import web

    return importlib.reload(web)


def test_auth_disabled_by_default(monkeypatch):
    web_module = load_web_module(monkeypatch, password=None)
    client = TestClient(web_module.app)

    response = client.get("/api/auth/status")

    assert response.status_code == 200
    assert response.json() == {"enabled": False, "authenticated": True}


def test_protected_route_requires_login_when_password_enabled(monkeypatch):
    web_module = load_web_module(monkeypatch, password="secret123")
    client = TestClient(web_module.app)

    response = client.get("/api/bets")

    assert response.status_code == 401
    assert response.json()["detail"] == "Authentication required"


def test_login_flow_sets_authenticated_session(monkeypatch):
    web_module = load_web_module(monkeypatch, password="secret123")
    client = TestClient(web_module.app)

    response = client.post("/api/auth/login", json={"password": "wrong"})
    assert response.status_code == 401

    response = client.post("/api/auth/login", json={"password": "secret123"})
    assert response.status_code == 200
    assert response.json() == {"enabled": True, "authenticated": True}

    response = client.get("/api/auth/status")
    assert response.status_code == 200
    assert response.json() == {"enabled": True, "authenticated": True}

    response = client.post("/api/auth/logout")
    assert response.status_code == 200
    assert response.json() == {"enabled": True, "authenticated": False}

    response = client.get("/api/auth/status")
    assert response.status_code == 200
    assert response.json() == {"enabled": True, "authenticated": False}