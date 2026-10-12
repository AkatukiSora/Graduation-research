from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import cast

import pytest

from generation_io import supervisor
from generation_io.supervisor import _Limits, _supervise


@pytest.fixture(autouse=True)
def isolate_default_resource_root(tmp_path, monkeypatch):
    resource_root = tmp_path / "supervisor-resource-root"
    resource_root.mkdir()
    monkeypatch.setattr(supervisor, "EVIDENCE", resource_root)


def limits(**overrides):
    values = {
        "wall_seconds": 5.0,
        "rss_bytes": 512 * 1024 * 1024,
        "minimum_memory_free_bytes": 0,
        "minimum_disk_free_bytes": 0,
        "output_bytes": 1024,
        "report_bytes": 1024 * 1024,
        "poll_seconds": 0.05,
        "terminate_grace_seconds": 0.1,
    }
    values.update(overrides)
    return _Limits(**values)


def launch(tmp_path: Path, argv: list[str], *, output_path=None, run_limits=None):
    return _supervise(
        argv,
        cwd=tmp_path,
        env=os.environ.copy(),
        stage="test-child",
        log_path=tmp_path / "child.log",
        report_path=tmp_path / "child-budget.json",
        output_path=output_path,
        limits=run_limits or limits(),
    )


def test_supervisor_records_child_exit_rss_and_log_hash(tmp_path):
    result = launch(tmp_path, [sys.executable, "-c", "print('child-ok')"])

    report = json.loads((tmp_path / "child-budget.json").read_text(encoding="utf-8"))
    assert result["status"] == "complete"
    assert result["exit_code"] == 0
    assert result["max_sampled_rss_bytes"] > 0
    assert report["log"]["sha256"]
    assert (tmp_path / "child.log").read_text(encoding="utf-8").strip() == "child-ok"


def test_supervisor_terminates_timed_out_child_and_records_failure(tmp_path):
    result = launch(
        tmp_path,
        [sys.executable, "-c", "import time; time.sleep(10)"],
        run_limits=limits(wall_seconds=0.15),
    )

    report = json.loads((tmp_path / "child-budget.json").read_text(encoding="utf-8"))
    assert result["status"] == "failed"
    assert result["failure_reason"] == "wall_timeout"
    assert result["exit_code"] != 0
    assert report["reaped"] is True


