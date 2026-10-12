from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from generation_io import portable_artifacts, supervisor
from generation_io.portable_artifacts import PortableArtifactError, generate_portable_artifact


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = Path(__file__).parent / "fixtures" / "synthetic"


def _run_supervisor(cwd: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    current_path = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(PACKAGE_ROOT), current_path) if part
    )
    environment.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
            "OMP_NUM_THREADS": "1",
            "OPENBLAS_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
            "NUMEXPR_NUM_THREADS": "1",
        }
    )
    return subprocess.run(
        [sys.executable, "-B", "-m", "generation_io.supervisor", *arguments],
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )


def _json_stdout(result: subprocess.CompletedProcess[str]) -> dict:
    return json.loads(result.stdout)


def _hash_tree(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _tree_snapshot(root: Path) -> dict[str, tuple[str, str | None]]:
    snapshot = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            snapshot[relative] = ("directory", None)
        elif path.is_file():
            snapshot[relative] = ("file", hashlib.sha256(path.read_bytes()).hexdigest())
        else:
            snapshot[relative] = ("other", None)
    return snapshot


def test_subprocess_init_generate_verify_resume_verify_uses_portable_paths_and_shared_budget(
    tmp_path: Path,
):
    data_dir = tmp_path / "external-data"
    shutil.copytree(FIXTURE_DIR, data_dir)
    state_dir = tmp_path / "supervisor-state"
    generated = tmp_path / "generated"
    resumed = tmp_path / "resumed"
    input_before = _hash_tree(data_dir)

    initialized = _run_supervisor(tmp_path, "--init-attempt", "--state-dir", str(state_dir))
    assert initialized.returncode == 0, initialized.stderr
    assert _json_stdout(initialized)["status"] == "initialized"

    first = _run_supervisor(
        tmp_path,
        "--stage", "generate",
        "--state-dir", str(state_dir),
        "--data-dir", str(data_dir),
        "--input-manifest", "input-manifest.json",
        "--region-id", "synthetic:rectangle",
        "--output-dir", str(generated),
    )
    assert first.returncode == 0, first.stderr
    generated_manifest = Path(_json_stdout(first)["manifest"])
    original_output = _hash_tree(generated)

    verified = _run_supervisor(
        tmp_path,
        "--stage", "verify",
        "--state-dir", str(state_dir),
        "--manifest", str(generated_manifest),
    )
    assert verified.returncode == 0, verified.stderr

    resumed_result = _run_supervisor(
        tmp_path,
        "--stage", "resume",
        "--state-dir", str(state_dir),
        "--manifest", str(generated_manifest),
        "--output-dir", str(resumed),
    )
    assert resumed_result.returncode == 0, resumed_result.stderr
    resumed_manifest = Path(_json_stdout(resumed_result)["manifest"])

    resumed_verified = _run_supervisor(
        tmp_path,
        "--stage", "verify",
        "--state-dir", str(state_dir),
        "--manifest", str(resumed_manifest),
    )
    assert resumed_verified.returncode == 0, resumed_verified.stderr

    state = json.loads((state_dir / "budget-state.json").read_text(encoding="utf-8"))
    assert state["stages_completed"] == 4
    assert 0 < state["aggregate_consumed_seconds"] < 600
    assert state["in_flight"] is None
    assert _hash_tree(data_dir) == input_before
    assert _hash_tree(generated) == original_output

    attempts = sorted((state_dir / "attempts").glob("attempt-*"))
    assert len(attempts) == 4
    attempts_by_stage = {
        json.loads((attempt / f"{stage}.json").read_text(encoding="utf-8"))["stage"]: attempt
        for attempt in attempts
        for stage in ("generate", "verify", "resume")
        if (attempt / f"{stage}.json").is_file()
    }
    assert set(attempts_by_stage) == {"generate", "verify", "resume"}
    generated_attempt = attempts_by_stage["generate"]
    resumed_attempt = attempts_by_stage["resume"]
    assert (generated_attempt / "ownership.json").is_file()
    assert (generated_attempt / "published-files.json").is_file()
    assert (resumed_attempt / "ownership.json").is_file()
    assert (resumed_attempt / "published-files.json").is_file()
    for attempt in attempts:
        stage = next(
            stage
            for stage in ("generate", "verify", "resume")
            if (attempt / f"{stage}.json").is_file()
        )
        if stage == "verify":
            assert not (attempt / "ownership.json").exists()
            assert not (attempt / "published-files.json").exists()

    assert supervisor._invalidate_owned_complete(
        generated,
        generated_attempt / "ownership.json",
        generated_attempt / "published-files.json",
        json.loads((generated_attempt / "ownership.json").read_text(encoding="utf-8"))["invocation_id"],
    )
    assert not generated_manifest.exists()
    assert supervisor._invalidate_owned_complete(
        resumed,
        resumed_attempt / "ownership.json",
        resumed_attempt / "published-files.json",
        json.loads((resumed_attempt / "ownership.json").read_text(encoding="utf-8"))["invocation_id"],
    )
    assert not resumed_manifest.exists()


def test_portable_supervisor_rejects_bad_state_preexisting_output_and_overlapping_roots(
    tmp_path: Path,
):
    data_dir = tmp_path / "data"
    shutil.copytree(FIXTURE_DIR, data_dir)
    state_dir = tmp_path / "state"
    assert _run_supervisor(tmp_path, "--init-attempt", "--state-dir", str(state_dir)).returncode == 0
    output = tmp_path / "already-there"
    output.mkdir()
    prior = output / "keep.txt"
    prior.write_bytes(b"keep exactly\n")
    before = _hash_tree(output)

    preexisting = _run_supervisor(
        tmp_path,
        "--stage", "generate",
        "--state-dir", str(state_dir),
        "--data-dir", str(data_dir),
        "--region-id", "synthetic:rectangle",
        "--output-dir", str(output),
    )
    assert preexisting.returncode == 2
    assert _hash_tree(output) == before
    assert json.loads((state_dir / "budget-state.json").read_text())["stages_completed"] == 0

    overlapping = _run_supervisor(
        tmp_path,
        "--stage", "generate",
        "--state-dir", str(state_dir),
        "--data-dir", str(data_dir),
        "--region-id", "synthetic:rectangle",
        "--output-dir", str(data_dir / "nested-output"),
    )
    assert overlapping.returncode == 2
    assert not (data_dir / "nested-output").exists()

    data_alias = tmp_path / "data-alias"
    data_alias.symlink_to(data_dir, target_is_directory=True)
    symlinked_root = _run_supervisor(
        tmp_path,
        "--stage", "generate",
        "--state-dir", str(state_dir),
        "--data-dir", str(data_alias),
        "--region-id", "synthetic:rectangle",
        "--input-manifest", "input-manifest.json",
        "--output-dir", str(tmp_path / "symlink-output"),
    )
    assert symlinked_root.returncode == 2
    assert not (tmp_path / "symlink-output").exists()

    state_path = state_dir / "budget-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["in_flight"] = {"invocation_id": "orphan"}
    state_path.write_text(json.dumps(state), encoding="utf-8")
    attempts_before = set((state_dir / "attempts").glob("attempt-*")) if (state_dir / "attempts").exists() else set()
    malformed = _run_supervisor(
        tmp_path,
        "--stage", "verify",
        "--state-dir", str(state_dir),
        "--manifest", str(output / "missing-manifest.json"),
    )
    assert malformed.returncode == 2
    attempts_after = set((state_dir / "attempts").glob("attempt-*")) if (state_dir / "attempts").exists() else set()
    assert attempts_after == attempts_before


def test_generate_rejects_state_nested_in_synthetic_data_without_mutating_tree(tmp_path: Path):
    data_dir = tmp_path / "synthetic-data"
    shutil.copytree(FIXTURE_DIR, data_dir)
    state_dir = data_dir / "supervisor-state"
    supervisor._initialize_portable_attempt(state_dir)
    output = tmp_path / "should-not-exist"
    before = _tree_snapshot(data_dir)

    with pytest.raises(supervisor.SupervisorError, match="portable_roots_overlap"):
        supervisor.run_portable_stage(
            "generate",
            state_dir=state_dir,
            data_dir=data_dir,
            input_manifest="input-manifest.json",
            region_id="synthetic:rectangle",
            output=output,
        )

    assert _tree_snapshot(data_dir) == before
    assert not (state_dir / "attempts").exists()
    assert not output.exists()
    assert not list(data_dir.rglob("ownership.json"))
    assert not list(data_dir.rglob("published-files.json"))


@pytest.mark.parametrize("stage", ["verify", "resume"])
def test_verify_and_resume_reject_state_nested_in_old_artifact_without_mutation(
    tmp_path: Path, stage: str
):
    old_artifact = tmp_path / "old-artifact"
    old_artifact.mkdir()
    manifest = old_artifact / "generation-manifest.json"
    manifest.write_text("{}\n", encoding="utf-8")
    state_dir = old_artifact / "supervisor-state"
    supervisor._initialize_portable_attempt(state_dir)
    output = tmp_path / "should-not-exist"
    before = _tree_snapshot(old_artifact)

    with pytest.raises(supervisor.SupervisorError, match="portable_roots_overlap"):
        if stage == "verify":
            supervisor.run_portable_stage(
                "verify", state_dir=state_dir, manifest=manifest
            )
        else:
            supervisor.run_portable_stage(
                "resume", state_dir=state_dir, manifest=manifest, output=output
            )

    assert _tree_snapshot(old_artifact) == before
    assert not (state_dir / "attempts").exists()
    assert not output.exists()
    assert not list(old_artifact.rglob("ownership.json"))
    assert not list(old_artifact.rglob("published-files.json"))


def test_malformed_generate_arguments_do_not_create_attempt_files(tmp_path: Path):
    data_dir = tmp_path / "synthetic-data"
    shutil.copytree(FIXTURE_DIR, data_dir)
    state_dir = tmp_path / "state"
    supervisor._initialize_portable_attempt(state_dir)
    output = tmp_path / "should-not-exist"
    before = _tree_snapshot(state_dir)

    with pytest.raises(
        supervisor.SupervisorError,
        match="generate_requires_data_manifest_region_output",
    ):
        supervisor.run_portable_stage(
            "generate",
            state_dir=state_dir,
            data_dir=data_dir,
            region_id="synthetic:rectangle",
            output=output,
        )

    assert _tree_snapshot(state_dir) == before
    assert not (state_dir / "attempts").exists()
    assert not output.exists()


def test_portable_supervisor_cli_uses_state_dir_without_legacy_evidence_paths(tmp_path: Path):
    result = _run_supervisor(tmp_path, "--init-attempt", "--state-dir", str(tmp_path / "state"))
    assert result.returncode == 0, result.stderr
    payload = _json_stdout(result)
    state_path = tmp_path / "state" / "budget-state.json"
    original_state = state_path.read_bytes()
    repeated = _run_supervisor(tmp_path, "--init-attempt", "--state-dir", str(tmp_path / "state"))
    assert repeated.returncode == 2
    assert state_path.read_bytes() == original_state
    assert payload["state_path"] == str(state_path.absolute())
    assert supervisor.PACKAGE == PACKAGE_ROOT


def test_post_spawn_monitor_exception_reaps_child_group_and_keeps_state_truthful(tmp_path: Path, monkeypatch):
    import time

    state_path = tmp_path / "budget-state.json"
    supervisor._initialize_attempt_state(state_path)
    attempt = tmp_path / "attempt-01"
    attempt.mkdir()
    marker = tmp_path / "child.pid"
    child = [
        sys.executable,
        "-B",
        "-c",
        "import os,sys,time; from pathlib import Path; Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)",
        str(marker),
    ]
    original_tree_size = supervisor._tree_size

    def fail_after_spawn(path: Path) -> int:
        deadline = time.monotonic() + 3
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.exists()
        raise OSError("monitor fault")

    monkeypatch.setattr(supervisor, "_tree_size", fail_after_spawn)
    limits = supervisor._Limits(
        wall_seconds=10,
        rss_bytes=512 * 1024 * 1024,
        minimum_memory_free_bytes=0,
        minimum_disk_free_bytes=0,
        output_bytes=1024,
        report_bytes=1024 * 1024,
        poll_seconds=0.01,
        terminate_grace_seconds=0.1,
    )
    report = supervisor._supervise(
        child,
        cwd=tmp_path,
        env=os.environ.copy(),
        stage="generate",
        log_path=attempt / "stage.log",
        report_path=attempt / "stage.json",
        output_path=tmp_path / "output",
        limits=limits,
        attempt_state=state_path,
        resource_root=tmp_path,
    )
    assert report["status"] == "failed"
    assert report["failure_reason"] == "monitor fault"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["in_flight"] is None
    assert state["stages_completed"] == 1
    assert not supervisor._process_group_exists(int(marker.read_text(encoding="ascii")))
    monkeypatch.setattr(supervisor, "_tree_size", original_tree_size)


def test_portable_manifest_name_rejects_backslash_paths():
    with pytest.raises(supervisor.SupervisorError, match="input_manifest_name_invalid"):
        supervisor._portable_manifest_name(f"folder{chr(92)}manifest.json")


def test_tree_size_ignores_files_disappearing_during_live_output_scan(tmp_path: Path, monkeypatch):
    output = tmp_path / "live-output"
    output.mkdir()
    disappearing = output / ".artifact.tmp"
    retained = output / "artifact.json"
    disappearing.write_bytes(b"temporary bytes")
    retained.write_bytes(b"stable")
    original_stat = Path.stat

    def race_stat(path: Path, *args, **kwargs):
        if path == disappearing:
            raise FileNotFoundError(path)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", race_stat)
    assert supervisor._tree_size(output) == len(b"stable")


def test_portable_receipt_hooks_reject_receipts_under_an_input_root(tmp_path: Path):
    data_dir = tmp_path / "external-data"
    shutil.copytree(FIXTURE_DIR, data_dir)
    output = tmp_path / "output"

    with pytest.raises(PortableArtifactError, match="invalid_ownership_receipt_path"):
        generate_portable_artifact(
            data_dir,
            region_id="synthetic:rectangle",
            output=output,
            ownership_receipt=data_dir / "ownership.json",
            published_receipt=data_dir / "published-files.json",
            invocation_id="caller-controlled",
        )

    assert not output.exists()
    assert not (data_dir / "ownership.json").exists()


def test_published_receipt_failure_removes_only_owned_complete_manifest(tmp_path: Path, monkeypatch):
    data_dir = tmp_path / "external-data"
    shutil.copytree(FIXTURE_DIR, data_dir)
    state_dir = tmp_path / "state"
    attempts = state_dir / "attempts"
    attempt = attempts / ("attempt-" + "a" * 32)
    attempt.mkdir(parents=True)
    state_path = state_dir / "budget-state.json"
    invocation_id = "receipt-failure-test"
    state_path.write_text(
        json.dumps(
            {
                "in_flight": {
                    "stage": "generate",
                    "invocation_id": invocation_id,
                    "owned_evidence_dir": str(attempt.resolve()),
                }
            }
        ),
        encoding="utf-8",
    )
    (attempt / "generate.log").touch()
    output = tmp_path / "generated"
    unowned = output / "unowned.txt"
    original_write = portable_artifacts._write_receipt_atomic

    def fail_published(path: Path, receipt: dict) -> None:
        if path.name == "published-files.json":
            unowned.write_bytes(b"preserve unknown data\n")
            raise OSError("injected receipt publication failure")
        original_write(path, receipt)

    monkeypatch.setattr(portable_artifacts, "_write_receipt_atomic", fail_published)
    with pytest.raises(PortableArtifactError):
        generate_portable_artifact(
            data_dir,
            region_id="synthetic:rectangle",
            output=output,
            ownership_receipt=attempt / "ownership.json",
            published_receipt=attempt / "published-files.json",
            invocation_id=invocation_id,
        )

    assert not (output / "generation-manifest.json").exists()
    assert unowned.read_bytes() == b"preserve unknown data\n"
    assert (attempt / "ownership.json").is_file()
    assert not (attempt / "published-files.json").exists()
