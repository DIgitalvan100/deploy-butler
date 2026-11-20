from __future__ import annotations

from pathlib import Path

import paramiko
import pytest

from deploy_butler.db import Project
from deploy_butler.defaults import DEFAULT_FORBIDDEN, DEFAULT_HINT, DEFAULT_LOG_PATTERNS
from deploy_butler.pipeline import Timing

FAST = Timing(
    ssh_timeout=1,
    log_window_s=0,
    health_timeout=1,
    health_attempts=3,
    health_interval=0,
    post_active_wait=0,
)


@pytest.fixture
def key_path(tmp_path: Path) -> Path:
    path = tmp_path / "id_ed25519"
    paramiko.ECDSAKey.generate().write_private_key_file(str(path))
    path.chmod(0o600)
    return path


def make_project(tmp_path: Path, key_path: Path, **overrides) -> Project:
    updates = tmp_path / "Updates"
    updates.mkdir(exist_ok=True)
    values = dict(
        id=1,
        name="myapp",
        updates_dir=str(updates),
        update_format="folder",
        ssh_host="203.0.113.10",
        ssh_port=22,
        ssh_user="deploy",
        ssh_key_path=str(key_path),
        app_dir="/opt/myapp/app",
        service_type="systemd",
        service_name="myapp",
        app_owner="myapp:myapp",
        backups_dir="/var/backups/deploy-butler/myapp",
        db_backup_paths=[],
        healthcheck_url="http://127.0.0.1:9/health",
        expected_codes=[200, 301, 302],
        expected_text="",
        log_error_patterns=list(DEFAULT_LOG_PATTERNS),
        forbidden_patterns=list(DEFAULT_FORBIDDEN),
        deploy_mode="inplace",
        unreachable_hint=DEFAULT_HINT,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )
    values.update(overrides)
    return Project(**values)
