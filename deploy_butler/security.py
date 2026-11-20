"""Localhost binding checks, CSRF, and log masking."""

from __future__ import annotations

import hmac
import re

_SECRET_RES = [
    re.compile(
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
        re.DOTALL,
    ),
    re.compile(
        r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|authorization|private[_-]?key)"
        r"(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|\S+)"
    ),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-+/=]+"),
    re.compile(r"\b(?:ghp_|gho_|ghu_|ghs_|github_pat_|sk-|xox[baprs]-)[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
]


def mask_text(value: str) -> str:
    """Hide tokens, passwords and private keys. Leave ordinary hashes alone."""
    if not value:
        return value
    masked = value
    for pattern in _SECRET_RES:
        if pattern.groups >= 3 and "password|passwd" in pattern.pattern:
            masked = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}***", masked)
        else:
            masked = pattern.sub("***", masked)
    return masked


def same_token(left: str, right: str) -> bool:
    a = left.encode("utf-8")
    b = right.encode("utf-8")
    if len(a) != len(b) or not a:
        return False
    return hmac.compare_digest(a, b)


def allowed_hosts(port: int) -> set[str]:
    return {f"127.0.0.1:{port}", f"localhost:{port}"}


def allowed_origins(port: int) -> set[str]:
    return {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
