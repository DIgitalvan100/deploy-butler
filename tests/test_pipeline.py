from __future__ import annotations

import threading
from pathlib import Path

from deploy_butler.pipeline import ERROR, ERROR_BEFORE, ROLLED_BACK, SUCCESS, run_deploy, run_manual_rollback
from deploy_butler.scanner import inspect_update
from deploy_butler.ssh import Unreachable as SSHUnreachable
from tests.conftest import FAST, make_project
from tests.fake_ssh import FakeRemote, Scenario


def _seed(project, name: str = "r35", text: str = "hello\n") -> None:
    folder = Path(project.updates_dir) / name
    (folder / "sub").mkdir(parents=True)
    (folder / "hello.txt").write_text(text, encoding="utf-8")
    (folder / "sub" / "app.js").write_text("console.log(1)\n", encoding="utf-8")


def _run(project, tmp_path, scenario, health=None, gate=None, name="r35"):
    inspection = inspect_update(project, name)
    assert inspection is not None

    def connector(current, timeout):
        return FakeRemote(current, scenario)

    return run_deploy(
        project,
        inspection,
        backup_db=True,
        home=tmp_path / "home",
        connector=connector,
        health=health or (lambda *_args: (True, "HTTP 200")),
        timing=FAST,
        gate=gate,
    )


def _commands(scenario: Scenario) -> list[str]:
    names = []
    for call in scenario.calls:
        if call[0] == "sha256sum":
            names.append("sha256sum")
        elif call[0] == "sudo":
            names.append(call[3])
        else:
            names.append(call[0])
    return names


def test_success(tmp_path, key_path):
    project = make_project(tmp_path, key_path)
    _seed(project)
    scenario = Scenario()
    outcome = _run(project, tmp_path, scenario)
    assert outcome.status == SUCCESS
    assert "rollback" not in _commands(scenario)
    assert _commands(scenario)[:6] == ["sftp", "sha256sum", "backup", "apply", "restart", "logs"]
    assert scenario.calls[2][:3] == ["sudo", "-n", project.helper_path]
    assert scenario.rollbacks == []


def test_archive_sha_mismatch_stops(tmp_path, key_path):
    project = make_project(tmp_path, key_path)
    _seed(project)
    scenario = Scenario()
    scenario.corrupt_remote_sha = True
    outcome = _run(project, tmp_path, scenario)
    assert outcome.status == ERROR_BEFORE
    assert "backup" not in _commands(scenario)
    assert "apply" not in _commands(scenario)
    assert any(event["key"] == "log.sha_mismatch" for event in outcome.events)


def test_applied_sha_mismatch_rolls_back(tmp_path, key_path):
    project = make_project(tmp_path, key_path)
    _seed(project)
    scenario = Scenario()
    scenario.bad_apply_hash = True
    outcome = _run(project, tmp_path, scenario)
    assert outcome.status == ROLLED_BACK
    assert len(scenario.rollbacks) == 1
    assert any(event["key"] == "log.hash_mismatch" for event in outcome.events)


