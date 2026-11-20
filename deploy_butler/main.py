"""Local FastAPI app. Binds to 127.0.0.1 and opens the browser."""

from __future__ import annotations

import getpass
import html
import json
import os
import re
import secrets
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from deploy_butler import __version__
from deploy_butler.db import Database, Project, utcnow
from deploy_butler.defaults import RELEASE_RE_SRC
from deploy_butler.helper_template import install_steps, render_helper, render_sudoers
from deploy_butler.i18n import STRINGS, translate
from deploy_butler.notify import notify_os
from deploy_butler.pipeline import (
    Timing,
    check_health,
    run_deploy,
    run_list_backups,
    run_manual_rollback,
    run_prune,
)
from deploy_butler.scanner import inspect_update, iso_mtime, scan_updates, short_hash
from deploy_butler.security import allowed_hosts, allowed_origins, mask_text, same_token
from deploy_butler.ssh import (
    HostKeyChanged,
    HostKeyUnknown,
    Unreachable,
    connect,
    save_host_key,
)
from deploy_butler.validate import apply_defaults, looks_like_secret, readiness_errors, validate_fields

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_ARCHIVE_SUFFIXES = (".tar.gz", ".tgz", ".tar.bz2", ".tar", ".zip")


def default_home() -> Path:
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home())
        return Path(base) / "deploy-butler"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "deploy-butler"
    return Path.home() / ".local" / "share" / "deploy-butler"


def asset_root() -> Path:
    here = Path(__file__).resolve().parent
    for candidate in (here.parent, here):
        if (candidate / "templates" / "index.html").is_file():
            return candidate
    return here.parent


class RunLog:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.cond = threading.Condition()
        self.done = False
        self.outcome: dict[str, Any] | None = None

    def add(self, event: dict[str, Any]) -> None:
        with self.cond:
            self.events.append(event)
            self.cond.notify_all()

    def finish(self, payload: dict[str, Any]) -> None:
        with self.cond:
            self.done = True
            self.outcome = payload
            self.cond.notify_all()

    def wait_from(self, index: int, timeout: float = 15.0):
        with self.cond:
            if index >= len(self.events) and not self.done:
                self.cond.wait(timeout=timeout)
            return list(self.events[index:]), self.done, self.outcome


class Runtime:
    def __init__(self, home: Path):
        self.home = home
        self.db = Database(home / "butler.db")
        self.known_hosts = home / "known_hosts"
        self.port = int(os.environ.get("DEPLOY_BUTLER_PORT", "8787"))
        self.connector = connect
        self.health = check_health
        self.timing = Timing()
        self.runs: dict[int, RunLog] = {}
        self.pending: dict[tuple[int, str], dict[str, Any]] = {}
        self.lock = threading.Lock()

    def remember(self, deploy_id: int, log: RunLog) -> None:
        with self.lock:
            self.runs[deploy_id] = log
            if len(self.runs) > 40:
                finished = [key for key, value in self.runs.items() if value.done and key != deploy_id]
                for key in finished[:-20]:
                    self.runs.pop(key, None)


