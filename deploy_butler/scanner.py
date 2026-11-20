"""Scan an updates folder and inspect one update before anything is uploaded."""

from __future__ import annotations

import hashlib
import io
import os
import stat
import tarfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from fnmatch import fnmatch
from pathlib import Path

from deploy_butler.db import Project
from deploy_butler.validate import RELEASE_RE, release_name_ok

_ARCHIVE_SUFFIXES = (".tar.gz", ".tgz", ".tar.bz2", ".tar", ".zip")


@dataclass
class Blocker:
    code: str
    path: str = ""
    pattern: str = ""
    name: str = ""


@dataclass
class FileInfo:
    path: str
    sha256: str
    size: int
    mode: int
    member_name: str


@dataclass
class Inspection:
    name: str
    release: str
    update_sha256: str
    source_path: str
    source_kind: str
    files: list[FileInfo] = field(default_factory=list)
    blockers: list[Blocker] = field(default_factory=list)
    size: int = 0
    mtime: float = 0.0

    @property
    def blocked(self) -> bool:
        return bool(self.blockers) or not release_name_ok(self.release)


@dataclass
class UpdateInfo:
    name: str
    release: str
    sha256: str
    size: int
    file_count: int
    mtime: float
    deployed: bool
    blocked: bool


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_stream(handle) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    while True:
        chunk = handle.read(1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
        total += len(chunk)
    return digest.hexdigest(), total


def manifest_sha(files: list[FileInfo]) -> str:
    digest = hashlib.sha256()
    for item in sorted(files, key=lambda entry: entry.path):
        digest.update(f"{item.path}\0{item.sha256}\0{item.size}\n".encode())
    return digest.hexdigest()


def normalize_rel(rel: str) -> tuple[str | None, str | None]:
    """Return a cleaned relative path, or an error code.

    A leading ./ is normal in archives. ".." , absolute paths and blank
    segments are rejected.
    """
    if rel == "" or "\x00" in rel or "\n" in rel or "\r" in rel:
        return None, "empty_path"
    raw = rel.replace("\\", "/")
    if raw.startswith("/") or raw.startswith("~") or (len(raw) >= 2 and raw[1] == ":"):
        return None, "absolute"
    parts: list[str] = []
    for part in raw.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            return None, "traversal"
        parts.append(part)
    if not parts:
        return None, "empty_path"
    return "/".join(parts), None


def classify_path(rel: str) -> str | None:
    _path, reason = normalize_rel(rel)
    return reason


def match_forbidden(rel: str, patterns: list[str]) -> str | None:
    normalized = rel.replace("\\", "/").rstrip("/")
    parts = normalized.split("/")
    base = parts[-1]
    for pattern in patterns:
        pattern = pattern.strip()
        if not pattern:
            continue
        if pattern.endswith("/"):
            segment = pattern[:-1]
            if any(fnmatch(part, segment) for part in parts):
                return pattern
        elif fnmatch(base, pattern) or fnmatch(normalized, pattern):
            return pattern
    return None


def release_from_name(name: str, update_format: str) -> str:
    stem = name
    if update_format == "archive":
        lower = name.lower()
        for suffix in _ARCHIVE_SUFFIXES:
            if lower.endswith(suffix):
                stem = name[: -len(suffix)]
                break
    return stem


def _file_mode(mode: int) -> int:
    return 0o755 if mode & 0o111 else 0o644


def _add_blocker(inspection: Inspection, code: str, path: str = "", pattern: str = "", name: str = "") -> None:
    inspection.blockers.append(Blocker(code=code, path=path, pattern=pattern, name=name))


def _consider_file(
    inspection: Inspection,
    rel: str,
    sha: str,
    size: int,
    mode: int,
    member_name: str,
    patterns: list[str],
) -> None:
    normalized, reason = normalize_rel(rel)
    if reason or not normalized:
        _add_blocker(inspection, reason or "empty_path", path=rel)
        return
    forbidden = match_forbidden(normalized, patterns)
    if forbidden:
        _add_blocker(inspection, "forbidden", path=normalized, pattern=forbidden)
    if any(item.path == normalized for item in inspection.files):
        _add_blocker(inspection, "special", path=normalized)
        return
    inspection.files.append(
        FileInfo(
            path=normalized,
            sha256=sha,
            size=size,
            mode=_file_mode(mode),
            member_name=member_name,
        )
    )


def inspect_folder(root: Path, patterns: list[str]) -> Inspection:
    inspection = Inspection(
        name=root.name,
        release=release_from_name(root.name, "folder"),
        update_sha256="",
        source_path=str(root),
        source_kind="folder",
        mtime=root.stat().st_mtime,
    )
    if root.is_symlink():
        _add_blocker(inspection, "symlink", path=root.name)
        return inspection
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        kept: list[str] = []
        for dirname in dirnames:
            current = Path(dirpath) / dirname
            rel = current.relative_to(root).as_posix()
            if current.is_symlink():
                _add_blocker(inspection, "symlink", path=rel)
                continue
            normalized, reason = normalize_rel(rel)
            if reason or not normalized:
                _add_blocker(inspection, reason or "empty_path", path=rel)
                continue
            forbidden = match_forbidden(normalized + "/", patterns)
            if forbidden:
                _add_blocker(inspection, "forbidden", path=normalized, pattern=forbidden)
                continue
            kept.append(dirname)
        dirnames[:] = kept
        for filename in sorted(filenames):
            current = Path(dirpath) / filename
            rel = current.relative_to(root).as_posix()
            if current.is_symlink():
                _add_blocker(inspection, "symlink", path=rel)
                continue
            if not current.is_file():
                _add_blocker(inspection, "special", path=rel)
                continue
            sha = sha256_file(current)
            _consider_file(
                inspection,
                rel,
                sha,
                current.stat().st_size,
                current.stat().st_mode,
                rel,
                patterns,
            )
    _finish(inspection, identity=manifest_sha(inspection.files), size=sum(item.size for item in inspection.files))
    return inspection


def _zip_is_symlink(info: zipfile.ZipInfo) -> bool:
    mode = info.external_attr >> 16
    return stat.S_ISLNK(mode)


def inspect_archive(path: Path, patterns: list[str]) -> Inspection:
    inspection = Inspection(
        name=path.name,
        release=release_from_name(path.name, "archive"),
        update_sha256="",
        source_path=str(path),
        source_kind="zip" if path.name.lower().endswith(".zip") else "tar",
        size=path.stat().st_size,
        mtime=path.stat().st_mtime,
    )
    if path.is_symlink():
        _add_blocker(inspection, "symlink", path=path.name)
        _finish(inspection, identity=sha256_file(path), size=path.stat().st_size)
        return inspection
    try:
        if inspection.source_kind == "zip":
            _inspect_zip(path, inspection, patterns)
        else:
            _inspect_tar(path, inspection, patterns)
    except (tarfile.TarError, zipfile.BadZipFile, OSError, ValueError):
        _add_blocker(inspection, "special", path=path.name)
    identity = sha256_file(path)
    _finish(inspection, identity=identity, size=path.stat().st_size)
    return inspection


def _inspect_tar(path: Path, inspection: Inspection, patterns: list[str]) -> None:
    with tarfile.open(path, "r:*") as archive:
        for member in archive.getmembers():
            name = member.name
            if member.issym():
                _add_blocker(inspection, "symlink", path=name)
                continue
            if member.islnk():
                _add_blocker(inspection, "hardlink", path=name)
                continue
            if member.isdir():
                normalized, reason = normalize_rel(name)
                if reason:
                    _add_blocker(inspection, reason, path=name)
                elif normalized and (forbidden := match_forbidden(normalized + "/", patterns)):
                    _add_blocker(inspection, "forbidden", path=normalized, pattern=forbidden)
                continue
            if not member.isfile():
                _add_blocker(inspection, "special", path=name)
                continue
            stream = archive.extractfile(member)
            if stream is None:
                _add_blocker(inspection, "special", path=name)
                continue
            sha, size = sha256_stream(stream)
            _consider_file(inspection, name, sha, size, member.mode, name, patterns)


def _inspect_zip(path: Path, inspection: Inspection, patterns: list[str]) -> None:
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            name = info.filename
            if _zip_is_symlink(info):
                _add_blocker(inspection, "symlink", path=name)
                continue
            if name.endswith("/"):
                normalized, reason = normalize_rel(name)
                if reason:
                    _add_blocker(inspection, reason, path=name)
                elif normalized and (forbidden := match_forbidden(normalized + "/", patterns)):
                    _add_blocker(inspection, "forbidden", path=normalized, pattern=forbidden)
                continue
            with archive.open(info, "r") as stream:
                sha, size = sha256_stream(stream)
            mode = (info.external_attr >> 16) or 0o644
            _consider_file(inspection, name, sha, size, mode, name, patterns)


def _finish(inspection: Inspection, identity: str, size: int) -> None:
    if not release_name_ok(inspection.release):
        _add_blocker(inspection, "bad_release", name=inspection.release)
    if not inspection.files and not any(item.code == "empty" for item in inspection.blockers):
        if not any(item.code in {"symlink", "special", "forbidden", "traversal", "absolute", "hardlink"} for item in inspection.blockers):
            _add_blocker(inspection, "empty")
    inspection.update_sha256 = identity
    inspection.size = size


def inspect_update(project: Project, name: str) -> Inspection | None:
    root = Path(project.updates_dir)
    if not root.is_dir():
        return None
    candidate = root / name
    if project.update_format == "folder":
        if not candidate.is_dir():
            return None
        return inspect_folder(candidate, project.forbidden_patterns)
    if not candidate.is_file():
        return None
    return inspect_archive(candidate, project.forbidden_patterns)


def scan_updates(project: Project, deployed_sha: str | None) -> tuple[list[UpdateInfo], str | None]:
    root = Path(project.updates_dir)
    if not root.is_dir():
        return [], "missing"
    found: list[UpdateInfo] = []
    if project.update_format == "folder":
        children = [path for path in root.iterdir() if path.is_dir() or path.is_symlink()]
    else:
        children = [
            path
            for path in root.iterdir()
            if path.is_file() and path.name.lower().endswith(_ARCHIVE_SUFFIXES)
        ]
    for child in children:
        if project.update_format == "folder":
            inspection = inspect_folder(child, project.forbidden_patterns)
        else:
            inspection = inspect_archive(child, project.forbidden_patterns)
        found.append(
            UpdateInfo(
                name=inspection.name,
                release=inspection.release,
                sha256=inspection.update_sha256,
                size=inspection.size,
                file_count=len(inspection.files),
                mtime=inspection.mtime,
                deployed=bool(deployed_sha) and inspection.update_sha256 == deployed_sha and not inspection.blocked,
                blocked=inspection.blocked,
            )
        )
    found.sort(key=lambda item: (item.mtime, item.name), reverse=True)
    return found, None


def _add_tar_file(archive: tarfile.TarFile, rel: str, stream, size: int, mode: int) -> None:
    info = tarfile.TarInfo(rel)
    info.size = size
    info.mode = mode
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = 0
    info.type = tarfile.REGTYPE
    archive.addfile(info, stream)


def build_normalized_tar(inspection: Inspection, dest: Path) -> str:
    if inspection.blocked:
        raise ValueError("blocked")
    dest.parent.mkdir(parents=True, exist_ok=True)
    directories: set[str] = set()
    for item in inspection.files:
        parent = Path(item.path).parent
        while str(parent) not in {".", ""}:
            directories.add(parent.as_posix())
            parent = parent.parent
    with tarfile.open(dest, "w:gz") as archive:
        for directory in sorted(directories):
            info = tarfile.TarInfo(directory)
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            info.uid = 0
            info.gid = 0
            info.mtime = 0
            archive.addfile(info)
        ordered = sorted(inspection.files, key=lambda item: item.path)
        if inspection.source_kind == "folder":
            root = Path(inspection.source_path)
            for item in ordered:
                with (root / item.path).open("rb") as handle:
                    _add_tar_file(archive, item.path, handle, item.size, item.mode)
        elif inspection.source_kind == "tar":
            with tarfile.open(inspection.source_path, "r:*") as source:
                for item in ordered:
                    member = source.getmember(item.member_name)
                    stream = source.extractfile(member)
                    if stream is None:
                        raise ValueError("missing member")
                    _add_tar_file(archive, item.path, stream, item.size, item.mode)
        elif inspection.source_kind == "zip":
            with zipfile.ZipFile(inspection.source_path) as source:
                for item in ordered:
                    with source.open(item.member_name, "r") as stream:
                        _add_tar_file(archive, item.path, stream, item.size, item.mode)
        else:
            raise ValueError("unknown source")
    return sha256_file(dest)


def assert_tar_matches(archive: Path, files: list[FileInfo]) -> None:
    expected = {item.path: item.sha256 for item in files}
    with tarfile.open(archive, "r:gz") as tar:
        members = [member for member in tar.getmembers() if member.isfile()]
        names = {member.name for member in members}
        if names != set(expected):
            raise ValueError("tar file set mismatch")
        for member in members:
            if member.issym() or member.islnk():
                raise ValueError("link in tar")
            if classify_path(member.name):
                raise ValueError("bad path in tar")
            stream = tar.extractfile(member)
            if stream is None:
                raise ValueError("unreadable member")
            digest, _size = sha256_stream(stream)
            if digest != expected[member.name]:
                raise ValueError("tar content mismatch")


def iso_mtime(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def short_hash(value: str, length: int = 12) -> str:
    return value[:length]


def hash_tar_bytes(payload: bytes) -> dict[str, str]:
    """Used by the fake SSH server: hash regular files inside a tar.gz."""
    found: dict[str, str] = {}
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue
            stream = archive.extractfile(member)
            if stream is None:
                continue
            digest, _size = sha256_stream(stream)
            found[member.name] = digest
    return found