def test_supervisor_stops_child_when_output_budget_is_exceeded(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    script = "from pathlib import Path; Path('output/data').write_bytes(b'x' * 100)"
    result = launch(
        tmp_path,
        [sys.executable, "-c", script],
        output_path=output,
        run_limits=limits(output_bytes=32),
    )

    report = json.loads((tmp_path / "child-budget.json").read_text(encoding="utf-8"))
    assert result["status"] == "failed"
    assert result["failure_reason"] == "output_budget_exceeded"
    assert report["reaped"] is True


def test_verify_failure_does_not_mutate_any_input_file(tmp_path, monkeypatch):
    input_root = tmp_path / "saved-output"
    input_root.mkdir()
    (input_root / "generation-manifest.json").write_bytes(b'{"saved":true}\n')
    (input_root / "normalized-points.jsonl").write_bytes(b'{"id":"fixed"}\n')
    (input_root / "nested").mkdir()
    (input_root / "nested" / "output.jsonl").write_bytes(b'{"nested":true}\n')
    before = {
        path.relative_to(input_root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in input_root.rglob("*")
        if path.is_file()
    }
    attempt = tmp_path / "attempt-01"
    attempt.mkdir()
    child = tmp_path / "verify_failure.py"
    child.write_text("raise SystemExit(2)\n", encoding="utf-8")
    monkeypatch.setattr("generation_io.supervisor.TASK_STARTED_MONOTONIC", time.monotonic())

    result = _supervise(
        [sys.executable, str(child)],
        cwd=tmp_path,
        env=os.environ.copy(),
        stage="verify",
        log_path=attempt / "verify.log",
        report_path=attempt / "verify.json",
        output_path=input_root,
        limits=limits(),
    )

    after = {
        path.relative_to(input_root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in input_root.rglob("*")
        if path.is_file()
    }
    assert result["failure_reason"] == "child_nonzero_exit"
    assert after == before


def test_memory_headroom_uses_minimum_of_host_and_cgroup_v2(tmp_path, monkeypatch):
    cgroup_root = tmp_path / "sys/fs/cgroup"
    cgroup = cgroup_root / "slice/worker"
    cgroup.mkdir(parents=True)
    cgroup_file = tmp_path / "proc/self/cgroup"
    cgroup_file.parent.mkdir(parents=True)
    cgroup_file.write_text("0::/slice/worker\n", encoding="ascii")
    (cgroup / "memory.max").write_text("1000\n", encoding="ascii")
    (cgroup / "memory.current").write_text("400\n", encoding="ascii")
    monkeypatch.setattr(supervisor, "CGROUP_FILE", cgroup_file, raising=False)
    monkeypatch.setattr(supervisor, "CGROUP_ROOT", cgroup_root, raising=False)
    monkeypatch.setattr(supervisor, "_host_available_memory_bytes", lambda: 900)

    assert supervisor._available_memory_bytes() == 600


def test_memory_headroom_uses_host_when_cgroup_limit_is_unbounded(tmp_path, monkeypatch):
    cgroup_root = tmp_path / "sys/fs/cgroup"
    cgroup = cgroup_root / "slice"
    cgroup.mkdir(parents=True)
    cgroup_file = tmp_path / "proc/self/cgroup"
    cgroup_file.parent.mkdir(parents=True)
    cgroup_file.write_text("0::/slice\n", encoding="ascii")
    (cgroup / "memory.max").write_text("max\n", encoding="ascii")
    monkeypatch.setattr(supervisor, "CGROUP_FILE", cgroup_file, raising=False)
    monkeypatch.setattr(supervisor, "CGROUP_ROOT", cgroup_root, raising=False)
    monkeypatch.setattr(supervisor, "_host_available_memory_bytes", lambda: 900)

    assert supervisor._available_memory_bytes() == 900


def test_memory_headroom_fails_closed_when_cgroup_v2_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(
        supervisor,
        "CGROUP_FILE",
        tmp_path / "missing-cgroup",
        raising=False,
    )
    monkeypatch.setattr(supervisor, "CGROUP_ROOT", tmp_path, raising=False)
    monkeypatch.setattr(supervisor, "_host_available_memory_bytes", lambda: 900)

    with pytest.raises(supervisor.SupervisorError, match="memory_headroom_unavailable"):
        supervisor._available_memory_bytes()


def test_supervisor_terminates_child_when_rss_budget_is_exceeded(tmp_path, monkeypatch):
    monkeypatch.setattr(supervisor, "_sample_rss", lambda _pid: 100)

    result = launch(
        tmp_path,
        [sys.executable, "-c", "import time; time.sleep(10)"],
        run_limits=limits(rss_bytes=50),
    )

    assert result["failure_reason"] == "rss_budget_exceeded"
    assert result["reaped"] is True


def test_supervisor_terminates_child_when_report_log_budget_is_exceeded(tmp_path):
    result = launch(
        tmp_path,
        [sys.executable, "-c", "import sys; sys.stdout.write('x' * 5000); sys.stdout.flush()"],
        run_limits=limits(report_bytes=4096),
    )

    assert result["failure_reason"] == "report_budget_exceeded"
    assert result["reaped"] is True


def test_supervisor_preflight_failure_does_not_start_child(tmp_path, monkeypatch):
    marker = tmp_path / "child-started"
    monkeypatch.setattr(
        supervisor,
        "_available_memory_bytes",
        lambda: (_ for _ in ()).throw(supervisor.SupervisorError("memory_headroom_unavailable")),
    )

    result = launch(
        tmp_path,
        [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"],
    )

    assert result["failure_reason"] == "memory_headroom_unavailable"
    assert not marker.exists()


def test_attempt_state_is_initialized_once_with_shared_fixed_budgets(tmp_path, monkeypatch):
    state_path = tmp_path / "budget-state.json"
    monkeypatch.setattr(supervisor, "_current_boot_id", lambda: "boot-test")
    monkeypatch.setattr(supervisor.time, "monotonic", lambda: 100.0)

    state = supervisor._initialize_attempt_state(state_path)

    assert state == {
        "state_version": 1,
        "workflow_id": state["workflow_id"],
        "task_id": "t_322319de",
        "boot_id": "boot-test",
        "started_monotonic": 100.0,
        "task_deadline_monotonic": 3700.0,
        "aggregate_limit_seconds": 600.0,
        "aggregate_consumed_seconds": 0.0,
        "stages_completed": 0,
        "in_flight": None,
    }
    original = state_path.read_bytes()

    with pytest.raises(supervisor.SupervisorError, match="budget_state_already_exists"):
        supervisor._initialize_attempt_state(state_path)

    assert state_path.read_bytes() == original


def test_supervisor_rejects_incomplete_or_wrong_boot_shared_state_before_spawn(tmp_path):
    state_path = tmp_path / "budget-state.json"
    marker = tmp_path / "child-started"
    state_path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(supervisor.SupervisorError, match="budget_state_unusable"):
        _supervise(
            [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"],
            cwd=tmp_path,
            env=os.environ.copy(),
            stage="tests",
            log_path=tmp_path / "state.log",
            report_path=tmp_path / "state.json",
            output_path=None,
            limits=limits(),
            attempt_state=state_path,
        )

    assert not marker.exists()


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        ("nonzero", "child_nonzero_exit"),
        ("timeout", "wall_timeout"),
        ("rss", "rss_budget_exceeded"),
        ("output", "output_budget_exceeded"),
        ("report", "report_budget_exceeded"),
        ("disk", "disk_headroom_below_budget"),
        ("preflight", "memory_headroom_unavailable"),
    ],
)
def test_verify_failure_matrix_does_not_mutate_inputs(tmp_path, monkeypatch, failure, expected):
    from types import SimpleNamespace

    input_root = tmp_path / "saved-input"
    input_root.mkdir()
    (input_root / "frozen.json").write_bytes(b"frozen input\n")
    before = {p.relative_to(input_root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in input_root.rglob("*") if p.is_file()}
    child = [sys.executable, "-B", "-c", "print('read-only')"]
    run_limits = limits()
    if failure == "nonzero":
        child = [sys.executable, "-B", "-c", "raise SystemExit(7)"]
    elif failure == "timeout":
        child = [sys.executable, "-B", "-c", "import time; time.sleep(5)"]
        run_limits = limits(wall_seconds=0.12)
    elif failure == "rss":
        monkeypatch.setattr(supervisor, "_sample_rss", lambda _pid: 100)
        child = [sys.executable, "-B", "-c", "import time; time.sleep(5)"]
        run_limits = limits(rss_bytes=50)
    elif failure == "output":
        run_limits = limits(output_bytes=1)
    elif failure == "report":
        child = [sys.executable, "-B", "-c", "import sys; sys.stdout.write('x' * 5000); sys.stdout.flush()"]
        run_limits = limits(report_bytes=4096)
    elif failure == "disk":
        monkeypatch.setattr(supervisor.shutil, "disk_usage", lambda _path: SimpleNamespace(free=0))
        run_limits = limits(minimum_disk_free_bytes=1)
    elif failure == "preflight":
        monkeypatch.setattr(supervisor, "_available_memory_bytes", lambda: (_ for _ in ()).throw(supervisor.SupervisorError("memory_headroom_unavailable")))
    result = launch(tmp_path, child, output_path=input_root, run_limits=run_limits)
    after = {p.relative_to(input_root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in input_root.rglob("*") if p.is_file()}
    assert result["failure_reason"] == expected
    assert after == before


def test_attempt_state_lock_and_wrong_boot_fail_before_spawn(tmp_path):
    state_path = tmp_path / "budget-state.json"
    state = supervisor._initialize_attempt_state(state_path)
    marker = tmp_path / "state-child"
    state["boot_id"] = "wrong-boot"
    supervisor._write_attempt_state(state_path, state)
    with pytest.raises(supervisor.SupervisorError, match="budget_state_unusable"):
        _supervise([sys.executable, "-B", "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"], cwd=tmp_path, env=os.environ.copy(), stage="tests", log_path=tmp_path / "wrong-boot.log", report_path=tmp_path / "wrong-boot.json", output_path=None, limits=limits(), attempt_state=state_path)
    assert not marker.exists()

    state["boot_id"] = supervisor._current_boot_id()
    supervisor._write_attempt_state(state_path, state)
    lock_fd = supervisor._acquire_budget_lock(state_path)
    try:
        with pytest.raises(supervisor.SupervisorError, match="budget_state_locked"):
            _supervise([sys.executable, "-B", "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"], cwd=tmp_path, env=os.environ.copy(), stage="tests", log_path=tmp_path / "locked.log", report_path=tmp_path / "locked.json", output_path=None, limits=limits(), attempt_state=state_path)
    finally:
        os.close(lock_fd)
    assert not marker.exists()


def test_shared_budget_is_cumulative_across_stage_invocations(tmp_path):
    state_path = tmp_path / "budget-state.json"
    state = supervisor._initialize_attempt_state(state_path)
    state["aggregate_consumed_seconds"] = 599.8
    supervisor._write_attempt_state(state_path, state)
    first_marker = tmp_path / "first-started"
    first = _supervise([sys.executable, "-B", "-c", f"from pathlib import Path; import time; Path({str(first_marker)!r}).touch(); time.sleep(5)"], cwd=tmp_path, env=os.environ.copy(), stage="tests", log_path=tmp_path / "first.log", report_path=tmp_path / "first.json", output_path=None, limits=limits(wall_seconds=1.0, poll_seconds=0.01, terminate_grace_seconds=0.03), attempt_state=state_path)
    assert first["failure_reason"] == "wall_timeout"
    assert first_marker.is_file()
    after_first = json.loads(state_path.read_text(encoding="utf-8"))
    assert after_first["stages_completed"] == 1
    assert after_first["aggregate_consumed_seconds"] > 600.0

    second_marker = tmp_path / "second-started"
    with pytest.raises(supervisor.SupervisorError, match="aggregate_budget_exhausted"):
        _supervise([sys.executable, "-B", "-c", f"from pathlib import Path; Path({str(second_marker)!r}).touch()"], cwd=tmp_path, env=os.environ.copy(), stage="verify", log_path=tmp_path / "second.log", report_path=tmp_path / "second.json", output_path=None, limits=limits(), attempt_state=state_path)
    assert not second_marker.exists()


def test_shared_budget_survives_independent_supervisor_processes(tmp_path):
    state_path = tmp_path / "budget-state.json"
    state = supervisor._initialize_attempt_state(state_path)
    state["aggregate_consumed_seconds"] = 599.0
    supervisor._write_attempt_state(state_path, state)

    worker = tmp_path / "supervisor_process.py"
    worker.write_text(
        textwrap.dedent(
            """\
            import dataclasses
            import json
            import os
            import sys
            from pathlib import Path

            sys.path.insert(0, str(Path.cwd()))
            from generation_io import supervisor

            state_path, marker, log, report, child = map(Path, sys.argv[1:6])
            mode, wall_seconds = sys.argv[6], float(sys.argv[7])
            state = supervisor._load_attempt_state(state_path)
            if mode == "both-exhausted":
                supervisor.time.monotonic = lambda: state["task_deadline_monotonic"] + 1
            limits = dataclasses.replace(
                supervisor._PRODUCTION_LIMITS,
                wall_seconds=wall_seconds,
                poll_seconds=0.005,
                terminate_grace_seconds=0.01,
            )
            try:
                result = supervisor._supervise(
                    [sys.executable, "-B", str(child), str(marker), "5"],
                    cwd=Path.cwd(),
                    env={
                        key: value
                        for key, value in os.environ.items()
                        if key != "PYTHONPATH"
                    },
                    stage="tests",
                    log_path=log,
                    report_path=report,
                    output_path=None,
                    limits=limits,
                    attempt_state=state_path,
                    resource_root=state_path.parent,
                )
            except supervisor.SupervisorError as exc:
                print(json.dumps({"error": str(exc)}))
            else:
                print(json.dumps({"result": result}))
            """
        ),
        encoding="utf-8",
    )
    child = tmp_path / "marker_child.py"
    child.write_text(
        "import sys, time\nfrom pathlib import Path\nPath(sys.argv[1]).touch()\ntime.sleep(float(sys.argv[2]))\n",
        encoding="utf-8",
    )

    def run_process(index: int, mode: str = "run", wall_seconds: float = 1.0):
        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                str(worker),
                str(state_path),
                str(tmp_path / f"marker-{index}"),
                str(tmp_path / f"stage-{index}.log"),
                str(tmp_path / f"stage-{index}.json"),
                str(child),
                mode,
                str(wall_seconds),
            ],
            cwd=supervisor.PACKAGE,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        return json.loads(completed.stdout)

    first = run_process(1, wall_seconds=0.12)["result"]
    first_report = json.loads((tmp_path / "stage-1.json").read_text(encoding="utf-8"))
    after_first = json.loads(state_path.read_text(encoding="utf-8"))
    assert first["failure_reason"] == "wall_timeout"
    assert first["aggregate_before"] == 599.0
    assert first["stages_completed"] == 1
    assert after_first["aggregate_consumed_seconds"] == first["aggregate_after"]
    assert first["aggregate_after"] > 599.0
    assert after_first["in_flight"] is None
    assert first["deadline_monotonic"] <= first["started_monotonic"] + 0.12
    assert first_report["reaped"] is True
    assert first_report["finalized"] is True
    assert first_report["exit_code"] is not None

    if after_first["aggregate_consumed_seconds"] >= 600.0:
        second = run_process(2, wall_seconds=5.0)
        assert second == {"error": "aggregate_budget_exhausted"}
        after_second = json.loads(state_path.read_text(encoding="utf-8"))
        assert after_second == after_first
        assert not (tmp_path / "marker-2").exists()
    else:
        second = run_process(2, wall_seconds=5.0)["result"]
        second_report = json.loads((tmp_path / "stage-2.json").read_text(encoding="utf-8"))
        after_second = json.loads(state_path.read_text(encoding="utf-8"))
        assert second["failure_reason"] == "wall_timeout"
        assert second["aggregate_before"] == after_first["aggregate_consumed_seconds"]
        assert second["deadline_monotonic"] <= second["started_monotonic"] + (600.0 - second["aggregate_before"])
        assert second["stages_completed"] == 2
        assert second["aggregate_after"] == after_second["aggregate_consumed_seconds"] > 600.0
        assert after_second["in_flight"] is None
        assert second_report["reaped"] is True
        assert second_report["finalized"] is True

    aggregate_exhausted = run_process(3)
    assert aggregate_exhausted == {"error": "aggregate_budget_exhausted"}
    assert not (tmp_path / "marker-3").exists()
    assert json.loads(state_path.read_text(encoding="utf-8")) == after_second

    both_exhausted = run_process(4, mode="both-exhausted")
    assert both_exhausted == {"error": "task_budget_exhausted"}
    assert not (tmp_path / "marker-4").exists()
    assert json.loads(state_path.read_text(encoding="utf-8")) == after_second


def test_late_supervisor_failure_invalidates_complete_when_report_is_refused(tmp_path):
    output = tmp_path / "late-output"
    attempt = tmp_path / "attempt-01"
    attempt.mkdir()
    state_path = tmp_path / "budget-state.json"
    supervisor._initialize_attempt_state(state_path)
    invocation = "late-invocation"
    child_code = (
        "import hashlib,json,sys; from pathlib import Path; output=Path(sys.argv[1]); attempt=Path(sys.argv[2]); inv=sys.argv[3]; "
        "output.mkdir(); complete=output/'generation-manifest.json'; complete.write_bytes(b'{\\\"complete\\\":true}\\n'); "
        "root=output.stat(); info=complete.stat(); body=complete.read_bytes(); base={'receipt_version':1,'invocation_id':inv,'output_path':str(output.resolve()),'root_st_dev':root.st_dev,'root_st_ino':root.st_ino}; "
        "(attempt/'ownership.json').write_text(json.dumps(base),encoding='utf-8'); pub=base|{'complete_path':str(complete.resolve()),'complete_st_dev':info.st_dev,'complete_st_ino':info.st_ino,'complete_bytes':len(body),'complete_sha256':hashlib.sha256(body).hexdigest()}; "
        "(attempt/'published-files.json').write_text(json.dumps(pub),encoding='utf-8')"
    )
    with pytest.raises(supervisor.SupervisorError, match="budget_report_exceeded"):
        _supervise([sys.executable, "-B", "-c", child_code, str(output), str(attempt), invocation], cwd=tmp_path, env=os.environ.copy(), stage="generate", log_path=attempt / "generate.log", report_path=attempt / "generate.json", output_path=output, limits=limits(report_bytes=2048), attempt_state=state_path, ownership_receipt=attempt / "ownership.json", published_receipt=attempt / "published-files.json", invocation_id=invocation)
    assert not (output / "generation-manifest.json").exists()
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["in_flight"] is None
    assert state["stages_completed"] == 1
    assert state["aggregate_consumed_seconds"] > 0


@pytest.mark.parametrize("failure", ["missing", "in_flight", "task_expired", "aggregate_exhausted"])
def test_supervisor_refuses_missing_or_exhausted_shared_budget_before_spawn(tmp_path, monkeypatch, failure):
    from types import SimpleNamespace

    state_path = tmp_path / "budget-state.json"
    marker = tmp_path / "forbidden-child"
    if failure != "missing":
        state = supervisor._initialize_attempt_state(state_path)
        if failure == "in_flight":
            state["in_flight"] = {"stage": "tests", "invocation_id": "orphan"}
        elif failure == "aggregate_exhausted":
            state["aggregate_consumed_seconds"] = 600.0
        supervisor._write_attempt_state(state_path, state)
        if failure == "task_expired":
            monkeypatch.setattr(supervisor, "time", SimpleNamespace(monotonic=lambda: state["task_deadline_monotonic"] + 1))
    expected = {
        "missing": "budget_state_unusable",
        "in_flight": "budget_state_unusable",
        "task_expired": "task_budget_exhausted",
        "aggregate_exhausted": "aggregate_budget_exhausted",
    }[failure]
    with pytest.raises(supervisor.SupervisorError, match=expected):
        _supervise([sys.executable, "-B", "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"], cwd=tmp_path, env=os.environ.copy(), stage="tests", log_path=tmp_path / f"{failure}.log", report_path=tmp_path / f"{failure}.json", output_path=None, limits=limits(), attempt_state=state_path)
    assert not marker.exists()


def test_cli_attempt_initialization_is_explicit_and_one_time(tmp_path, monkeypatch):
    state_path = tmp_path / "budget-state.json"
    monkeypatch.setattr(supervisor, "ATTEMPT_STATE", state_path)
    assert supervisor.main(["--init-attempt", "--attempt-state", str(state_path)]) == 0
    original = state_path.read_bytes()
    assert json.loads(original)["aggregate_consumed_seconds"] == 0.0
    assert supervisor.main(["--init-attempt", "--attempt-state", str(state_path)]) == 2
    assert state_path.read_bytes() == original


def test_late_supervisor_failure_invalidates_only_receipt_owned_complete(tmp_path):
    output = tmp_path / "fresh-output"
    output.mkdir()
    complete = output / "generation-manifest.json"
    complete.write_bytes(b"complete\n")
    receipt = tmp_path / "ownership.json"
    published = tmp_path / "published-files.json"
    root = output.stat()
    file_stat = complete.stat()
    invocation_id = "invocation-test"
    receipt.write_text(
        json.dumps(
            {
                "receipt_version": 1,
                "invocation_id": invocation_id,
                "output_path": str(output.resolve()),
                "root_st_dev": root.st_dev,
                "root_st_ino": root.st_ino,
            }
        ),
        encoding="utf-8",
    )
    published.write_text(
        json.dumps(
            {
                "receipt_version": 1,
                "invocation_id": invocation_id,
                "output_path": str(output.resolve()),
                "root_st_dev": root.st_dev,
                "root_st_ino": root.st_ino,
                "complete_path": str(complete.resolve()),
                "complete_st_dev": file_stat.st_dev,
                "complete_st_ino": file_stat.st_ino,
                "complete_bytes": complete.stat().st_size,
                "complete_sha256": hashlib.sha256(complete.read_bytes()).hexdigest(),
            }
        ),
        encoding="utf-8",
    )

    assert supervisor._invalidate_owned_complete(output, receipt, published, invocation_id)
    assert not complete.exists()


def test_late_supervisor_failure_preserves_complete_without_matching_receipts(tmp_path):
    output = tmp_path / "unknown-output"
    output.mkdir()
    complete = output / "generation-manifest.json"
    complete.write_bytes(b"unknown writer\n")

    assert not supervisor._invalidate_owned_complete(
        output, tmp_path / "missing-ownership.json", tmp_path / "missing-published.json", "other"
    )
    assert complete.read_bytes() == b"unknown writer\n"


def _f1_owned_sleep_child(tmp_path: Path, *, sleep_seconds: float):
    output = tmp_path / "fresh-output"
    output.mkdir()
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    marker = tmp_path / "child.pid"
    state_path = tmp_path / "budget-state.json"
    supervisor._initialize_attempt_state(state_path)
    invocation_id = "f1-owned-child"
    child_code = textwrap.dedent(
        """\
        import hashlib, json, os, sys, time
        from pathlib import Path
        output, attempt, marker = map(Path, sys.argv[1:4])
        invocation_id, sleep_seconds = sys.argv[4], float(sys.argv[5])
        complete = output / "generation-manifest.json"
        body = b'{"complete":true}\\n'
        complete.write_bytes(body)
        root = output.stat()
        owner = {
            "receipt_version": 1,
            "invocation_id": invocation_id,
            "output_path": str(output.resolve()),
            "root_st_dev": root.st_dev,
            "root_st_ino": root.st_ino,
        }
        (attempt / "ownership.json").write_text(json.dumps(owner), encoding="utf-8")
        info = complete.stat()
        published = owner | {
            "complete_path": str(complete.resolve()),
            "complete_st_dev": info.st_dev,
            "complete_st_ino": info.st_ino,
            "complete_bytes": len(body),
            "complete_sha256": hashlib.sha256(body).hexdigest(),
        }
        (attempt / "published-files.json").write_text(json.dumps(published), encoding="utf-8")
        marker_path = Path(marker)
        marker_temp = marker_path.with_name(marker_path.name + ".tmp")
        marker_temp.write_text(str(os.getpid()), encoding="ascii")
        marker_temp.replace(marker_path)
        time.sleep(sleep_seconds)
        """
    )
    return (
        output,
        attempt,
        marker,
        state_path,
        invocation_id,
        [sys.executable, "-B", "-c", child_code, str(output), str(attempt), str(marker), invocation_id, str(sleep_seconds)],
    )


def _wait_for_child_marker(marker: Path) -> int:
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if marker.exists():
            return int(marker.read_text(encoding="ascii"))
        time.sleep(0.01)
    raise AssertionError("child did not publish its ready marker")


def _pid_is_live(pid: int) -> bool:
    try:
        stat_line = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except FileNotFoundError:
        return False
    state = stat_line.rsplit(")", 1)[1].split()[0]
    return state != "Z"


def _cleanup_test_child(marker: Path) -> None:
    if not marker.exists():
        return
    pid = int(marker.read_text(encoding="ascii"))
    if _pid_is_live(pid):
        try:
            if os.getpgid(pid) == pid:
                os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + 2.0
        while _pid_is_live(pid) and time.monotonic() < deadline:
            time.sleep(0.01)


@pytest.mark.parametrize(
    ("failure", "sleep_seconds"),
    [
        ("monitor_disk", 10.0),
        ("monitor_tree", 10.0),
        ("finalize_write", 0.05),
        ("log_flush", 0.05),
        ("log_fsync", 0.05),
        ("report_construction", 0.05),
        ("report_publication", 0.05),
        ("termination_unproven", 10.0),
    ],
)
def test_post_spawn_failures_reap_child_preserve_budget_truth_and_invalidate_owned_complete(
    tmp_path, monkeypatch, failure, sleep_seconds
):
    output, attempt, marker, state_path, invocation_id, child = _f1_owned_sleep_child(
        tmp_path, sleep_seconds=sleep_seconds
    )
    log_path = attempt / "generate.log"
    report_path = attempt / "generate.json"
    original_disk_usage = supervisor.shutil.disk_usage
    original_tree_size = supervisor._tree_size

    if failure == "monitor_disk":
        calls = 0

        def disk_usage(path):
            nonlocal calls
            calls += 1
            if calls == 2:
                _wait_for_child_marker(marker)
                raise OSError("injected monitor disk failure")
            return original_disk_usage(path)

        monkeypatch.setattr(supervisor.shutil, "disk_usage", disk_usage)
    elif failure == "monitor_tree":
        injected = False

        def tree_size(path):
            nonlocal injected
            if not injected:
                injected = True
                _wait_for_child_marker(marker)
                raise OSError("injected monitor tree failure")
            return original_tree_size(path)

        monkeypatch.setattr(supervisor, "_tree_size", tree_size)
    elif failure == "finalize_write":
        original_write_state = supervisor._write_attempt_state

        def write_state(path, state):
            if state.get("in_flight") is None and state.get("stages_completed", 0) == 1:
                raise OSError("injected budget finalize write failure")
            return original_write_state(path, state)

        monkeypatch.setattr(supervisor, "_write_attempt_state", write_state)
    elif failure == "log_flush":
        original_path_open = Path.open

        class FlushFailureStream:
            def __init__(self, stream):
                self.stream = stream

            def __getattr__(self, name):
                return getattr(self.stream, name)

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return self.stream.__exit__(exc_type, exc_value, traceback)

            def flush(self):
                raise OSError("injected log flush failure")

        def path_open(path, *args, **kwargs):
            stream = original_path_open(path, *args, **kwargs)
            if Path(path) == log_path and args and args[0] == "xb":
                return FlushFailureStream(stream)
            return stream

        monkeypatch.setattr(Path, "open", path_open)
    elif failure == "log_fsync":
        original_fsync = os.fsync

        def fsync(fd):
            try:
                target = Path(os.readlink(f"/proc/self/fd/{fd}"))
            except OSError:
                target = None
            if target == log_path:
                raise OSError("injected log fsync failure")
            return original_fsync(fd)

        monkeypatch.setattr(supervisor.os, "fsync", fsync)
    elif failure == "report_construction":
        original_dumps = supervisor.json.dumps

        def dumps(value, *args, **kwargs):
            if isinstance(value, dict) and value.get("stage") == "generate":
                raise RuntimeError("injected report construction failure")
            return original_dumps(value, *args, **kwargs)

        monkeypatch.setattr(supervisor.json, "dumps", dumps)
    elif failure == "report_publication":
        original_write_exclusive = supervisor._write_exclusive

        def write_exclusive(path, contents):
            if Path(path) == report_path:
                raise OSError("injected report publication failure")
            return original_write_exclusive(path, contents)

        monkeypatch.setattr(supervisor, "_write_exclusive", write_exclusive)
    elif failure == "termination_unproven":
        original_terminate = supervisor._terminate_process_group

        def terminate_then_fail(process, grace_seconds):
            original_terminate(process, grace_seconds)
            raise OSError("injected termination proof failure")

        monkeypatch.setattr(supervisor, "_terminate_process_group", terminate_then_fail)

    caught = None
    result = None
    child_was_live_after_call = None
    try:
        try:
            result = _supervise(
                child,
                cwd=tmp_path,
                env=os.environ.copy(),
                stage="generate",
                log_path=log_path,
                report_path=report_path,
                output_path=output,
                limits=(
                    limits(wall_seconds=0.15, poll_seconds=0.01, terminate_grace_seconds=0.05)
                    if failure == "termination_unproven"
                    else limits(wall_seconds=3.0, poll_seconds=0.01, terminate_grace_seconds=0.05)
                ),
                attempt_state=state_path,
                invocation_id=invocation_id,
                ownership_receipt=attempt / "ownership.json",
                published_receipt=attempt / "published-files.json",
            )
        except (OSError, RuntimeError, supervisor.SupervisorError) as exc:
            caught = exc
        pid = _wait_for_child_marker(marker)
        child_was_live_after_call = _pid_is_live(pid)
    finally:
        _cleanup_test_child(marker)

    pid = _wait_for_child_marker(marker)
    assert not child_was_live_after_call, f"child {pid} remained live after supervisor exit"
    assert not _pid_is_live(pid), f"test cleanup could not stop child {pid}"
    assert not (output / "generation-manifest.json").exists()
    if failure == "termination_unproven":
        assert result is None
        assert caught is not None
        assert str(caught) == "wall_timeout; injected termination proof failure"
    elif result is not None:
        assert result["status"] == "failed"
        assert result["reaped"] is True
        expected_reason = {
            "monitor_disk": "injected monitor disk failure",
            "monitor_tree": "injected monitor tree failure",
        }[failure]
        assert result["failure_reason"] == expected_reason
    else:
        assert caught is not None
        expected_error = {
            "finalize_write": "injected budget finalize write failure",
            "log_flush": "injected log flush failure",
            "log_fsync": "injected log fsync failure",
            "report_construction": "injected report construction failure",
            "report_publication": "budget_report_write_failed",
        }[failure]
        assert str(caught) == expected_error

    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    if failure in {"finalize_write", "termination_unproven"}:
        assert persisted["in_flight"] is not None
        assert persisted["stages_completed"] == 0
    else:
        assert persisted["in_flight"] is None
        assert persisted["stages_completed"] == 1


def test_terminate_process_group_rejects_group_remaining_after_leader_reap(monkeypatch):
    class ReapedProcess:
        pid = 987654
        returncode = 0

        def __init__(self):
            self.wait_timeouts = []

        def wait(self, timeout=None):
            self.wait_timeouts.append(timeout)
            return self.returncode

    fake_process = ReapedProcess()
    process = cast(subprocess.Popen[bytes], fake_process)
    signals = []
    clock = [0.0]

    def fake_killpg(pgid, signum):
        assert pgid == process.pid
        signals.append(signum)

    def group_still_exists(pgid):
        assert pgid == process.pid
        return True

    def monotonic():
        return clock[0]

    def sleep(seconds):
        assert seconds > 0
        clock[0] += seconds

    monkeypatch.setattr(supervisor.os, "killpg", fake_killpg)
    monkeypatch.setattr(supervisor, "_process_group_exists", group_still_exists)
    monkeypatch.setattr(supervisor.time, "monotonic", monotonic)
    monkeypatch.setattr(supervisor.time, "sleep", sleep)

    with pytest.raises(supervisor.SupervisorError, match="^child_reap_failed$"):
        supervisor._terminate_process_group(process, grace_seconds=0.1)

    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert process.wait_timeouts == [0.1]


def test_terminate_process_group_succeeds_after_group_disappears(monkeypatch):
    class ReapedProcess:
        pid = 987655
        returncode = 0

        def __init__(self):
            self.wait_timeouts = []

        def wait(self, timeout=None):
            self.wait_timeouts.append(timeout)
            return self.returncode

    fake_process = ReapedProcess()
    process = cast(subprocess.Popen[bytes], fake_process)
    signals = []
    clock = [0.0]

    def fake_killpg(pgid, signum):
        assert pgid == process.pid
        signals.append(signum)

    def group_exists_until_kill(pgid):
        assert pgid == process.pid
        return signal.SIGKILL not in signals

    def monotonic():
        return clock[0]

    def sleep(seconds):
        assert seconds > 0
        clock[0] += seconds

    monkeypatch.setattr(supervisor.os, "killpg", fake_killpg)
    monkeypatch.setattr(supervisor, "_process_group_exists", group_exists_until_kill)
    monkeypatch.setattr(supervisor.time, "monotonic", monotonic)
    monkeypatch.setattr(supervisor.time, "sleep", sleep)

    supervisor._terminate_process_group(process, grace_seconds=0.1)

    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert process.wait_timeouts == [0.1]
    assert not group_exists_until_kill(process.pid)


def test_unproven_group_termination_preserves_budget_in_flight(tmp_path, monkeypatch):
    output, attempt, marker, state_path, invocation_id, child = _f1_owned_sleep_child(
        tmp_path, sleep_seconds=10.0
    )
    log_path = attempt / "generate.log"
    report_path = attempt / "generate.json"
    monkeypatch.setattr(supervisor, "_process_group_exists", lambda pgid: True)

    try:
        with pytest.raises(supervisor.SupervisorError, match="child_reap_failed"):
            _supervise(
                child,
                cwd=tmp_path,
                env=os.environ.copy(),
                stage="generate",
                log_path=log_path,
                report_path=report_path,
                output_path=output,
                limits=limits(wall_seconds=0.15, poll_seconds=0.01, terminate_grace_seconds=0.03),
                attempt_state=state_path,
                invocation_id=invocation_id,
                ownership_receipt=attempt / "ownership.json",
                published_receipt=attempt / "published-files.json",
            )
    finally:
        _cleanup_test_child(marker)

    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["in_flight"] is not None
    assert persisted["stages_completed"] == 0
    assert persisted["aggregate_consumed_seconds"] == 0.0
    assert not report_path.exists()
    assert not (output / "generation-manifest.json").exists()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires fork")
def test_terminate_process_group_kills_term_ignoring_forked_descendant(tmp_path):
    marker = tmp_path / "descendant.pid"
    child_code = textwrap.dedent(
        """\
        import os, signal, sys, time
        from pathlib import Path
        marker = Path(sys.argv[1])
        descendant = os.fork()
        if descendant == 0:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            marker.write_text(str(os.getpid()), encoding="ascii")
            while True:
                time.sleep(0.05)
        os._exit(0)
        """
    )
    process = subprocess.Popen(
        [sys.executable, "-B", "-c", child_code, str(marker)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )
    descendant_pid = None
    termination_error = None
    try:
        deadline = time.monotonic() + 3.0
        marker_contents = ""
        while time.monotonic() < deadline and not marker_contents.isdigit():
            if marker.exists():
                marker_contents = marker.read_text(encoding="ascii")
            if not marker_contents.isdigit():
                time.sleep(0.01)
        assert marker_contents.isdigit(), "forked descendant did not publish its pid"
        descendant_pid = int(marker_contents)
        process.wait(timeout=2.0)

        try:
            supervisor._terminate_process_group(process, grace_seconds=0.1)
        except supervisor.SupervisorError as exc:
            termination_error = exc

        assert termination_error is None or str(termination_error) == "child_reap_failed"
        assert not _pid_is_live(descendant_pid)
        if termination_error is None:
            assert not supervisor._process_group_exists(process.pid)
        else:
            assert supervisor._process_group_exists(process.pid)
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if process.returncode is None:
            process.wait(timeout=2.0)
        if descendant_pid is not None:
            deadline = time.monotonic() + 2.0
            while _pid_is_live(descendant_pid) and time.monotonic() < deadline:
                time.sleep(0.01)
            assert not _pid_is_live(descendant_pid)


def test_terminate_process_group_rejects_unqueryable_remaining_group(monkeypatch):
    class ReapedProcess:
        pid = 987656
        returncode = 0

        def __init__(self):
            self.wait_timeouts = []

        def wait(self, timeout=None):
            self.wait_timeouts.append(timeout)
            return self.returncode

    fake_process = ReapedProcess()
    process = cast(subprocess.Popen[bytes], fake_process)
    signals = []
    clock = [0.0]

    def fake_killpg(pgid, signum):
        assert pgid == process.pid
        signals.append(signum)

    def group_becomes_unqueryable_after_kill(pgid):
        assert pgid == process.pid
        if signal.SIGKILL in signals:
            raise OSError("injected process-group query failure")
        return True

    def monotonic():
        return clock[0]

    def sleep(seconds):
        assert seconds > 0
        clock[0] += seconds

    monkeypatch.setattr(supervisor.os, "killpg", fake_killpg)
    monkeypatch.setattr(supervisor, "_process_group_exists", group_becomes_unqueryable_after_kill)
    monkeypatch.setattr(supervisor.time, "monotonic", monotonic)
    monkeypatch.setattr(supervisor.time, "sleep", sleep)

    with pytest.raises(supervisor.SupervisorError, match="^child_reap_failed$"):
        supervisor._terminate_process_group(process, grace_seconds=0.1)

    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert fake_process.wait_timeouts == [0.1]
