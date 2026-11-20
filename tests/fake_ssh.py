"""In-memory SSH stand-in. It only accepts upload, sha256 and helper calls."""

from __future__ import annotations

import hashlib
from pathlib import Path

from deploy_butler.scanner import hash_tar_bytes


class RemoteResult:
    def __init__(self, argv: list[str], exit_code: int, stdout: str, stderr: str = ""):
        self.argv = argv
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


class Scenario:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.uploads: dict[str, bytes] = {}
        self.bad_apply_hash = False
        self.corrupt_remote_sha = False
        self.fail_restart_times = 0
        self.restarts = 0
        self.rollbacks: list[list[str]] = []
        self.fail_backup = False
        self.fail_rollback = False
        self.logs = "listening on 3000\n"


class FakeRemote:
    def __init__(self, project, scenario: Scenario):
        self.project = project
        self.scenario = scenario

    def upload(self, local: Path, remote_name: str) -> str:
        self.scenario.uploads[remote_name] = Path(local).read_bytes()
        self.scenario.calls.append(["sftp", remote_name])
        return f"{self.project.staging_dir}/{remote_name}"

    def sha256(self, remote_name: str) -> str:
        self.scenario.calls.append(["sha256sum", remote_name])
        if self.scenario.corrupt_remote_sha:
            return "0" * 64
        return hashlib.sha256(self.scenario.uploads[remote_name]).hexdigest()

    def helper(self, args: list[str], stdin: str = "") -> RemoteResult:
        allowed = {"hashes", "backup", "apply", "restart", "status", "logs", "rollback", "list-backups", "prune"}
        if not args or args[0] not in allowed:
            raise AssertionError(f"command not allowed: {args}")
        self.scenario.calls.append(["sudo", "-n", self.project.helper_path, *args])
        command = args[0]
        if command == "backup":
            if self.scenario.fail_backup:
                return RemoteResult(args, 1, "", "backup failed")
            return RemoteResult(args, 0, "APP 12\nOK\n", "")
        if command == "apply":
            payload = self.scenario.uploads[args[1] + ".tar.gz"]
            hashes = hash_tar_bytes(payload)
            if self.scenario.bad_apply_hash and hashes:
                first = sorted(hashes)[0]
                hashes[first] = "ab" * 32
            body = "\n".join(f"{digest}\t{path}" for path, digest in sorted(hashes.items()))
            return RemoteResult(args, 0, body + "\nOK\n", "")
        if command == "restart":
            self.scenario.restarts += 1
            if self.scenario.restarts <= self.scenario.fail_restart_times:
                return RemoteResult(args, 1, "inactive\n", "not active")
            return RemoteResult(args, 0, "active\n", "")
        if command == "status":
            return RemoteResult(args, 0, "active\n", "")
        if command == "logs":
            return RemoteResult(args, 0, self.scenario.logs + "OK\n", "")
        if command == "rollback":
            self.scenario.rollbacks.append(list(args))
            if self.scenario.fail_rollback:
                return RemoteResult(args, 1, "", "rollback failed")
            return RemoteResult(args, 0, "OK\n", "")
        if command == "hashes":
            return RemoteResult(args, 0, "OK\n", "")
        if command == "list-backups":
            return RemoteResult(args, 0, "OK\n", "")
        if command == "prune":
            return RemoteResult(args, 0, "OK\n", "")
        raise AssertionError(command)

    def remove(self, remote_name: str) -> None:
        self.scenario.calls.append(["sftp-remove", remote_name])
        self.scenario.uploads.pop(remote_name, None)

    def close(self) -> None:
        self.scenario.calls.append(["close"])
