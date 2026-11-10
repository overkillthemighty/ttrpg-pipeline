import re

import pytest
from fastapi.testclient import TestClient

from app import security
from app.main import app


def login(c: TestClient, user: str, password: str) -> str:
    tok = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    r = c.post("/login", data={"username": user, "password": password, "csrf": tok}, follow_redirects=False)
    assert r.status_code == 303, r.text[:300]
    return re.search(r'name="csrf-token" content="([^"]+)"', c.get("/").text).group(1)


@pytest.fixture(scope="module")
def admin():
    with TestClient(app) as c:
        csrf = login(c, "admin", "test-password-123")
        c.post("/settings/users", data={"username": "guest", "password": "guest-password-1", "csrf": csrf})
        yield c, csrf


def test_pages_render(admin):
    c, _ = admin
    for path in ("/", "/upload", "/record", "/client", "/settings/keys", "/settings/users", "/account"):
        assert c.get(path).status_code == 200, path


def test_idle_when_nothing_runs(admin):
    c, _ = admin
    r = c.get("/api/v1/health/idle")
    assert r.status_code == 200 and r.json()["idle"] is True


def test_guest_sees_progress_not_output(admin):
    c, _ = admin
    key = security.create_api_key("admin-laptop", "admin")
    r = c.post("/api/v1/jobs", headers={"X-Api-Key": key}, json={
        "title": "Admin session", "session_date": "2026-01-01",
        "files": [{"name": "DM", "filename": "DM.flac", "size": 10}], "options": {}})
    assert r.status_code == 201
    jid = r.json()["id"]
    busy = c.get("/api/v1/health/idle")  # a laptop upload that just started holds back an update
    assert busy.status_code == 503 and "upload" in busy.json()["reasons"][0]
    with TestClient(app) as g:
        login(g, "guest", "guest-password-1")
        j = g.get(f"/api/v1/jobs/{jid}").json()
        assert j["can_see_output"] is False and j["can_manage"] is False
        assert g.get(f"/api/v1/jobs/{jid}/log").status_code == 403
        assert g.get("/settings/users").status_code in (303, 403)


def test_cannot_remove_own_admin(admin):
    c, csrf = admin
    me = security.get_user_by_name("admin")
    r = c.post(f"/settings/users/{me['id']}/flag", data={"flag": "is_admin", "value": "0", "csrf": csrf},
               follow_redirects=True)
    assert "remove your own admin" in r.text
    assert security.get_user_by_name("admin")["is_admin"]
