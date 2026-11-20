"""SQLite storage for projects, deploy history and nothing secret."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from deploy_butler.validate import apply_defaults


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Project:
    id: int
    name: str
    updates_dir: str
    update_format: str
    ssh_host: str
    ssh_port: int
    ssh_user: str
    ssh_key_path: str
    app_dir: str
    service_type: str
    service_name: str
    app_owner: str
    backups_dir: str
    db_backup_paths: list[str]
    healthcheck_url: str
    expected_codes: list[int]
    expected_text: str
    log_error_patterns: list[str]
    forbidden_patterns: list[str]
    deploy_mode: str
    unreachable_hint: str
    created_at: str
    updated_at: str

    @property
    def helper_path(self) -> str:
        return f"/usr/local/sbin/deploy-butler-{self.name}"

    @property
    def sudoers_path(self) -> str:
        return f"/etc/sudoers.d/deploy-butler-{self.name}"

    @property
    def staging_dir(self) -> str:
        return f"/tmp/deploy-butler/{self.name}"

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "updates_dir": self.updates_dir,
            "update_format": self.update_format,
            "ssh_host": self.ssh_host,
            "ssh_port": self.ssh_port,
            "ssh_user": self.ssh_user,
            "ssh_key_path": self.ssh_key_path,
            "app_dir": self.app_dir,
            "service_type": self.service_type,
            "service_name": self.service_name,
            "app_owner": self.app_owner,
            "backups_dir": self.backups_dir,
            "db_backup_paths": list(self.db_backup_paths),
            "healthcheck_url": self.healthcheck_url,
            "expected_codes": list(self.expected_codes),
            "expected_text": self.expected_text,
            "log_error_patterns": list(self.log_error_patterns),
            "forbidden_patterns": list(self.forbidden_patterns),
            "deploy_mode": self.deploy_mode,
            "unreachable_hint": self.unreachable_hint,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


_COLUMNS = (
    "name",
    "updates_dir",
    "update_format",
    "ssh_host",
    "ssh_port",
    "ssh_user",
    "ssh_key_path",
    "app_dir",
    "service_type",
    "service_name",
    "app_owner",
    "backups_dir",
    "db_backup_paths",
    "healthcheck_url",
    "expected_codes",
    "expected_text",
    "log_error_patterns",
    "forbidden_patterns",
    "deploy_mode",
    "unreachable_hint",
    "created_at",
    "updated_at",
)


def _row_to_project(row: sqlite3.Row) -> Project:
    return Project(
        id=row["id"],
        name=row["name"],
        updates_dir=row["updates_dir"],
        update_format=row["update_format"],
        ssh_host=row["ssh_host"],
        ssh_port=row["ssh_port"],
        ssh_user=row["ssh_user"],
        ssh_key_path=row["ssh_key_path"],
        app_dir=row["app_dir"],
        service_type=row["service_type"],
        service_name=row["service_name"],
        app_owner=row["app_owner"],
        backups_dir=row["backups_dir"],
        db_backup_paths=json.loads(row["db_backup_paths"]),
        healthcheck_url=row["healthcheck_url"],
        expected_codes=json.loads(row["expected_codes"]),
        expected_text=row["expected_text"],
        log_error_patterns=json.loads(row["log_error_patterns"]),
        forbidden_patterns=json.loads(row["forbidden_patterns"]),
        deploy_mode=row["deploy_mode"],
        unreachable_hint=row["unreachable_hint"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _pack(values: dict[str, Any], now: str, created: str | None = None) -> dict[str, Any]:
    data = apply_defaults(values)
    return {
        "name": data["name"],
        "updates_dir": data["updates_dir"],
        "update_format": data["update_format"],
        "ssh_host": data["ssh_host"],
        "ssh_port": data["ssh_port"],
        "ssh_user": data["ssh_user"],
        "ssh_key_path": data["ssh_key_path"],
        "app_dir": data["app_dir"],
        "service_type": data["service_type"],
        "service_name": data["service_name"],
        "app_owner": data["app_owner"],
        "backups_dir": data["backups_dir"],
        "db_backup_paths": json.dumps(data["db_backup_paths"], ensure_ascii=False),
        "healthcheck_url": data["healthcheck_url"],
        "expected_codes": json.dumps(data["expected_codes"]),
        "expected_text": data["expected_text"],
        "log_error_patterns": json.dumps(data["log_error_patterns"], ensure_ascii=False),
        "forbidden_patterns": json.dumps(data["forbidden_patterns"], ensure_ascii=False),
        "deploy_mode": data["deploy_mode"],
        "unreachable_hint": data["unreachable_hint"],
        "created_at": created or now,
        "updated_at": now,
    }


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def _migrate(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                updates_dir TEXT NOT NULL DEFAULT '',
                update_format TEXT NOT NULL,
                ssh_host TEXT NOT NULL DEFAULT '',
                ssh_port INTEGER NOT NULL DEFAULT 22,
                ssh_user TEXT NOT NULL DEFAULT '',
                ssh_key_path TEXT NOT NULL DEFAULT '',
                app_dir TEXT NOT NULL DEFAULT '',
                service_type TEXT NOT NULL,
                service_name TEXT NOT NULL DEFAULT '',
                app_owner TEXT NOT NULL DEFAULT '',
                backups_dir TEXT NOT NULL DEFAULT '',
                db_backup_paths TEXT NOT NULL DEFAULT '[]',
                healthcheck_url TEXT NOT NULL DEFAULT '',
                expected_codes TEXT NOT NULL DEFAULT '[200,301,302]',
                expected_text TEXT NOT NULL DEFAULT '',
                log_error_patterns TEXT NOT NULL DEFAULT '[]',
                forbidden_patterns TEXT NOT NULL DEFAULT '[]',
                deploy_mode TEXT NOT NULL,
                unreachable_hint TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS deploys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                kind TEXT NOT NULL,
                update_name TEXT NOT NULL DEFAULT '',
                update_sha256 TEXT NOT NULL DEFAULT '',
                release_name TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL DEFAULT '',
                duration_ms INTEGER NOT NULL DEFAULT 0,
                actor TEXT NOT NULL DEFAULT '',
                backup_db INTEGER NOT NULL DEFAULT 0,
                log TEXT NOT NULL DEFAULT '[]',
                message_key TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_deploys_project ON deploys(project_id, id);
            """
        )
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def list_projects(self) -> list[Project]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM projects ORDER BY name COLLATE NOCASE").fetchall()
        return [_row_to_project(row) for row in rows]

    def get_project(self, project_id: int) -> Project | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return _row_to_project(row) if row else None

    def get_project_by_name(self, name: str) -> Project | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM projects WHERE name = ?", (name,)).fetchone()
        return _row_to_project(row) if row else None

    def create_project(self, values: dict[str, Any]) -> Project:
        now = utcnow()
        packed = _pack(values, now)
        columns = ", ".join(_COLUMNS)
        marks = ", ".join("?" for _ in _COLUMNS)
        with self._lock:
            cur = self._conn.execute(
                f"INSERT INTO projects ({columns}) VALUES ({marks})",
                [packed[column] for column in _COLUMNS],
            )
            self._conn.commit()
            project_id = int(cur.lastrowid)
        project = self.get_project(project_id)
        if project is None:
            raise RuntimeError("project insert failed")
        return project

    def update_project(self, project_id: int, values: dict[str, Any]) -> Project | None:
        current = self.get_project(project_id)
        if current is None:
            return None
        now = utcnow()
        packed = _pack(values, now, created=current.created_at)
        assignments = ", ".join(f"{column} = ?" for column in _COLUMNS)
        with self._lock:
            self._conn.execute(
                f"UPDATE projects SET {assignments} WHERE id = ?",
                [packed[column] for column in _COLUMNS] + [project_id],
            )
            self._conn.commit()
        return self.get_project(project_id)

    def delete_project(self, project_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def add_deploy(
        self,
        *,
        project_id: int,
        kind: str,
        update_name: str,
        update_sha256: str,
        release_name: str,
        status: str,
        started_at: str,
        actor: str,
        backup_db: bool,
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO deploys (
                    project_id, kind, update_name, update_sha256, release_name,
                    status, started_at, actor, backup_db, log
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '[]')
                """,
                (
                    project_id,
                    kind,
                    update_name,
                    update_sha256,
                    release_name,
                    status,
                    started_at,
                    actor,
                    1 if backup_db else 0,
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def finish_deploy(
        self,
        deploy_id: int,
        *,
        status: str,
        finished_at: str,
        duration_ms: int,
        events: list[dict[str, Any]],
        message_key: str,
        update_sha256: str | None = None,
    ) -> None:
        with self._lock:
            if update_sha256 is None:
                self._conn.execute(
                    """
                    UPDATE deploys
                    SET status = ?, finished_at = ?, duration_ms = ?, log = ?, message_key = ?
                    WHERE id = ?
                    """,
                    (status, finished_at, duration_ms, json.dumps(events, ensure_ascii=False), message_key, deploy_id),
                )
            else:
                self._conn.execute(
                    """
                    UPDATE deploys
                    SET status = ?, finished_at = ?, duration_ms = ?, log = ?, message_key = ?,
                        update_sha256 = ?
                    WHERE id = ?
                    """,
                    (
                        status,
                        finished_at,
                        duration_ms,
                        json.dumps(events, ensure_ascii=False),
                        message_key,
                        update_sha256,
                        deploy_id,
                    ),
                )
            self._conn.commit()

    def list_deploys(self, project_id: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, project_id, kind, update_name, update_sha256, release_name,
                       status, started_at, finished_at, duration_ms, actor, backup_db, message_key
                FROM deploys WHERE project_id = ? ORDER BY id DESC
                """,
                (project_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_deploy(self, project_id: int, deploy_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM deploys WHERE project_id = ? AND id = ?",
                (project_id, deploy_id),
            ).fetchone()
        if row is None:
            return None
        data = dict(row)
        try:
            data["log"] = json.loads(data["log"])
        except json.JSONDecodeError:
            data["log"] = []
        return data

    def latest_deployed_sha(self, project_id: int) -> str | None:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT kind, status, update_sha256 FROM deploys
                WHERE project_id = ? ORDER BY id DESC
                """,
                (project_id,),
            ).fetchall()
        for row in rows:
            if row["status"] != "success":
                continue
            if row["kind"] == "deploy" and row["update_sha256"]:
                return row["update_sha256"]
            if row["kind"] == "rollback":
                return None
        return None
