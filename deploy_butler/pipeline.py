"""Deploy, rollback and prune. The remote side only sees the helper."""

from __future__ import annotations

import http.client
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from deploy_butler.db import Project, utcnow
from deploy_butler.i18n import translate
from deploy_butler.locking import FileLock
from deploy_butler.scanner import (
    Inspection,
    assert_tar_matches,
    build_normalized_tar,
    sha256_file,
)
from deploy_butler.security import mask_text
from deploy_butler.ssh import (
    HostKeyChanged,
    HostKeyUnknown,
    KeyEncrypted,
    KeyNotFound,
    KeyUnreadable,
    Unreachable,
    load_private_key,
)
from deploy_butler.validate import readiness_errors, release_name_ok

SUCCESS = "success"
ROLLED_BACK = "rolled_back"
ERROR = "error"
ERROR_BEFORE = "error_before_changes"

Connector = Callable[[Project, float], Any]
HealthFn = Callable[[str, float, list[int], str], tuple[bool, str]]


@dataclass
class Timing:
    ssh_timeout: float = 10.0
    log_window_s: int = 20
    health_timeout: float = 10.0
    health_attempts: int = 3
    health_interval: float = 5.0
    post_active_wait: float = 15.0


@dataclass
class DeployOutcome:
    status: str
    release: str
    update_name: str
    update_sha256: str
    backup_db: bool
    started_at: str
    finished_at: str
    duration_ms: int
    events: list[dict[str, Any]] = field(default_factory=list)
    message_key: str = ""


@dataclass
class CheckResult:
    ok: bool
    kind: str
    detail: str = ""


class _Stop(Exception):
    def __init__(self, status: str):
        self.status = status


def find_pattern(text: str, patterns: list[str]) -> str | None:
    ordered = sorted((item for item in patterns if item), key=len, reverse=True)
    for pattern in ordered:
        if re.search(r"(?<![A-Za-z0-9_])" + re.escape(pattern), text):
            return pattern
    return None


def is_active(stdout: str) -> bool:
    return any(line.strip() == "active" for line in stdout.splitlines())


