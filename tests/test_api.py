from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import threading

from fastapi.testclient import TestClient

from deploy_butler.i18n import STRINGS
from deploy_butler.main import create_app
from deploy_butler.pipeline import check_health
from deploy_butler.security import mask_text


def _client(tmp_path: Path):
    app = create_app(tmp_path / "home")
    client = TestClient(app, base_url="http://127.0.0.1:8787")
    page = client.get("/")
    assert page.status_code == 200
    assert "__CSRF_TOKEN__" not in page.text
    token = page.cookies.get("butler_csrf")
    assert token
    return client, token


def _post(client, token, url, payload):
    return client.post(
        url,
        json=payload,
        headers={"Origin": "http://127.0.0.1:8787", "X-CSRF-Token": token},
    )


def test_csrf_host_and_export(tmp_path):
    client, token = _client(tmp_path)
    rejected_host = client.post(
        "/api/projects",
        json={"name": "myapp"},
        headers={"Host": "evil.example", "Origin": "http://127.0.0.1:8787", "X-CSRF-Token": token},
    )
    assert rejected_host.status_code == 400
    rejected_origin = client.post(
        "/api/projects",
        json={"name": "myapp"},
        headers={"Origin": "http://evil.example", "X-CSRF-Token": token},
    )
    assert rejected_origin.status_code == 403
    missing = client.post("/api/projects", json={"name": "myapp"}, headers={"Origin": "http://127.0.0.1:8787"})
    assert missing.status_code == 403
    created = _post(client, token, "/api/projects", {"name": "myapp"})
    assert created.status_code == 200, created.text
    project_id = created.json()["project"]["id"]
    exported = client.get(f"/api/projects/{project_id}/export")
    assert exported.status_code == 200
    payload = exported.json()
    assert payload["ssh_key_path"] == ""
    assert "PRIVATE KEY" not in exported.text
    assert "password" not in payload
    sneaky = dict(created.json()["project"])
    armor = "-" * 5
    sneaky["ssh_key_path"] = f"{armor}BEGIN OPENSSH PRIVATE KEY{armor}\nabc\n{armor}END OPENSSH PRIVATE KEY{armor}"
    imported = _post(client, token, "/api/import", sneaky)
    assert imported.status_code == 400


def test_i18n_and_js_keys():
    assert set(STRINGS["ru"]) == set(STRINGS["en"])
    for key, value in STRINGS["ru"].items():
        assert value.strip()
        assert STRINGS["en"][key].strip()
    script = Path("static/app.js").read_text(encoding="utf-8")
    keys = set(re.findall(r'\bt\(\s*"([a-z0-9_.]+)"', script))
    known = set(STRINGS["en"])

    def covered(key: str) -> bool:
        if key in known:
            return True
        return any(item.startswith(key) for item in known)

    missing = sorted(key for key in keys if not covered(key))
    assert missing == []
    assert "innerHTML" not in script


def test_mask_keeps_hashes():
    digest = "ab" * 32
    assert mask_text(digest) == digest
    assert "supersecret" not in mask_text("password=supersecret")
    assert "ghp_abcdefghijklmnop" not in mask_text("token ghp_abcdefghijklmnop")
    armor = "-" * 5
    pem = f"{armor}BEGIN OPENSSH PRIVATE KEY{armor}\nSECRETDATA\n{armor}END OPENSSH PRIVATE KEY{armor}"
    assert "SECRETDATA" not in mask_text(pem)


def test_health_does_not_follow_redirects():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/health":
                body = b"ok-marker"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(302)
            self.send_header("Location", "/health")
            self.end_headers()

        def log_message(self, _format, *_args):
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        ok, detail = check_health(f"http://127.0.0.1:{port}/go", 2, [302], "")
        assert ok, detail
        ok, detail = check_health(f"http://127.0.0.1:{port}/go", 2, [200], "")
        assert not ok
        ok, detail = check_health(f"http://127.0.0.1:{port}/health", 2, [200], "ok-marker")
        assert ok, detail
        ok, detail = check_health(f"http://127.0.0.1:{port}/health", 2, [200], "missing-text")
        assert not ok
    finally:
        server.shutdown()


def test_scanner_marks_deployed_update(tmp_path):
    from deploy_butler.db import Database
    from deploy_butler.scanner import inspect_folder, scan_updates
    from tests.conftest import make_project

    key = tmp_path / "key"
    key.write_text("x", encoding="utf-8")
    project = make_project(tmp_path, key)
    folder = Path(project.updates_dir) / "r35"
    folder.mkdir()
    (folder / "app.js").write_text("console.log(1)\n", encoding="utf-8")
    inspection = inspect_folder(folder, project.forbidden_patterns)
    db = Database(tmp_path / "home" / "butler.db")
    saved = db.create_project(project.public_dict())
    deploy_id = db.add_deploy(
        project_id=saved.id,
        kind="deploy",
        update_name="r35",
        update_sha256=inspection.update_sha256,
        release_name="r35",
        status="running",
        started_at="2026-01-01T00:00:00Z",
        actor="tester",
        backup_db=False,
    )
    db.finish_deploy(
        deploy_id,
        status="success",
        finished_at="2026-01-01T00:00:01Z",
        duration_ms=10,
        events=[],
        message_key="success",
    )
    found, error = scan_updates(saved, db.latest_deployed_sha(saved.id))
    assert error is None
    assert found[0].deployed is True
    assert found[0].name == "r35"
    raw = json.dumps(saved.public_dict())
    assert "PRIVATE KEY" not in raw
