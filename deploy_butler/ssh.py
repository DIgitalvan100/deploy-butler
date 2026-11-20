"""SSH and SFTP. The only remote commands are sha256sum and the project helper."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path

import paramiko
from paramiko.ssh_exception import AuthenticationException, BadHostKeyException, SSHException

from deploy_butler.db import Project
from deploy_butler.security import mask_text

_ARCHIVE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}\.tar\.gz$")
_RELEASE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_DIGITS = re.compile(r"^[0-9]{1,6}$")


class SSHProblem(Exception):
    pass


class Unreachable(SSHProblem):
    def __init__(self, detail: str):
        self.detail = mask_text(detail)
        super().__init__(self.detail)


class KeyNotFound(SSHProblem):
    pass


class KeyEncrypted(SSHProblem):
    pass


class KeyUnreadable(SSHProblem):
    pass


class HostKeyUnknown(SSHProblem):
    def __init__(self, hostname: str, key: paramiko.PKey):
        self.hostname = hostname
        self.key = key
        self.key_type = key.get_name()
        self.fingerprint = key.fingerprint
        super().__init__(self.fingerprint)


class HostKeyChanged(SSHProblem):
    def __init__(self, hostname: str, key: paramiko.PKey, expected: paramiko.PKey):
        self.hostname = hostname
        self.key = key
        self.expected = expected
        self.key_type = key.get_name()
        self.fingerprint = key.fingerprint
        self.expected_fingerprint = expected.fingerprint
        super().__init__(self.fingerprint)


class CaptureRejectPolicy(paramiko.RejectPolicy):
    """Reject unknown host keys and surface the fingerprint. Never auto-add."""

    def missing_host_key(self, client, hostname, key):
        raise HostKeyUnknown(hostname, key)


@dataclass
class RemoteResult:
    argv: list[str]
    exit_code: int
    stdout: str
    stderr: str


def hostkey_name(host: str, port: int) -> str:
    if port == 22:
        return host
    return f"[{host}]:{port}"


def load_private_key(path: str) -> None:
    """Read the key enough to know it is usable. The passphrase is never supplied."""
    key_path = Path(path).expanduser()
    if not key_path.is_file():
        raise KeyNotFound()
    try:
        paramiko.PKey.from_path(key_path)
    except paramiko.PasswordRequiredException as exc:
        raise KeyEncrypted() from exc
    except TypeError as exc:
        message = str(exc).lower()
        if "password" in message or "encrypted" in message:
            raise KeyEncrypted() from exc
        raise KeyUnreadable() from exc
    except (OSError, SSHException, ValueError) as exc:
        raise KeyUnreadable() from exc


def save_host_key(path: Path, host: str, port: int, key: paramiko.PKey) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        keys = paramiko.HostKeys(str(path))
    else:
        keys = paramiko.HostKeys()
    keys.add(hostkey_name(host, port), key.get_name(), key)
    keys.save(str(path))


class ParamikoRemote:
    def __init__(self, client: paramiko.SSHClient, project: Project, sftp: paramiko.SFTPClient):
        self.client = client
        self.project = project
        self.sftp = sftp
        self._uploaded: set[str] = set()

    def _remote_path(self, remote_name: str) -> str:
        if not _ARCHIVE_NAME.fullmatch(remote_name):
            raise ValueError("refusing archive name")
        return f"{self.project.staging_dir}/{remote_name}"

    def _mkdir_staging(self) -> None:
        current = ""
        for part in self.project.staging_dir.strip("/").split("/"):
            current += "/" + part
            if current in {"/tmp", "/tmp/deploy-butler"} or current == self.project.staging_dir:
                try:
                    self.sftp.stat(current)
                except OSError:
                    if current == "/tmp":
                        continue
                    self.sftp.mkdir(current)

    def upload(self, local: Path, remote_name: str) -> str:
        remote = self._remote_path(remote_name)
        self._mkdir_staging()
        self.sftp.put(str(local), remote)
        self._uploaded.add(remote_name)
        return remote

    def remove(self, remote_name: str) -> None:
        remote = self._remote_path(remote_name)
        try:
            self.sftp.remove(remote)
        except FileNotFoundError:
            return
        except OSError as exc:
            if getattr(exc, "errno", None) in {2} or "No such file" in str(exc):
                return
            raise

    def sha256(self, remote_name: str) -> str:
        remote = self._remote_path(remote_name)
        result = self._exec(["sha256sum", remote])
        if result.exit_code != 0 or not result.stdout.strip():
            raise Unreachable(result.stderr.strip() or "sha256sum failed")
        token = result.stdout.split()[0].strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", token):
            raise Unreachable("sha256sum returned an unexpected line")
        return token

    def helper(self, args: list[str], stdin: str = "") -> RemoteResult:
        self._check_helper_args(args)
        if len(stdin) > 5_000_000:
            raise ValueError("helper stdin is too large")
        return self._exec(["sudo", "-n", self.project.helper_path, *args], stdin=stdin)

    def _check_helper_args(self, args: list[str]) -> None:
        if not args:
            raise ValueError("missing helper command")
        command, rest = args[0], args[1:]
        if command == "hashes" and rest == []:
            return
        if command == "restart" and rest == []:
            return
        if command == "status" and rest == []:
            return
        if command == "list-backups" and rest == []:
            return
        if command == "logs" and len(rest) == 1 and _DIGITS.fullmatch(rest[0]):
            return
        if command == "prune" and len(rest) == 1 and _DIGITS.fullmatch(rest[0]):
            return
        if command in {"backup", "rollback"} and rest and _RELEASE.fullmatch(rest[0]):
            if rest[1:] in ([], ["--db"]):
                return
        if (
            command == "apply"
            and len(rest) == 2
            and _RELEASE.fullmatch(rest[0])
            and rest[1] == f"{self.project.staging_dir}/{rest[0]}.tar.gz"
        ):
            return
        raise ValueError("helper command is not allowed")

    def _exec(self, argv: list[str], stdin: str = "") -> RemoteResult:
        if argv[0] not in {"sha256sum", "sudo"}:
            raise ValueError("remote command is not allowed")
        if argv[0] == "sudo" and argv[:3] != ["sudo", "-n", self.project.helper_path]:
            raise ValueError("sudo target is not the helper")
        command = " ".join(shlex.quote(part) for part in argv)
        channel_in, stdout, stderr = self.client.exec_command(command, timeout=3600)
        if stdin:
            channel_in.write(stdin)
        channel_in.channel.shutdown_write()
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        code = stdout.channel.recv_exit_status()
        return RemoteResult(argv=argv, exit_code=code, stdout=out, stderr=err)

    def close(self) -> None:
        try:
            self.sftp.close()
        finally:
            self.client.close()


def connect(project: Project, timeout: float, known_hosts: Path) -> ParamikoRemote:
    known_hosts.parent.mkdir(parents=True, exist_ok=True)
    known_hosts.touch(exist_ok=True)
    client = paramiko.SSHClient()
    client.load_host_keys(str(known_hosts))
    client.set_missing_host_key_policy(CaptureRejectPolicy())
    try:
        client.connect(
            hostname=project.ssh_host,
            port=project.ssh_port,
            username=project.ssh_user,
            key_filename=str(Path(project.ssh_key_path).expanduser()),
            look_for_keys=False,
            allow_agent=False,
            timeout=timeout,
            auth_timeout=timeout,
            banner_timeout=timeout,
        )
    except HostKeyUnknown:
        client.close()
        raise
    except BadHostKeyException as exc:
        client.close()
        raise HostKeyChanged(exc.hostname, exc.key, exc.expected_key) from exc
    except AuthenticationException as exc:
        client.close()
        raise Unreachable("authentication failed") from exc
    except (OSError, SSHException, EOFError) as exc:
        client.close()
        raise Unreachable(str(exc) or exc.__class__.__name__) from exc
    try:
        sftp = client.open_sftp()
    except (OSError, SSHException) as exc:
        client.close()
        raise Unreachable(str(exc) or "sftp failed") from exc
    return ParamikoRemote(client, project, sftp)
