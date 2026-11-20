from __future__ import annotations

import io
import shutil
import sqlite3
import subprocess
import tarfile
import uuid
from pathlib import Path

import paramiko
import pytest

from deploy_butler.helper_template import render_helper, render_sudoers
from deploy_butler.ssh import CaptureRejectPolicy, HostKeyUnknown, save_host_key
from tests.conftest import make_project


def _render(tmp_path: Path, **overrides) -> tuple[Path, object]:
    key = tmp_path / "key"
    key.write_text("not-a-real-key", encoding="utf-8")
    fields = dict(
        name="p" + uuid.uuid4().hex[:10],
        service_type="none",
        service_name="",
        app_owner="",
        app_dir=str(tmp_path / "app"),
        backups_dir=str(tmp_path / "backups"),
        db_backup_paths=[],
    )
    fields.update(overrides)
    project = make_project(tmp_path, key, **fields)
    script = tmp_path / "helper.sh"
    script.write_text(render_helper(project), encoding="utf-8")
    script.chmod(0o755)
    return script, project


def _run(script: Path, *args: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(script), *args],
        input=stdin,
        text=True,
        capture_output=True,
        check=False,
    )


def test_shellcheck_and_sudoers(tmp_path):
    script, project = _render(tmp_path)
    proc = subprocess.run(
        ["shellcheck", "-s", "bash", str(script)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    text = script.read_text(encoding="utf-8")
    assert "set -euo pipefail" in text
    assert "flock -n 9" in text
    assert "printenv" not in text
    assert "set -x" not in text
    sudoers = render_sudoers(project)
    assert sudoers.strip().endswith(f"{project.ssh_user} ALL=(root) NOPASSWD: {project.helper_path}")
    assert "*" not in sudoers.splitlines()[-1]
    visudo = shutil.which("visudo")
    if visudo:
        path = tmp_path / "sudoers"
        path.write_text(sudoers, encoding="utf-8")
        checked = subprocess.run([visudo, "-cf", str(path)], capture_output=True, text=True, check=False)
        assert checked.returncode == 0, checked.stderr


def test_helper_inplace_roundtrip_and_rejects_traversal(tmp_path):
    script, project = _render(tmp_path)
    app = Path(project.app_dir)
    app.mkdir()
    (app / "hello.txt").write_text("v1\n", encoding="utf-8")
    staging = Path(project.staging_dir)
    staging.mkdir(parents=True)
    try:
        hashed = _run(script, "hashes", stdin="hello.txt\nmissing.txt\n")
        assert hashed.returncode == 0, hashed.stderr
        assert "MISSING\tmissing.txt" in hashed.stdout
        backed = _run(script, "backup", "r1")
        assert backed.returncode == 0, backed.stderr
        assert (Path(project.backups_dir) / "r1.tar.gz").is_file()

        archive = staging / "r1.tar.gz"
        _write_tar(archive, {"hello.txt": b"v2\n"})
        applied = _run(script, "apply", "r1", str(archive))
        assert applied.returncode == 0, applied.stderr
        assert (app / "hello.txt").read_text(encoding="utf-8") == "v2\n"
        assert "hello.txt" in applied.stdout

        rolled = _run(script, "rollback", "r1")
        assert rolled.returncode == 0, rolled.stderr
        assert (app / "hello.txt").read_text(encoding="utf-8") == "v1\n"

        bad = staging / "r2.tar.gz"
        _write_tar(bad, {"../evil.txt": b"nope"})
        _run(script, "backup", "r2")
        rejected = _run(script, "apply", "r2", str(bad))
        assert rejected.returncode != 0
        assert not (tmp_path / "evil.txt").exists()
        assert (app / "hello.txt").read_text(encoding="utf-8") == "v1\n"

        symlink = staging / "r3.tar.gz"
        _write_symlink_tar(symlink)
        _run(script, "backup", "r3")
        linked = _run(script, "apply", "r3", str(symlink))
        assert linked.returncode != 0

        bad_name = _run(script, "backup", "../etc")
        assert bad_name.returncode != 0
        assert "bad release" in bad_name.stderr
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        Path(f"/tmp/deploy-butler-{project.name}.lock").unlink(missing_ok=True)


def test_helper_releases_switch_symlink(tmp_path):
    root = tmp_path / "opt" / "myapp"
    root.mkdir(parents=True)
    script, project = _render(
        tmp_path,
        deploy_mode="releases",
        app_dir=str(root / "current"),
        backups_dir=str(tmp_path / "rel-backups"),
    )
    staging = Path(project.staging_dir)
    staging.mkdir(parents=True)
    try:
        assert _run(script, "backup", "r1").returncode == 0
        first = staging / "r1.tar.gz"
        _write_tar(first, {"hello.txt": b"one\n"})
        assert _run(script, "apply", "r1", str(first)).returncode == 0
        assert (root / "current").is_symlink()
        assert (root / "current" / "hello.txt").read_text(encoding="utf-8") == "one\n"
        assert _run(script, "backup", "r2").returncode == 0
        second = staging / "r2.tar.gz"
        _write_tar(second, {"hello.txt": b"two\n"})
        assert _run(script, "apply", "r2", str(second)).returncode == 0
        assert (root / "current" / "hello.txt").read_text(encoding="utf-8") == "two\n"
        assert _run(script, "rollback", "r2").returncode == 0
        assert (root / "current" / "hello.txt").read_text(encoding="utf-8") == "one\n"
        assert (root / "releases" / "r2").is_dir()
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        Path(f"/tmp/deploy-butler-{project.name}.lock").unlink(missing_ok=True)


def test_helper_sqlite_backup_roundtrip(tmp_path):
    database = tmp_path / "data" / "app.db"
    database.parent.mkdir()
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute("INSERT INTO t VALUES (1)")
    script, project = _render(tmp_path, db_backup_paths=[str(database)])
    app = Path(project.app_dir)
    app.mkdir()
    (app / "hello.txt").write_text("v1\n", encoding="utf-8")
    assert _run(script, "backup", "r1", "--db").returncode == 0
    with sqlite3.connect(database) as conn:
        conn.execute("UPDATE t SET x = 2")
    assert _run(script, "rollback", "r1", "--db").returncode == 0
    with sqlite3.connect(database) as conn:
        value = conn.execute("SELECT x FROM t").fetchone()[0]
    assert value == 1
    Path(f"/tmp/deploy-butler-{project.name}.lock").unlink(missing_ok=True)


def test_host_key_is_rejected_until_saved(tmp_path):
    key = paramiko.ECDSAKey.generate()
    client = paramiko.SSHClient()
    with pytest.raises(HostKeyUnknown) as caught:
        CaptureRejectPolicy().missing_host_key(client, "203.0.113.10", key)
    assert caught.value.fingerprint == key.fingerprint
    assert client.get_host_keys().lookup("203.0.113.10") is None
    path = tmp_path / "known_hosts"
    save_host_key(path, "203.0.113.10", 22, key)
    loaded = paramiko.HostKeys(str(path))
    assert loaded.lookup("203.0.113.10") is not None
    source = Path("deploy_butler/ssh.py").read_text(encoding="utf-8")
    assert "AutoAddPolicy" not in source
    assert "WarningPolicy" not in source


def _write_tar(path: Path, files: dict[str, bytes]) -> None:
    with tarfile.open(path, "w:gz") as tar:
        for name, payload in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))


def _write_symlink_tar(path: Path) -> None:
    with tarfile.open(path, "w:gz") as tar:
        info = tarfile.TarInfo("link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tar.addfile(info)