def create_app(home: Path | None = None) -> FastAPI:
    runtime = Runtime(home or Path(os.environ.get("DEPLOY_BUTLER_HOME", default_home())))
    app = FastAPI(title="Deploy Butler", version=__version__, docs_url=None, redoc_url=None)
    app.state.runtime = runtime
    root = asset_root()
    app.mount("/static", StaticFiles(directory=root / "static"), name="static")

    @app.middleware("http")
    async def guard(request: Request, call_next):
        port = request.app.state.runtime.port
        host = request.headers.get("host", "")
        if host not in allowed_hosts(port):
            return JSONResponse({"error": "bad_host"}, status_code=400)
        if request.method in {"POST", "PUT", "DELETE", "PATCH"}:
            origin = request.headers.get("origin")
            if origin not in allowed_origins(port):
                return JSONResponse({"error": "bad_origin"}, status_code=403)
            cookie = request.cookies.get("butler_csrf", "")
            header = request.headers.get("x-csrf-token", "")
            if not same_token(cookie, header):
                return JSONResponse({"error": "csrf"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self'; script-src 'self'; "
            "connect-src 'self'; img-src 'self'; base-uri 'none'; form-action 'self'"
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    def lang_of(request: Request) -> str:
        value = request.headers.get("x-butler-lang", "ru")
        return value if value in STRINGS else "ru"

    def actor() -> str:
        try:
            return getpass.getuser()
        except Exception:
            return "local"

    def project_or_404(project_id: int) -> Project:
        project = runtime.db.get_project(project_id)
        if project is None:
            raise LookupError(project_id)
        return project

    def json_error(status: int, error: str, **extra: Any) -> JSONResponse:
        return JSONResponse({"error": error, **extra}, status_code=status)

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> FileResponse:
        return FileResponse(root / "static" / "favicon.svg", media_type="image/svg+xml")

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        token = _csrf_token(request)
        page = (root / "templates" / "index.html").read_text(encoding="utf-8")
        page = page.replace("__CSRF_TOKEN__", html.escape(token, quote=True))
        response = HTMLResponse(page)
        _set_csrf(response, token)
        return response

    @app.get("/api/bootstrap")
    def bootstrap() -> dict[str, Any]:
        return {"version": __version__, "i18n": STRINGS, "release_rule": RELEASE_RE_SRC}

    @app.get("/api/projects")
    def list_projects() -> dict[str, Any]:
        return {"projects": [item.public_dict() for item in runtime.db.list_projects()]}

    @app.post("/api/projects")
    async def create_project(request: Request) -> JSONResponse:
        payload = await _body(request)
        payload = apply_defaults(payload)
        errors = validate_fields(payload)
        if errors:
            return json_error(400, "validation", fields=errors)
        if runtime.db.get_project_by_name(payload["name"]):
            return json_error(409, "exists")
        project = runtime.db.create_project(payload)
        return JSONResponse({"project": project.public_dict()})

    @app.get("/api/projects/{project_id}")
    def get_project(project_id: int) -> JSONResponse:
        try:
            project = project_or_404(project_id)
        except LookupError:
            return json_error(404, "not_found")
        data = project.public_dict()
        data["ready"] = not readiness_errors(data)
        return JSONResponse({"project": data})

    @app.put("/api/projects/{project_id}")
    async def update_project(project_id: int, request: Request) -> JSONResponse:
        try:
            project_or_404(project_id)
        except LookupError:
            return json_error(404, "not_found")
        payload = apply_defaults(await _body(request))
        errors = validate_fields(payload)
        if errors:
            return json_error(400, "validation", fields=errors)
        other = runtime.db.get_project_by_name(payload["name"])
        if other and other.id != project_id:
            return json_error(409, "exists")
        saved = runtime.db.update_project(project_id, payload)
        if saved is None:
            return json_error(404, "not_found")
        return JSONResponse({"project": saved.public_dict()})

    @app.delete("/api/projects/{project_id}")
    def delete_project(project_id: int) -> JSONResponse:
        if not runtime.db.delete_project(project_id):
            return json_error(404, "not_found")
        return JSONResponse({"ok": True})

    @app.get("/api/projects/{project_id}/export")
    def export_project(project_id: int) -> Response:
        try:
            project = project_or_404(project_id)
        except LookupError:
            return json_error(404, "not_found")
        payload = project.public_dict()
        payload.pop("id", None)
        payload.pop("created_at", None)
        payload.pop("updated_at", None)
        payload["exported_by"] = "deploy-butler"
        payload["version"] = 1
        raw = json.dumps(payload, ensure_ascii=False, indent=2)
        if looks_like_secret(raw):
            return json_error(400, "unsafe")
        return Response(
            content=raw,
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="deploy-butler-{project.name}.json"'},
        )

    @app.post("/api/import")
    async def import_project(request: Request) -> JSONResponse:
        payload = await _body(request)
        if not isinstance(payload, dict):
            return json_error(400, "validation")
        payload.pop("exported_by", None)
        payload.pop("version", None)
        payload.pop("id", None)
        flat = json.dumps(payload, ensure_ascii=False)
        if looks_like_secret(flat):
            return json_error(400, "unsafe")
        normalized = apply_defaults(payload)
        errors = validate_fields(normalized)
        if errors:
            return json_error(400, "validation", fields=errors)
        existing = runtime.db.get_project_by_name(normalized["name"])
        if existing:
            saved = runtime.db.update_project(existing.id, normalized)
        else:
            saved = runtime.db.create_project(normalized)
        if saved is None:
            return json_error(404, "not_found")
        return JSONResponse({"project": saved.public_dict()})

    @app.post("/api/check-dir")
    async def check_dir(request: Request) -> JSONResponse:
        payload = await _body(request)
        raw = str(payload.get("updates_dir", "")).strip()
        fmt = str(payload.get("update_format", "folder"))
        if not raw or any(char in raw for char in "\n\r\x00"):
            return JSONResponse({"ok": False, "error": "invalid"})
        path = Path(raw).expanduser()
        if not path.is_dir():
            return JSONResponse({"ok": False, "error": "missing"})
        if fmt == "archive":
            count = sum(1 for item in path.iterdir() if item.is_file() and item.name.lower().endswith(_ARCHIVE_SUFFIXES))
        else:
            count = sum(1 for item in path.iterdir() if item.is_dir() or item.is_symlink())
        return JSONResponse({"ok": True, "count": count})

    @app.get("/api/projects/{project_id}/updates")
    def updates(project_id: int) -> JSONResponse:
        try:
            project = project_or_404(project_id)
        except LookupError:
            return json_error(404, "not_found")
        rows, error = scan_updates(project, runtime.db.latest_deployed_sha(project.id))
        return JSONResponse(
            {
                "error": error,
                "updates": [
                    {
                        "name": item.name,
                        "release": item.release,
                        "sha256": item.sha256,
                        "sha256_short": short_hash(item.sha256),
                        "size": item.size,
                        "file_count": item.file_count,
                        "mtime": iso_mtime(item.mtime),
                        "deployed": item.deployed,
                        "blocked": item.blocked,
                    }
                    for item in rows
                ],
            }
        )

    @app.get("/api/projects/{project_id}/helper")
    def helper(project_id: int) -> JSONResponse:
        try:
            project = project_or_404(project_id)
        except LookupError:
            return json_error(404, "not_found")
        try:
            script = render_helper(project)
            sudoers = render_sudoers(project)
        except ValueError:
            return JSONResponse({"ready": False, "steps": []})
        return JSONResponse(
            {
                "ready": True,
                "script": script,
                "sudoers": sudoers,
                "script_name": f"deploy-butler-{project.name}",
                "sudoers_name": f"deploy-butler-{project.name}.sudoers",
                "script_path": project.helper_path,
                "sudoers_path": project.sudoers_path,
                "steps": install_steps(project),
            }
        )

    @app.post("/api/projects/{project_id}/preview")
    async def preview(project_id: int, request: Request) -> JSONResponse:
        try:
            project = project_or_404(project_id)
        except LookupError:
            return json_error(404, "not_found")
        payload = await _body(request)
        name = str(payload.get("update_name", ""))
        inspection = inspect_update(project, name)
        if inspection is None:
            return json_error(404, "not_found")
        body = _inspection_payload(inspection)
        body["backup_db_default"] = bool(project.db_backup_paths)
        body["ready"] = not readiness_errors(project.public_dict())
        if inspection.blocked or not body["ready"]:
            return JSONResponse(body)
        remote = None
        try:
            remote = runtime.connector(project, runtime.timing.ssh_timeout)
            stdin = "".join(f"{item.path}\n" for item in inspection.files)
            result = remote.helper(["hashes"], stdin=stdin)
            if result.exit_code != 0:
                body["hashes_ok"] = False
                body["busy"] = result.exit_code == 2
                return JSONResponse(body)
            found = {}
            for line in result.stdout.splitlines():
                if "\t" not in line:
                    continue
                token, path = line.split("\t", 1)
                found[path.strip()] = token.strip().lower()
            for item in body["files"]:
                server = found.get(item["path"])
                if server is None or server == "missing":
                    item["state"] = "new"
                elif server == item["sha256"]:
                    item["state"] = "unchanged"
                else:
                    item["state"] = "changed"
            body["hashes_ok"] = True
            return JSONResponse(body)
        except HostKeyUnknown as exc:
            _remember_key(runtime, project, exc.key, changed=False)
            body["needs_hostkey"] = True
            body["changed"] = False
            body["fingerprint"] = exc.fingerprint
            body["key_type"] = exc.key_type
            return JSONResponse(body)
        except HostKeyChanged as exc:
            _remember_key(runtime, project, exc.key, changed=True)
            body["needs_hostkey"] = True
            body["changed"] = True
            body["fingerprint"] = exc.fingerprint
            body["key_type"] = exc.key_type
            return JSONResponse(body)
        except Unreachable:
            body["unreachable"] = True
            body["hint"] = project.unreachable_hint
            return JSONResponse(body)
        finally:
            if remote is not None:
                remote.close()

    @app.post("/api/host-keys/confirm")
    async def confirm_host_key(request: Request) -> JSONResponse:
        payload = await _body(request)
        try:
            project = project_or_404(int(payload.get("project_id")))
        except (LookupError, TypeError, ValueError):
            return json_error(404, "not_found")
        fingerprint = str(payload.get("fingerprint", ""))
        pending = runtime.pending.get((project.id, fingerprint))
        if not pending or time.time() - pending["at"] > 600:
            return json_error(404, "not_found")
        if pending["host"] != project.ssh_host or pending["port"] != project.ssh_port:
            return json_error(409, "mismatch")
        save_host_key(runtime.known_hosts, project.ssh_host, project.ssh_port, pending["key"])
        runtime.pending.pop((project.id, fingerprint), None)
        return JSONResponse({"ok": True})

    @app.post("/api/projects/{project_id}/deploys")
    async def start_deploy(project_id: int, request: Request) -> JSONResponse:
        try:
            project = project_or_404(project_id)
        except LookupError:
            return json_error(404, "not_found")
        payload = await _body(request)
        name = str(payload.get("update_name", ""))
        inspection = inspect_update(project, name)
        if inspection is None:
            return json_error(404, "not_found")
        if inspection.blocked:
            return json_error(400, "blocked")
        backup_db = bool(payload.get("backup_db")) and bool(project.db_backup_paths)
        os_notify = bool(payload.get("os_notify"))
        language = lang_of(request)
        deploy_id = runtime.db.add_deploy(
            project_id=project.id,
            kind="deploy",
            update_name=inspection.name,
            update_sha256=inspection.update_sha256,
            release_name=inspection.release,
            status="running",
            started_at=utcnow(),
            actor=actor(),
            backup_db=backup_db,
        )
        log = RunLog()
        runtime.remember(deploy_id, log)

        def worker() -> None:
            result = run_deploy(
                project,
                inspection,
                backup_db=backup_db,
                home=runtime.home,
                connector=runtime.connector,
                health=runtime.health,
                timing=runtime.timing,
                on_event=log.add,
                lang=language,
            )
            runtime.db.finish_deploy(
                deploy_id,
                status=result.status,
                finished_at=result.finished_at,
                duration_ms=result.duration_ms,
                events=result.events,
                message_key=result.message_key,
                update_sha256=result.update_sha256,
            )
            log.finish({"status": result.status, "duration_ms": result.duration_ms, "release": result.release})
            if os_notify:
                notify_os(
                    "Deploy Butler",
                    translate(language, f"notify.{result.status}", name=inspection.name),
                )

        threading.Thread(target=worker, name=f"deploy-{deploy_id}", daemon=True).start()
        return JSONResponse({"deploy_id": deploy_id})

    @app.post("/api/projects/{project_id}/history/{deploy_id}/rollback")
    async def start_rollback(project_id: int, deploy_id: int, request: Request) -> JSONResponse:
        try:
            project = project_or_404(project_id)
        except LookupError:
            return json_error(404, "not_found")
        record = runtime.db.get_deploy(project_id, deploy_id)
        if record is None or record["kind"] != "deploy":
            return json_error(404, "not_found")
        if record["status"] == "error_before_changes" or record["status"] == "running":
            return json_error(409, "nothing_to_roll_back")
        payload = await _body(request)
        if payload.get("confirm") is not True:
            return json_error(400, "confirm")
        language = lang_of(request)
        os_notify = bool(payload.get("os_notify"))
        new_id = runtime.db.add_deploy(
            project_id=project.id,
            kind="rollback",
            update_name=record["update_name"],
            update_sha256=record["update_sha256"],
            release_name=record["release_name"],
            status="running",
            started_at=utcnow(),
            actor=actor(),
            backup_db=bool(record["backup_db"]),
        )
        log = RunLog()
        runtime.remember(new_id, log)

        def worker() -> None:
            result = run_manual_rollback(
                project,
                release=record["release_name"],
                update_name=record["update_name"],
                update_sha256=record["update_sha256"],
                backup_db=bool(record["backup_db"]),
                home=runtime.home,
                connector=runtime.connector,
                health=runtime.health,
                timing=runtime.timing,
                on_event=log.add,
                lang=language,
            )
            runtime.db.finish_deploy(
                new_id,
                status=result.status,
                finished_at=result.finished_at,
                duration_ms=result.duration_ms,
                events=result.events,
                message_key=result.message_key,
            )
            log.finish({"status": result.status, "duration_ms": result.duration_ms, "release": result.release})
            if os_notify:
                notify_os("Deploy Butler", translate(language, f"notify.{result.status}", name=record["update_name"]))

        threading.Thread(target=worker, name=f"rollback-{new_id}", daemon=True).start()
        return JSONResponse({"deploy_id": new_id})

    @app.post("/api/projects/{project_id}/prune")
    async def prune(project_id: int, request: Request) -> JSONResponse:
        try:
            project = project_or_404(project_id)
        except LookupError:
            return json_error(404, "not_found")
        payload = await _body(request)
        if payload.get("confirm") is not True:
            return json_error(400, "confirm")
        try:
            keep = int(payload.get("keep"))
        except (TypeError, ValueError):
            return json_error(400, "validation")
        language = lang_of(request)
        new_id = runtime.db.add_deploy(
            project_id=project.id,
            kind="prune",
            update_name="",
            update_sha256="",
            release_name="",
            status="running",
            started_at=utcnow(),
            actor=actor(),
            backup_db=False,
        )
        log = RunLog()
        runtime.remember(new_id, log)

        def worker() -> None:
            result = run_prune(
                project,
                keep=keep,
                home=runtime.home,
                connector=runtime.connector,
                timing=runtime.timing,
                on_event=log.add,
                lang=language,
            )
            runtime.db.finish_deploy(
                new_id,
                status=result.status,
                finished_at=result.finished_at,
                duration_ms=result.duration_ms,
                events=result.events,
                message_key=result.message_key,
            )
            log.finish({"status": result.status, "duration_ms": result.duration_ms})

        threading.Thread(target=worker, name=f"prune-{new_id}", daemon=True).start()
        return JSONResponse({"deploy_id": new_id})

    @app.post("/api/projects/{project_id}/backups")
    def backups(project_id: int) -> JSONResponse:
        try:
            project = project_or_404(project_id)
        except LookupError:
            return json_error(404, "not_found")
        ok, text = run_list_backups(project, home=runtime.home, connector=runtime.connector, timing=runtime.timing)
        if not ok:
            return JSONResponse({"ok": False, "error": mask_text(text)})
        rows = []
        for line in text.splitlines():
            if line == "OK" or not line.strip():
                continue
            parts = line.split(" ", 1)
            if len(parts) == 2 and parts[0].isdigit():
                rows.append({"size": int(parts[0]), "name": parts[1]})
        return JSONResponse({"ok": True, "backups": rows})

    @app.get("/api/projects/{project_id}/history")
    def history(project_id: int) -> JSONResponse:
        try:
            project_or_404(project_id)
        except LookupError:
            return json_error(404, "not_found")
        return JSONResponse({"history": runtime.db.list_deploys(project_id)})

    @app.get("/api/projects/{project_id}/history/{deploy_id}")
    def history_one(project_id: int, deploy_id: int) -> JSONResponse:
        record = runtime.db.get_deploy(project_id, deploy_id)
        if record is None:
            return json_error(404, "not_found")
        return JSONResponse({"deploy": record})

    @app.get("/api/runs/{deploy_id}/events")
    def events(deploy_id: int) -> StreamingResponse:
        log = runtime.runs.get(deploy_id)
        if log is None:
            return json_error(404, "not_found")

        def stream():
            index = 0
            while True:
                chunk, done, payload = log.wait_from(index)
                for event in chunk:
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                    index += 1
                if done:
                    yield f"data: {json.dumps({'event': 'done', **(payload or {})}, ensure_ascii=False)}\n\n"
                    break
                yield ": ping\n\n"

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app


def _csrf_token(request: Request) -> str:
    current = request.cookies.get("butler_csrf", "")
    if _TOKEN_RE.fullmatch(current):
        return current
    return secrets.token_urlsafe(32)


def _set_csrf(response: Response, token: str) -> None:
    response.set_cookie(
        "butler_csrf",
        token,
        max_age=7 * 24 * 3600,
        httponly=False,
        samesite="strict",
        path="/",
        secure=False,
    )


async def _body(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _remember_key(runtime: Runtime, project: Project, key: Any, changed: bool) -> None:
    fingerprint = key.fingerprint
    runtime.pending[(project.id, fingerprint)] = {
        "host": project.ssh_host,
        "port": project.ssh_port,
        "key": key,
        "at": time.time(),
        "changed": changed,
    }


def _inspection_payload(inspection) -> dict[str, Any]:
    return {
        "name": inspection.name,
        "release": inspection.release,
        "update_sha256": inspection.update_sha256,
        "blocked": inspection.blocked,
        "hashes_ok": False,
        "needs_hostkey": False,
        "unreachable": False,
        "files": [
            {
                "path": item.path,
                "sha256": item.sha256,
                "sha256_short": short_hash(item.sha256),
                "size": item.size,
                "state": "unknown",
            }
            for item in inspection.files
        ],
        "blockers": [
            {"code": item.code, "path": item.path, "pattern": item.pattern, "name": item.name}
            for item in inspection.blockers
        ],
    }


def main() -> None:
    port = int(os.environ.get("DEPLOY_BUTLER_PORT", "8787"))
    host = "127.0.0.1"
    app = create_app()
    if os.environ.get("DEPLOY_BUTLER_NO_BROWSER") != "1":
        threading.Timer(0.8, lambda: webbrowser.open(f"http://{host}:{port}/")).start()
    import uvicorn

    uvicorn.run(app, host=host, port=port, log_level="info")