def parse_hash_map(stdout: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for line in stdout.splitlines():
        if "\t" not in line:
            continue
        left, right = line.split("\t", 1)
        path = right.strip()
        token = left.strip().lower()
        if path:
            found[path] = token
    return found


def hashes_match(stdout: str, inspection: Inspection) -> tuple[bool, str]:
    found = parse_hash_map(stdout)
    expected = {item.path: item.sha256.lower() for item in inspection.files}
    if set(found) != set(expected):
        missing = sorted(set(expected) ^ set(found))
        return False, missing[0] if missing else ""
    for path, digest in expected.items():
        if found[path] != digest:
            return False, path
    return True, ""


def check_health(url: str, timeout: float, codes: list[int], expected_text: str) -> tuple[bool, str]:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False, "bad url"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path = path + "?" + parsed.query
    connection: http.client.HTTPConnection | None = None
    try:
        if parsed.scheme == "https":
            connection = http.client.HTTPSConnection(parsed.hostname, port, timeout=timeout)
        else:
            connection = http.client.HTTPConnection(parsed.hostname, port, timeout=timeout)
        connection.request("GET", path, headers={"User-Agent": "DeployButler/1.0", "Accept": "*/*"})
        response = connection.getresponse()
        body = response.read(65536)
        code = response.status
    except Exception as exc:
        return False, mask_text(exc.__class__.__name__)
    finally:
        if connection is not None:
            connection.close()
    if code not in set(codes):
        return False, f"HTTP {code}"
    text = body.decode("utf-8", "replace")
    if expected_text and expected_text not in text:
        return False, "missing expected text"
    return True, f"HTTP {code}"


def _lock_file(home: Path, project: Project) -> Path:
    token = str(project.id) if project.id else project.name
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", token) or "project"
    return home / "locks" / f"{safe}.lock"


def _helper_args(backup_db: bool, release: str, command: str) -> list[str]:
    args = [command, release]
    if backup_db:
        args.append("--db")
    return args


def run_deploy(
    project: Project,
    inspection: Inspection,
    *,
    backup_db: bool,
    home: Path,
    connector: Connector,
    health: HealthFn | None = None,
    timing: Timing | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    gate: Callable[[], None] | None = None,
    lang: str = "ru",
) -> DeployOutcome:
    timing = timing or Timing()
    health_fn = health or check_health
    if backup_db and not project.db_backup_paths:
        backup_db = False
    started_at = utcnow()
    started = time.monotonic()
    events: list[dict[str, Any]] = []

    def emit(step: str, level: str, key: str, **params: object) -> None:
        event = {
            "step": step,
            "level": level,
            "key": key,
            "params": {name: mask_text(str(value)) for name, value in params.items()},
            "ts": utcnow(),
        }
        events.append(event)
        if on_event:
            on_event(event)

    def outcome(status: str) -> DeployOutcome:
        label = translate(lang, f"status.{status}")
        emit("done", "info" if status == SUCCESS else "error", "log.done", status=label)
        finished = utcnow()
        return DeployOutcome(
            status=status,
            release=inspection.release,
            update_name=inspection.name,
            update_sha256=inspection.update_sha256,
            backup_db=backup_db,
            started_at=started_at,
            finished_at=finished,
            duration_ms=int((time.monotonic() - started) * 1000),
            events=list(events),
            message_key=status,
        )

    def stop(status: str) -> None:
        raise _Stop(status)

    emit("validate", "info", "log.validate")
    if readiness_errors(project.public_dict()):
        emit("validate", "error", "log.not_ready")
        return outcome(ERROR_BEFORE)
    if not release_name_ok(inspection.release):
        emit("validate", "error", "log.bad_release")
        return outcome(ERROR_BEFORE)
    if inspection.blocked:
        emit("validate", "error", "log.blockers")
        return outcome(ERROR_BEFORE)
    try:
        load_private_key(project.ssh_key_path)
    except KeyNotFound:
        emit("validate", "error", "log.key_missing")
        return outcome(ERROR_BEFORE)
    except KeyEncrypted:
        emit("validate", "error", "log.key_encrypted")
        return outcome(ERROR_BEFORE)
    except KeyUnreadable:
        emit("validate", "error", "log.key_unreadable")
        return outcome(ERROR_BEFORE)

    if os.name == "posix":
        mode = Path(project.ssh_key_path).expanduser().stat().st_mode
        if mode & 0o077:
            emit("validate", "warn", "log.perms")

    lock = FileLock(_lock_file(home, project))
    if not lock.acquire():
        emit("validate", "error", "log.busy")
        return outcome(ERROR_BEFORE)

    remote = None
    uploaded: str | None = None
    applied = False
    status = ERROR_BEFORE

    def rollback_and_recheck() -> bool:
        emit("rollback", "warn", "log.rollback", release=inspection.release)
        try:
            result = remote.helper(_helper_args(backup_db, inspection.release, "rollback"))
        except Exception as exc:
            emit("rollback", "error", "log.rollback_fail", detail=mask_text(str(exc)))
            return False
        if result.exit_code != 0:
            emit("rollback", "error", "log.rollback_fail", detail=mask_text(result.stderr.strip()))
            return False
        emit("rollback", "info", "log.rollback_ok")
        emit("restart", "info", "log.restart")
        try:
            restarted = remote.helper(["restart"])
        except Exception as exc:
            emit("restart", "error", "log.restart_fail", detail=mask_text(str(exc)))
            restarted = None
        if restarted is None or restarted.exit_code != 0 or not is_active(restarted.stdout):
            emit("restart", "error", "log.restart_fail")
        emit("checks", "info", "log.recheck")
        checked = _checks()
        if checked.ok:
            emit("checks", "info", "log.recheck_ok")
        else:
            emit("checks", "warn", "log.recheck_bad", detail=checked.detail or checked.kind)
        return True

    def _checks() -> CheckResult:
        try:
            logs = remote.helper(["logs", str(int(timing.log_window_s))])
        except Exception as exc:
            return CheckResult(False, "logs", mask_text(str(exc)))
        if logs.exit_code != 0:
            return CheckResult(False, "logs", mask_text(logs.stderr.strip()))
        pattern = find_pattern(logs.stdout, project.log_error_patterns)
        if pattern:
            return CheckResult(False, "pattern", pattern)
        if project.healthcheck_url:
            detail = ""
            ok = False
            for attempt in range(1, timing.health_attempts + 1):
                if attempt > 1 and timing.health_interval > 0:
                    time.sleep(timing.health_interval)
                ok, detail = health_fn(
                    project.healthcheck_url,
                    timing.health_timeout,
                    list(project.expected_codes),
                    project.expected_text,
                )
                emit(
                    "checks",
                    "info" if ok else "warn",
                    "log.health_ok" if ok else "log.health_try",
                    n=attempt,
                    detail=detail,
                )
                if ok:
                    break
            if not ok:
                return CheckResult(False, "health", detail)
        if timing.post_active_wait > 0:
            emit("checks", "info", "log.wait_active", seconds=int(timing.post_active_wait))
            time.sleep(timing.post_active_wait)
        try:
            state = remote.helper(["status"])
        except Exception as exc:
            return CheckResult(False, "inactive", mask_text(str(exc)))
        if state.exit_code != 0 or not is_active(state.stdout):
            return CheckResult(False, "inactive", state.stdout.strip())
        return CheckResult(True, "ok")

    try:
        if gate is not None:
            gate()
        emit("connect", "info", "log.connect", timeout=int(timing.ssh_timeout))
        remote = connector(project, timing.ssh_timeout)
        with tempfile.TemporaryDirectory(prefix="deploy-butler-") as tmp:
            archive_path = Path(tmp) / f"{inspection.release}.tar.gz"
            emit("pack", "info", "log.pack", sha=inspection.update_sha256)
            build_normalized_tar(inspection, archive_path)
            assert_tar_matches(archive_path, inspection.files)
            remote_name = f"{inspection.release}.tar.gz"
            remote_path = remote.upload(archive_path, remote_name)
            uploaded = remote_name
            emit("upload", "info", "log.upload", path=remote_path)
            remote_sha = remote.sha256(remote_name)
            local_digest = sha256_file(archive_path)
            if remote_sha != local_digest:
                emit("upload", "error", "log.sha_mismatch")
                stop(ERROR_BEFORE)
            emit("upload", "info", "log.sha_ok")

        emit("backup", "info", "log.backup", release=inspection.release)
        backed = remote.helper(_helper_args(backup_db, inspection.release, "backup"))
        if backed.exit_code != 0:
            emit("backup", "error", "log.backup_fail", detail=mask_text(backed.stderr.strip()))
            stop(ERROR_BEFORE)
        emit("backup", "info", "log.backup_ok")

        emit("apply", "info", "log.apply", release=inspection.release)
        applied = True
        applied_result = remote.helper(
            ["apply", inspection.release, f"{project.staging_dir}/{inspection.release}.tar.gz"]
        )
        if applied_result.exit_code != 0:
            emit("apply", "error", "log.apply_fail", detail=mask_text(applied_result.stderr.strip()))
            if not rollback_and_recheck():
                stop(ERROR)
            stop(ROLLED_BACK)
        ok_hash, bad_path = hashes_match(applied_result.stdout, inspection)
        if not ok_hash:
            emit("apply", "error", "log.hash_mismatch", path=bad_path)
            if not rollback_and_recheck():
                stop(ERROR)
            stop(ROLLED_BACK)

        emit("restart", "info", "log.restart")
        restarted = remote.helper(["restart"])
        if restarted.exit_code != 0 or not is_active(restarted.stdout):
            emit("restart", "error", "log.restart_fail")
            if not rollback_and_recheck():
                stop(ERROR)
            stop(ROLLED_BACK)

        emit("checks", "info", "log.checks")
        checked = _checks()
        if not checked.ok:
            if checked.kind == "pattern":
                emit("checks", "error", "log.pattern", pattern=checked.detail)
            elif checked.kind == "health":
                emit("checks", "error", "log.health_bad", detail=checked.detail)
            elif checked.kind == "inactive":
                emit("checks", "error", "log.inactive")
            else:
                emit("checks", "error", "log.health_bad", detail=checked.detail or checked.kind)
            if not rollback_and_recheck():
                stop(ERROR)
            stop(ROLLED_BACK)
        status = SUCCESS
    except _Stop as stopped:
        status = stopped.status
    except Unreachable:
        emit("connect", "error", "log.unreachable", hint=project.unreachable_hint)
        status = ERROR_BEFORE
    except HostKeyUnknown as exc:
        emit("connect", "error", "log.hostkey_unknown", fingerprint=exc.fingerprint)
        status = ERROR_BEFORE
    except HostKeyChanged as exc:
        emit("connect", "error", "log.hostkey_changed", fingerprint=exc.fingerprint)
        status = ERROR_BEFORE
    except Exception as exc:
        emit("done", "error", "log.unexpected", detail=mask_text(str(exc)))
        if applied and remote is not None:
            status = ROLLED_BACK if rollback_and_recheck() else ERROR
        else:
            status = ERROR_BEFORE
    finally:
        if remote is not None and uploaded:
            try:
                remote.remove(uploaded)
                emit("cleanup", "info", "log.cleanup")
            except Exception:
                emit("cleanup", "warn", "log.cleanup_fail")
        if remote is not None:
            try:
                remote.close()
            except Exception:
                pass
        lock.release()
    return outcome(status)


def run_manual_rollback(
    project: Project,
    *,
    release: str,
    update_name: str,
    update_sha256: str,
    backup_db: bool,
    home: Path,
    connector: Connector,
    health: HealthFn | None = None,
    timing: Timing | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    lang: str = "ru",
) -> DeployOutcome:
    timing = timing or Timing()
    health_fn = health or check_health
    started_at = utcnow()
    started = time.monotonic()
    events: list[dict[str, Any]] = []

    def emit(step: str, level: str, key: str, **params: object) -> None:
        event = {
            "step": step,
            "level": level,
            "key": key,
            "params": {name: mask_text(str(value)) for name, value in params.items()},
            "ts": utcnow(),
        }
        events.append(event)
        if on_event:
            on_event(event)

    def outcome(status: str) -> DeployOutcome:
        label = translate(lang, f"status.{status}")
        emit("done", "info" if status == SUCCESS else "error", "log.done", status=label)
        return DeployOutcome(
            status=status,
            release=release,
            update_name=update_name,
            update_sha256=update_sha256,
            backup_db=backup_db,
            started_at=started_at,
            finished_at=utcnow(),
            duration_ms=int((time.monotonic() - started) * 1000),
            events=list(events),
            message_key=status,
        )

    emit("rollback", "info", "log.manual_rollback", release=release)
    if readiness_errors(project.public_dict()) or not release_name_ok(release):
        emit("validate", "error", "log.not_ready")
        return outcome(ERROR_BEFORE)
    try:
        load_private_key(project.ssh_key_path)
    except KeyNotFound:
        emit("validate", "error", "log.key_missing")
        return outcome(ERROR_BEFORE)
    except KeyEncrypted:
        emit("validate", "error", "log.key_encrypted")
        return outcome(ERROR_BEFORE)
    except KeyUnreadable:
        emit("validate", "error", "log.key_unreadable")
        return outcome(ERROR_BEFORE)

    lock = FileLock(_lock_file(home, project))
    if not lock.acquire():
        emit("validate", "error", "log.busy")
        return outcome(ERROR_BEFORE)
    remote = None
    status = ERROR
    try:
        emit("connect", "info", "log.connect", timeout=int(timing.ssh_timeout))
        remote = connector(project, timing.ssh_timeout)
        emit("rollback", "warn", "log.rollback", release=release)
        result = remote.helper(_helper_args(backup_db, release, "rollback"))
        if result.exit_code != 0:
            emit("rollback", "error", "log.rollback_fail", detail=mask_text(result.stderr.strip()))
            status = ERROR
        else:
            emit("rollback", "info", "log.rollback_ok")
            emit("restart", "info", "log.restart")
            restarted = remote.helper(["restart"])
            if restarted.exit_code != 0 or not is_active(restarted.stdout):
                emit("restart", "error", "log.restart_fail")
                status = ERROR
            else:
                emit("checks", "info", "log.checks")
                logs = remote.helper(["logs", str(int(timing.log_window_s))])
                pattern = find_pattern(logs.stdout, project.log_error_patterns) if logs.exit_code == 0 else "logs"
                detail = pattern or ""
                healthy = True
                if project.healthcheck_url and not pattern:
                    for attempt in range(1, timing.health_attempts + 1):
                        if attempt > 1 and timing.health_interval > 0:
                            time.sleep(timing.health_interval)
                        healthy, detail = health_fn(
                            project.healthcheck_url,
                            timing.health_timeout,
                            list(project.expected_codes),
                            project.expected_text,
                        )
                        emit("checks", "info" if healthy else "warn", "log.health_try", n=attempt, detail=detail)
                        if healthy:
                            break
                if pattern or logs.exit_code != 0 or not healthy:
                    emit("checks", "error", "log.manual_check_fail", detail=detail or "checks")
                    status = ERROR
                else:
                    if timing.post_active_wait > 0:
                        emit("checks", "info", "log.wait_active", seconds=int(timing.post_active_wait))
                        time.sleep(timing.post_active_wait)
                    state = remote.helper(["status"])
                    if state.exit_code != 0 or not is_active(state.stdout):
                        emit("checks", "error", "log.manual_check_fail", detail="inactive")
                        status = ERROR
                    else:
                        status = SUCCESS
    except Unreachable:
        emit("connect", "error", "log.unreachable", hint=project.unreachable_hint)
        status = ERROR_BEFORE
    except HostKeyUnknown as exc:
        emit("connect", "error", "log.hostkey_unknown", fingerprint=exc.fingerprint)
        status = ERROR_BEFORE
    except HostKeyChanged as exc:
        emit("connect", "error", "log.hostkey_changed", fingerprint=exc.fingerprint)
        status = ERROR_BEFORE
    except Exception as exc:
        emit("done", "error", "log.unexpected", detail=mask_text(str(exc)))
        status = ERROR
    finally:
        if remote is not None:
            try:
                remote.close()
            except Exception:
                pass
        lock.release()
    return outcome(status)


def run_prune(
    project: Project,
    *,
    keep: int,
    home: Path,
    connector: Connector,
    timing: Timing | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    lang: str = "ru",
) -> DeployOutcome:
    timing = timing or Timing()
    started_at = utcnow()
    started = time.monotonic()
    events: list[dict[str, Any]] = []

    def emit(step: str, level: str, key: str, **params: object) -> None:
        event = {
            "step": step,
            "level": level,
            "key": key,
            "params": {name: mask_text(str(value)) for name, value in params.items()},
            "ts": utcnow(),
        }
        events.append(event)
        if on_event:
            on_event(event)

    def outcome(status: str) -> DeployOutcome:
        label = translate(lang, f"status.{status}")
        emit("done", "info" if status == SUCCESS else "error", "log.done", status=label)
        return DeployOutcome(
            status=status,
            release="",
            update_name="",
            update_sha256="",
            backup_db=False,
            started_at=started_at,
            finished_at=utcnow(),
            duration_ms=int((time.monotonic() - started) * 1000),
            events=list(events),
            message_key=status,
        )

    if keep < 0 or keep > 1000:
        emit("cleanup", "error", "log.not_ready")
        return outcome(ERROR_BEFORE)
    emit("cleanup", "info", "log.prune", keep=keep)
    if readiness_errors(project.public_dict()):
        emit("validate", "error", "log.not_ready")
        return outcome(ERROR_BEFORE)
    try:
        load_private_key(project.ssh_key_path)
    except (KeyNotFound, KeyEncrypted, KeyUnreadable):
        emit("validate", "error", "log.key_missing")
        return outcome(ERROR_BEFORE)
    lock = FileLock(_lock_file(home, project))
    if not lock.acquire():
        emit("validate", "error", "log.busy")
        return outcome(ERROR_BEFORE)
    remote = None
    status = ERROR
    try:
        remote = connector(project, timing.ssh_timeout)
        result = remote.helper(["prune", str(int(keep))])
        if result.exit_code != 0:
            emit("cleanup", "error", "log.rollback_fail", detail=mask_text(result.stderr.strip()))
            status = ERROR
        else:
            emit("cleanup", "info", "log.prune_ok")
            status = SUCCESS
    except Unreachable:
        emit("connect", "error", "log.unreachable", hint=project.unreachable_hint)
        status = ERROR_BEFORE
    except (HostKeyUnknown, HostKeyChanged) as exc:
        emit("connect", "error", "log.hostkey_unknown", fingerprint=exc.fingerprint)
        status = ERROR_BEFORE
    except Exception as exc:
        emit("done", "error", "log.unexpected", detail=mask_text(str(exc)))
        status = ERROR
    finally:
        if remote is not None:
            try:
                remote.close()
            except Exception:
                pass
        lock.release()
    return outcome(status)


def run_list_backups(
    project: Project,
    *,
    home: Path,
    connector: Connector,
    timing: Timing | None = None,
) -> tuple[bool, str]:
    """Return (ok, stdout or error key). Holds the project lock so it cannot race a deploy."""
    timing = timing or Timing()
    if readiness_errors(project.public_dict()):
        return False, "log.not_ready"
    try:
        load_private_key(project.ssh_key_path)
    except (KeyNotFound, KeyEncrypted, KeyUnreadable):
        return False, "log.key_missing"
    lock = FileLock(_lock_file(home, project))
    if not lock.acquire():
        return False, "log.busy"
    remote = None
    try:
        remote = connector(project, timing.ssh_timeout)
        result = remote.helper(["list-backups"])
        if result.exit_code != 0:
            return False, mask_text(result.stderr.strip()) or "log.unexpected"
        return True, result.stdout
    except Unreachable:
        return False, project.unreachable_hint
    except (HostKeyUnknown, HostKeyChanged) as exc:
        return False, exc.fingerprint
    except Exception as exc:
        return False, mask_text(str(exc))
    finally:
        if remote is not None:
            try:
                remote.close()
            except Exception:
                pass
        lock.release()
