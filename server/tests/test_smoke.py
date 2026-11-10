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
    for path in ("/", "/guide", "/upload", "/record", "/client", "/settings/keys", "/settings/users", "/account"):
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


def test_guide_is_public():
    with TestClient(app) as anon:
        r = anon.get("/guide", follow_redirects=False)
        assert r.status_code == 200 and "Three ways to get audio in" in r.text


def test_reads_commit_from_git_dir(tmp_path):
    from app.updates import read_commit
    sha = "a" * 40
    (tmp_path / "HEAD").write_text("ref: refs/heads/main\n")
    (tmp_path / "packed-refs").write_text(f"# pack-refs\n{sha} refs/heads/main\n")
    assert read_commit(tmp_path) == sha
    (tmp_path / "refs" / "heads").mkdir(parents=True)
    (tmp_path / "refs" / "heads" / "main").write_text("b" * 40 + "\n")
    assert read_commit(tmp_path) == "b" * 40
    assert read_commit(tmp_path / "missing") == ""


def _commit(sha: str, msg: str, date: str = "2020-01-01T00:00:00Z") -> dict:
    return {"sha": sha * 40, "html_url": "u", "commit": {"message": msg, "committer": {"date": date}}}


def test_update_status_ignores_docs_only_changes():
    from app.updates import Status, apply_compare
    st = Status(commit="0" * 40)
    apply_compare(st, {"status": "ahead", "commits": [_commit("1", "Docs")], "files": [{"filename": "README.md"}]})
    assert st.state == "current"
    st = Status(commit="0" * 40)
    apply_compare(st, {"status": "identical", "commits": [], "files": []})
    assert st.state == "current"


def test_update_status_flags_code_changes():
    import time
    from datetime import datetime, timezone
    from app.updates import Status, apply_compare
    st = Status(commit="0" * 40)
    apply_compare(st, {"status": "ahead", "files": [{"filename": "server/app/web.py"}, {"filename": "README.md"}],
                       "commits": [_commit("1", "Old fix\n\nbody"), _commit("2", "New page")]})
    assert st.state == "behind" and [c["title"] for c in st.commits] == ["New page", "Old fix"]
    assert st.latest == "2" * 40
    just_now = datetime.fromtimestamp(time.time() - 60, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    st = Status(commit="0" * 40)
    apply_compare(st, {"status": "ahead", "files": [{"filename": "server/client/record.ps1"}],
                       "commits": [_commit("3", "Recorder", just_now)]})
    assert st.state == "pending"
    st = Status(commit="0" * 40)   # an old push isn't "out of date" while a session keeps the server busy
    apply_compare(st, {"status": "ahead", "files": [{"filename": "server/app/jobs.py"}],
                       "commits": [_commit("4", "Fix")]}, ["a job is running"])
    assert st.state == "pending" and "a job is running" in st.detail


def test_update_check_is_admin_only(admin):
    c, csrf = admin
    r = c.post("/settings/update-check", data={"csrf": csrf, "next": "/"}, follow_redirects=False)
    assert r.status_code == 303
    assert "Version" in c.get("/").text
    with TestClient(app) as g:
        login(g, "guest", "guest-password-1")
        tok = re.search(r'name="csrf-token" content="([^"]+)"', g.get("/").text).group(1)
        r = g.post("/settings/update-check", data={"csrf": tok}, follow_redirects=False)
        assert r.status_code in (303, 403)