def test_forbidden_file_blocks(tmp_path, key_path):
    project = make_project(tmp_path, key_path)
    folder = Path(project.updates_dir) / "r35"
    folder.mkdir()
    (folder / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    (folder / "app.js").write_text("console.log(1)\n", encoding="utf-8")
    called = []

    def connector(current, timeout):
        called.append(current)
        raise AssertionError("SSH must not run")

    inspection = inspect_update(project, "r35")
    assert inspection.blocked
    outcome = run_deploy(
        project,
        inspection,
        backup_db=False,
        home=tmp_path / "home",
        connector=connector,
        timing=FAST,
    )
    assert outcome.status == ERROR_BEFORE
    assert called == []
    assert any(item.code == "forbidden" for item in inspection.blockers)


def test_path_traversal_in_archive(tmp_path, key_path):
    import io
    import tarfile

    project = make_project(tmp_path, key_path, update_format="archive")
    archive = Path(project.updates_dir) / "r35.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        payload = b"owned"
        info = tarfile.TarInfo("../etc/passwd")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    inspection = inspect_update(project, archive.name)
    assert inspection is not None
    assert inspection.blocked
    assert any(item.code == "traversal" for item in inspection.blockers)
    called = []
    outcome = run_deploy(
        project,
        inspection,
        backup_db=False,
        home=tmp_path / "home",
        connector=lambda *_args: called.append(1),
        timing=FAST,
    )
    assert outcome.status == ERROR_BEFORE
    assert called == []


def test_bad_release_name(tmp_path, key_path):
    project = make_project(tmp_path, key_path)
    folder = Path(project.updates_dir) / "-bad"
    folder.mkdir()
    (folder / "app.js").write_text("console.log(1)\n", encoding="utf-8")
    inspection = inspect_update(project, "-bad")
    assert inspection.blocked
    outcome = run_deploy(
        project,
        inspection,
        backup_db=False,
        home=tmp_path / "home",
        connector=lambda *_args: (_ for _ in ()).throw(AssertionError("no ssh")),
        timing=FAST,
    )
    assert outcome.status == ERROR_BEFORE
    assert any(item.code == "bad_release" for item in inspection.blockers)


def test_health_failure_rolls_back_once(tmp_path, key_path):
    project = make_project(tmp_path, key_path)
    _seed(project)
    scenario = Scenario()
    calls = []
    outcome = _run(
        project,
        tmp_path,
        scenario,
        health=lambda *_args: calls.append(1) or (False, "HTTP 500"),
    )
    assert outcome.status == ROLLED_BACK
    assert len(scenario.rollbacks) == 1
    assert len(calls) >= FAST.health_attempts
    assert any(event["key"] == "log.health_bad" for event in outcome.events)


def test_restart_failure_rolls_back(tmp_path, key_path):
    project = make_project(tmp_path, key_path, healthcheck_url="")
    _seed(project)
    scenario = Scenario()
    scenario.fail_restart_times = 1
    outcome = _run(project, tmp_path, scenario, health=lambda *_args: (True, "HTTP 200"))
    assert outcome.status == ROLLED_BACK
    assert scenario.restarts >= 2
    assert len(scenario.rollbacks) == 1
    assert any(event["key"] == "log.restart_fail" for event in outcome.events)


def test_parallel_deploy_is_rejected(tmp_path, key_path):
    project = make_project(tmp_path, key_path, healthcheck_url="")
    _seed(project)
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def connector(current, timeout):
        calls.append(1)
        return FakeRemote(current, Scenario())

    def gate():
        entered.set()
        assert release.wait(5)

    inspection = inspect_update(project, "r35")

    def run(with_gate):
        return run_deploy(
            project,
            inspection,
            backup_db=False,
            home=tmp_path / "home",
            connector=connector,
            health=lambda *_args: (True, "HTTP 200"),
            timing=FAST,
            gate=gate if with_gate else None,
        )

    holder: dict = {}
    worker = threading.Thread(target=lambda: holder.update(first=run(True)))
    worker.start()
    assert entered.wait(5)
    second = run(False)
    release.set()
    worker.join(5)
    assert second.status == ERROR_BEFORE
    assert any(event["key"] == "log.busy" for event in second.events)
    assert holder["first"].status == SUCCESS
    assert calls == [1]


def test_unreachable_uses_hint(tmp_path, key_path):
    project = make_project(tmp_path, key_path)
    _seed(project)

    def connector(_project, _timeout):
        raise SSHUnreachable("timed out")

    inspection = inspect_update(project, "r35")
    outcome = run_deploy(
        project,
        inspection,
        backup_db=False,
        home=tmp_path / "home",
        connector=connector,
        timing=FAST,
    )
    assert outcome.status == ERROR_BEFORE
    assert any(event["key"] == "log.unreachable" and project.unreachable_hint in event["params"]["hint"] for event in outcome.events)


def test_manual_rollback_does_not_roll_back_again(tmp_path, key_path):
    project = make_project(tmp_path, key_path)
    scenario = Scenario()

    def connector(current, timeout):
        return FakeRemote(current, scenario)

    outcome = run_manual_rollback(
        project,
        release="r35",
        update_name="r35",
        update_sha256="abc",
        backup_db=False,
        home=tmp_path / "home",
        connector=connector,
        health=lambda *_args: (False, "HTTP 500"),
        timing=FAST,
    )
    assert outcome.status == ERROR
    assert len(scenario.rollbacks) == 1
    assert any(event["key"] == "log.manual_check_fail" for event in outcome.events)
