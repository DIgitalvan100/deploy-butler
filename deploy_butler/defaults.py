"""Defaults the user can edit. Nothing here is a secret."""

DEFAULT_LOG_PATTERNS = [
    "Error",
    "TypeError",
    "ReferenceError",
    "SyntaxError",
    "Unhandled",
    "EADDRINUSE",
]

DEFAULT_FORBIDDEN = [
    "*.env",
    ".env*",
    "*.db",
    "*.sqlite",
    "*.pem",
    "*.key",
    "id_rsa*",
    "node_modules/",
    ".git/",
    "*-test.js",
    "test-*",
    "seed-*",
]

DEFAULT_CODES = [200, 301, 302]

DEFAULT_HINT = "Сервер недоступен. Проверьте интернет/VPN/ключ"

RELEASE_RE_SRC = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"

PROTECTED_DIRS = {
    "/",
    "/bin",
    "/boot",
    "/dev",
    "/etc",
    "/lib",
    "/lib64",
    "/proc",
    "/root",
    "/run",
    "/sbin",
    "/sys",
    "/usr",
    "/var",
    "/opt",
    "/home",
    "/tmp",
    "/srv",
}
