"""Project field checks. Server paths are Linux paths, even on Windows."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from deploy_butler.defaults import (
    DEFAULT_CODES,
    DEFAULT_FORBIDDEN,
    DEFAULT_HINT,
    DEFAULT_LOG_PATTERNS,
    PROTECTED_DIRS,
    RELEASE_RE_SRC,
)

RELEASE_RE = re.compile(RELEASE_RE_SRC)
USER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,31}$")
OWNER_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_-]{0,31}(?::[A-Za-z_][A-Za-z0-9_-]{0,31})?$"
)
SERVICE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._@-]{0,63}$")
SECRET_MARKERS = ("PRIVATE KEY", "BEGIN OPENSSH", "BEGIN RSA", "BEGIN EC PRIVATE")

READY_FIELDS = (
    "name",
    "updates_dir",
    "ssh_host",
    "ssh_user",
    "ssh_key_path",
    "app_dir",
    "backups_dir",
)


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def as_str_list(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        parts = re.split(r"[\n,]", value)
        return [part.strip() for part in parts if part.strip()]
    if isinstance(value, (list, tuple)):
        return [str(part).strip() for part in value if str(part).strip()]
    raise ValueError("list")


def as_int_list(value: Any) -> list[int]:
    raw = as_str_list(value) if not isinstance(value, (list, tuple)) else value
    if isinstance(value, str) or value is None or value == "":
        raw = as_str_list(value)
    numbers: list[int] = []
    for item in raw:
        numbers.append(int(str(item).strip()))
    return numbers


def looks_like_secret(value: str) -> bool:
    upper = value.upper()
    return any(marker in upper for marker in SECRET_MARKERS)


def linux_dir(path: str) -> bool:
    if not path.startswith("/"):
        return False
    if any(char in path for char in "\n\r\x00'`\"\\"):
        return False
    trimmed = path.rstrip("/")
    if trimmed in PROTECTED_DIRS or trimmed == "":
        return False
    parts = trimmed.split("/")
    if any(part in {"", ".", ".."} for part in parts[1:]):
        return False
    if len(parts) - 1 < 2:
        return False
    return True


def _clean_linux_dir(path: str) -> str:
    return path.rstrip("/") if path != "/" else path


def apply_defaults(data: dict[str, Any]) -> dict[str, Any]:
    merged = dict(data)
    merged["ssh_port"] = int(merged.get("ssh_port") or 22)
    merged["update_format"] = _text(merged.get("update_format") or "folder")
    merged["service_type"] = _text(merged.get("service_type") or "systemd")
    merged["deploy_mode"] = _text(merged.get("deploy_mode") or "inplace")
    merged["service_name"] = _text(merged.get("service_name"))
    merged["app_owner"] = _text(merged.get("app_owner"))
    merged["healthcheck_url"] = _text(merged.get("healthcheck_url"))
    merged["expected_text"] = _text(merged.get("expected_text"))
    merged["unreachable_hint"] = _text(merged.get("unreachable_hint")) or DEFAULT_HINT
    name = _text(merged.get("name"))
    merged["name"] = name
    backups = _text(merged.get("backups_dir"))
    if not backups and RELEASE_RE.fullmatch(name):
        backups = f"/var/backups/deploy-butler/{name}"
    merged["backups_dir"] = backups
    try:
        patterns = as_str_list(merged.get("log_error_patterns"))
    except ValueError:
        patterns = []
    merged["log_error_patterns"] = patterns or list(DEFAULT_LOG_PATTERNS)
    try:
        forbidden = as_str_list(merged.get("forbidden_patterns"))
    except ValueError:
        forbidden = []
    merged["forbidden_patterns"] = forbidden or list(DEFAULT_FORBIDDEN)
    try:
        codes = as_int_list(merged.get("expected_codes"))
    except (TypeError, ValueError):
        codes = []
    merged["expected_codes"] = codes or list(DEFAULT_CODES)
    try:
        merged["db_backup_paths"] = as_str_list(merged.get("db_backup_paths"))
    except ValueError:
        merged["db_backup_paths"] = []
    for key in (
        "updates_dir",
        "ssh_host",
        "ssh_user",
        "ssh_key_path",
        "app_dir",
    ):
        merged[key] = _text(merged.get(key))
    if merged["app_dir"]:
        merged["app_dir"] = _clean_linux_dir(merged["app_dir"])
    if merged["backups_dir"]:
        merged["backups_dir"] = _clean_linux_dir(merged["backups_dir"])
    merged["db_backup_paths"] = [_clean_linux_dir(path) for path in merged["db_backup_paths"]]
    return merged


def validate_fields(data: dict[str, Any]) -> dict[str, str]:
    """Return field -> error code. Empty dict means the draft can be saved."""
    errors: dict[str, str] = {}
    values = apply_defaults(data)

    def secret_scan(field: str, value: str) -> None:
        if looks_like_secret(value):
            errors[field] = "unsafe"

    name = values["name"]
    secret_scan("name", name)
    if not name:
        errors["name"] = "required"
    elif not RELEASE_RE.fullmatch(name):
        errors["name"] = "invalid"

    if values["update_format"] not in {"folder", "archive"}:
        errors["update_format"] = "invalid"
    if values["service_type"] not in {"systemd", "pm2", "none"}:
        errors["service_type"] = "invalid"
    if values["deploy_mode"] not in {"inplace", "releases"}:
        errors["deploy_mode"] = "invalid"

    port = values["ssh_port"]
    if not isinstance(port, int) or isinstance(port, bool) or port < 1 or port > 65535:
        errors["ssh_port"] = "invalid"

    host = values["ssh_host"]
    secret_scan("ssh_host", host)
    if host and (len(host) > 253 or any(ch in host for ch in " \n\r\t'\"`")):
        errors["ssh_host"] = "invalid"

    user = values["ssh_user"]
    secret_scan("ssh_user", user)
    if user and not USER_RE.fullmatch(user):
        errors["ssh_user"] = "invalid"

    key_path = values["ssh_key_path"]
    secret_scan("ssh_key_path", key_path)
    if any(ch in key_path for ch in "\n\r\x00"):
        errors["ssh_key_path"] = "invalid"

    updates = values["updates_dir"]
    secret_scan("updates_dir", updates)
    if any(ch in updates for ch in "\n\r\x00"):
        errors["updates_dir"] = "invalid"

    for field in ("app_dir", "backups_dir"):
        path = values[field]
        secret_scan(field, path)
        if path and not linux_dir(path):
            errors[field] = "invalid"

    for path in values["db_backup_paths"]:
        secret_scan("db_backup_paths", path)
        if not linux_dir(path):
            errors["db_backup_paths"] = "invalid"
            break

    owner = values["app_owner"]
    secret_scan("app_owner", owner)
    if owner and not OWNER_RE.fullmatch(owner):
        errors["app_owner"] = "invalid"

    service = values["service_name"]
    secret_scan("service_name", service)
    if service and not SERVICE_RE.fullmatch(service):
        errors["service_name"] = "invalid"
    if values["service_type"] != "none" and service and not SERVICE_RE.fullmatch(service):
        errors["service_name"] = "invalid"

    url = values["healthcheck_url"]
    secret_scan("healthcheck_url", url)
    if url:
        parsed = urlparse(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or any(ch in url for ch in " \n\r\t'`")
            or len(url) > 500
        ):
            errors["healthcheck_url"] = "invalid"

    try:
        codes = values["expected_codes"]
        if not codes or any(code < 100 or code > 599 for code in codes):
            errors["expected_codes"] = "invalid"
    except (TypeError, ValueError):
        errors["expected_codes"] = "invalid"

    if len(values["expected_text"]) > 200 or any(ch in values["expected_text"] for ch in "\n\r"):
        errors["expected_text"] = "invalid"
    secret_scan("expected_text", values["expected_text"])

    hint = values["unreachable_hint"]
    secret_scan("unreachable_hint", hint)
    if not hint or len(hint) > 400 or any(ch in hint for ch in "\n\r"):
        errors["unreachable_hint"] = "invalid"

    for field, items in (
        ("log_error_patterns", values["log_error_patterns"]),
        ("forbidden_patterns", values["forbidden_patterns"]),
    ):
        if len(items) > 40:
            errors[field] = "invalid"
            continue
        for item in items:
            secret_scan(field, item)
            if len(item) > 80 or any(ch in item for ch in "\n\r\x00"):
                errors[field] = "invalid"
                break

    return errors


def readiness_errors(data: dict[str, Any]) -> dict[str, str]:
    errors = validate_fields(data)
    values = apply_defaults(data)
    for field in READY_FIELDS:
        if not values.get(field):
            errors.setdefault(field, "required")
    if values["service_type"] != "none" and not values["service_name"]:
        errors.setdefault("service_name", "required")
    if not linux_dir(values.get("app_dir", "")):
        errors["app_dir"] = "invalid"
    if not linux_dir(values.get("backups_dir", "")):
        errors["backups_dir"] = "invalid"
    return errors


def release_name_ok(name: str) -> bool:
    return bool(RELEASE_RE.fullmatch(name or ""))
