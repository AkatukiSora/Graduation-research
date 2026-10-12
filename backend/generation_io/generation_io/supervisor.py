from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import shutil
import signal
import stat
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Sequence


TEST_AGGREGATE_BUDGET_SECONDS = 600
RSS_BUDGET_BYTES = 512 * 1024 * 1024
MINIMUM_HEADROOM_BYTES = 1024 * 1024 * 1024
OUTPUT_BUDGET_BYTES = 32 * 1024 * 1024
REPORT_BUDGET_BYTES = 8 * 1024 * 1024
POLL_SECONDS = 0.05
TERMINATE_GRACE_SECONDS = 2.0
ROOT = Path("/opt/data/workspaces/graduation-research-g02")
PACKAGE = Path(__file__).resolve().parents[1]
EVIDENCE = Path("/opt/data/workspaces/graduation-research-evidence/unified-work/G02-revision4")
PYTHON = Path("/opt/data/workspaces/graduation-research-evidence/unified-work/P02/.venv/bin/python")
P05 = "/opt/data/workspaces/graduation-research-p05/backend/generation"
CGROUP_FILE = Path("/proc/self/cgroup")
CGROUP_ROOT = Path("/sys/fs/cgroup")
TASK_STARTED_MONOTONIC = time.monotonic()
ATTEMPT_STATE = EVIDENCE / "validation/budget-state.json"
TASK_ID = "t_322319de"
TASK_BUDGET_SECONDS = 3600.0
AGGREGATE_BUDGET_SECONDS = 600.0


class SupervisorError(RuntimeError):
    pass


def _current_boot_id() -> str:
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as exc:
        raise SupervisorError("budget_state_unusable") from exc
    if not value:
        raise SupervisorError("budget_state_unusable")
    return value


def _state_path(path: str | Path) -> Path:
    result = Path(path).absolute()
    _reject_symlink_path(result.parent)
    if result.is_symlink():
        raise SupervisorError("budget_state_unusable")
    return result


def _initialize_attempt_state(path: str | Path) -> dict[str, Any]:
    state_path = _state_path(path)
    if not state_path.parent.is_dir():
        raise SupervisorError("budget_state_parent_missing")
    now = time.monotonic()
    state = {
        "state_version": 1,
        "workflow_id": str(uuid.uuid4()),
        "task_id": TASK_ID,
        "boot_id": _current_boot_id(),
        "started_monotonic": now,
        "task_deadline_monotonic": now + TASK_BUDGET_SECONDS,
        "aggregate_limit_seconds": AGGREGATE_BUDGET_SECONDS,
        "aggregate_consumed_seconds": 0.0,
        "stages_completed": 0,
        "in_flight": None,
    }
    payload = _state_bytes(state)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(state_path, flags, 0o600)
    except FileExistsError as exc:
        raise SupervisorError("budget_state_already_exists") from exc
    except OSError as exc:
        raise SupervisorError("budget_state_unusable") from exc
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        _fsync_directory(state_path.parent)
    except BaseException:
        try:
            state_path.unlink()
        except OSError:
            pass
        raise
    return state


def _load_attempt_state(path: Path) -> dict[str, Any]:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("not a regular file")
            data = stream.read(65537)
        if len(data) > 65536:
            raise ValueError("oversized state")
        state = json.loads(data, object_pairs_hook=_unique_json_object)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise SupervisorError("budget_state_unusable") from exc
    keys = {
        "state_version", "workflow_id", "task_id", "boot_id", "started_monotonic",
        "task_deadline_monotonic", "aggregate_limit_seconds", "aggregate_consumed_seconds",
        "stages_completed", "in_flight",
    }
    if not isinstance(state, dict) or set(state) != keys:
        raise SupervisorError("budget_state_unusable")
    numeric = (
        state["started_monotonic"], state["task_deadline_monotonic"],
        state["aggregate_limit_seconds"], state["aggregate_consumed_seconds"],
    )
    if (
        isinstance(state["state_version"], bool)
        or state["state_version"] != 1
        or state["task_id"] != TASK_ID
        or state["boot_id"] != _current_boot_id()
        or not isinstance(state["workflow_id"], str)
        or not state["workflow_id"]
        or any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in numeric)
        or state["started_monotonic"] < 0
        or state["task_deadline_monotonic"] != state["started_monotonic"] + TASK_BUDGET_SECONDS
        or state["aggregate_limit_seconds"] != AGGREGATE_BUDGET_SECONDS
        or state["aggregate_consumed_seconds"] < 0
        or isinstance(state["stages_completed"], bool)
        or not isinstance(state["stages_completed"], int)
        or state["stages_completed"] < 0
        or state["in_flight"] is not None
    ):
        raise SupervisorError("budget_state_unusable")
    return state


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _state_bytes(state: dict[str, Any]) -> bytes:
    return (json.dumps(state, allow_nan=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _write_attempt_state(path: Path, state: dict[str, Any]) -> None:
    _reject_symlink_path(path.parent)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(temporary, flags, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(_state_bytes(state))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except BaseException as exc:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise SupervisorError("budget_state_unusable") from exc


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _acquire_budget_lock(path: Path) -> int:
    lock_path = path.with_name(f".{path.name}.lock")
    _reject_symlink_path(lock_path.parent)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    fd = None
    try:
        fd = os.open(lock_path, flags, 0o600)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise SupervisorError("budget_state_unusable")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BlockingIOError as exc:
        try:
            if fd is not None:
                os.close(fd)
        except (OSError, UnboundLocalError):
            pass
        raise SupervisorError("budget_state_locked") from exc
    except OSError as exc:
        try:
            if fd is not None:
                os.close(fd)
        except (OSError, UnboundLocalError):
            pass
        raise SupervisorError("budget_state_unusable") from exc


@dataclass(frozen=True)
class _Limits:
    wall_seconds: float
    rss_bytes: int
    minimum_memory_free_bytes: int
    minimum_disk_free_bytes: int
    output_bytes: int
    report_bytes: int
    poll_seconds: float
    terminate_grace_seconds: float


_PRODUCTION_LIMITS = _Limits(
    wall_seconds=180.0,
    rss_bytes=RSS_BUDGET_BYTES,
    minimum_memory_free_bytes=MINIMUM_HEADROOM_BYTES,
    minimum_disk_free_bytes=MINIMUM_HEADROOM_BYTES,
    output_bytes=OUTPUT_BUDGET_BYTES,
    report_bytes=REPORT_BUDGET_BYTES,
    poll_seconds=POLL_SECONDS,
    terminate_grace_seconds=TERMINATE_GRACE_SECONDS,
)


def _supervise(
    argv: Sequence[str],
    *,
    cwd: str | Path,
    env: dict[str, str],
    stage: str,
    log_path: str | Path,
    report_path: str | Path,
    output_path: str | Path | None,
    limits: _Limits,
    attempt_state: str | Path | None = None,
    invocation_id: str | None = None,
    ownership_receipt: str | Path | None = None,
    published_receipt: str | Path | None = None,
    resource_root: str | Path | None = None,
) -> dict[str, Any]:
    if attempt_state is None:
        return _supervise_impl(
            argv, cwd=cwd, env=env, stage=stage, log_path=log_path,
            report_path=report_path, output_path=output_path, limits=limits,
            ownership_receipt=ownership_receipt, published_receipt=published_receipt,
            invocation_id=invocation_id, resource_root=resource_root,
        )
    state_path = _state_path(attempt_state)
    lock_fd = _acquire_budget_lock(state_path)
    try:
        state = _load_attempt_state(state_path)
        now = time.monotonic()
        task_remaining = state["task_deadline_monotonic"] - now
        aggregate_before = state["aggregate_consumed_seconds"]
        aggregate_remaining = state["aggregate_limit_seconds"] - aggregate_before
        if task_remaining <= 0:
            raise SupervisorError("task_budget_exhausted")
        if aggregate_remaining <= 0:
            raise SupervisorError("aggregate_budget_exhausted")
        invocation = invocation_id or str(uuid.uuid4())
        context: dict[str, Any] = {
            "state": state,
            "state_path": state_path,
            "invocation_id": invocation,
            "task_remaining_before": task_remaining,
            "aggregate_before": aggregate_before,
            "spawned": False,
            "report_fields": {},
        }

        def begin(started: float, deadline: float) -> None:
            state["in_flight"] = {
                "invocation_id": invocation,
                "stage": stage,
                "started_monotonic": started,
                "deadline_monotonic": deadline,
                "upper_limit_seconds": deadline - started,
                "owned_evidence_dir": str(Path(log_path).parent.absolute()),
            }
            _write_attempt_state(state_path, state)
            context["started_monotonic"] = started
            context["deadline_monotonic"] = deadline

        def mark_spawned() -> None:
            context["spawned"] = True

        def finalize(consumed: float, *, reaped: bool) -> None:
            if not reaped:
                raise SupervisorError("child_reap_failed")
            state["aggregate_consumed_seconds"] += consumed
            state["stages_completed"] += 1
            state["in_flight"] = None
            _write_attempt_state(state_path, state)
            context["report_fields"] = {
                "workflow_id": state["workflow_id"],
                "invocation_id": invocation,
                "state_path": str(state_path),
                "started_monotonic": context["started_monotonic"],
                "deadline_monotonic": context["deadline_monotonic"],
                "remaining_task_before": task_remaining,
                "aggregate_before": aggregate_before,
                "consumed_this_stage": consumed,
                "aggregate_after": state["aggregate_consumed_seconds"],
                "stages_completed": state["stages_completed"],
                "finalized": True,
                "reaped": reaped,
            }

        context["begin"] = begin
        context["mark_spawned"] = mark_spawned
        context["finalize"] = finalize
        try:
            result = _supervise_impl(
                argv, cwd=cwd, env=env, stage=stage, log_path=log_path,
                report_path=report_path, output_path=output_path, limits=limits,
                _budget_context=context, ownership_receipt=ownership_receipt,
                published_receipt=published_receipt, invocation_id=invocation,
                resource_root=resource_root,
            )
        except BaseException:
            if not context["spawned"] and state["in_flight"] is not None:
                state["in_flight"] = None
                _write_attempt_state(state_path, state)
            raise
        if not context["spawned"] and state["in_flight"] is not None:
            state["in_flight"] = None
            _write_attempt_state(state_path, state)
        return result
    finally:
        os.close(lock_fd)


def _supervise_impl(
    argv: Sequence[str],
    *,
    cwd: str | Path,
    env: dict[str, str],
    stage: str,
    log_path: str | Path,
    report_path: str | Path,
    output_path: str | Path | None,
    limits: _Limits,
    _budget_context: dict[str, Any] | None = None,
    ownership_receipt: str | Path | None = None,
    published_receipt: str | Path | None = None,
    invocation_id: str | None = None,
    resource_root: str | Path | None = None,
) -> dict[str, Any]:
    command = [os.fspath(part) for part in argv]
    workdir = Path(cwd)
    log_file = Path(log_path)
    report_file = Path(report_path)
    output = Path(output_path) if output_path is not None else None
    disk_root = Path(resource_root) if resource_root is not None else EVIDENCE
    log_file.parent.mkdir(parents=True, exist_ok=True)
    report_file.parent.mkdir(parents=True, exist_ok=True)
    _reject_symlink_path(log_file.parent)
    _reject_symlink_path(report_file.parent)
    started = time.monotonic()
    max_rss = 0
    failure_reason = None
    exit_code = None
    process: subprocess.Popen[bytes] | None = None
    reaped = True
    min_disk_free = None
    child_started = None
    owned_before = None
    owned_after = None
    pending_exception: tuple[BaseException, Any] | None = None

    def remember_exception(exc: BaseException) -> None:
        nonlocal pending_exception
        if pending_exception is None:
            pending_exception = (exc, exc.__traceback__)

    def invalidate_owned_output() -> None:
        if output is None or stage not in {"generate", "resume"}:
            return
        try:
            _invalidate_owned_complete(
                output, ownership_receipt, published_receipt, invocation_id
            )
        except BaseException:
            pass

    try:
        with log_file.open("xb") as log_stream:
            try:
                if not command or not workdir.is_dir():
                    raise SupervisorError("invalid_child_command_or_cwd")
                available_memory = _available_memory_bytes()
                disk_free = shutil.disk_usage(disk_root).free
                min_disk_free = disk_free
                if available_memory < limits.minimum_memory_free_bytes:
                    raise SupervisorError("memory_headroom_below_budget")
                if disk_free < limits.minimum_disk_free_bytes:
                    raise SupervisorError("disk_headroom_below_budget")
                if _budget_context is not None:
                    now = time.monotonic()
                    remaining_task = _budget_context["state"]["task_deadline_monotonic"] - now
                    remaining_aggregate = (
                        _budget_context["state"]["aggregate_limit_seconds"]
                        - _budget_context["state"]["aggregate_consumed_seconds"]
                    )
                else:
                    now = time.monotonic()
                    remaining_task = TASK_BUDGET_SECONDS - (now - TASK_STARTED_MONOTONIC)
                    remaining_aggregate = limits.wall_seconds
                if remaining_task <= 0:
                    raise SupervisorError("task_budget_exhausted")
                if remaining_aggregate <= 0:
                    raise SupervisorError("aggregate_budget_exhausted")
                spawn_start = now
                deadline = spawn_start + min(limits.wall_seconds, remaining_task, remaining_aggregate)
                if _budget_context is not None:
                    owned_before = _owned_code_refs()
                    spawn_start = time.monotonic()
                    remaining_task = _budget_context["state"]["task_deadline_monotonic"] - spawn_start
                    remaining_aggregate = _budget_context["state"]["aggregate_limit_seconds"] - _budget_context["state"]["aggregate_consumed_seconds"]
                    if remaining_task <= 0:
                        raise SupervisorError("task_budget_exhausted")
                    if remaining_aggregate <= 0:
                        raise SupervisorError("aggregate_budget_exhausted")
                    deadline = spawn_start + min(limits.wall_seconds, remaining_task, remaining_aggregate)
                    _budget_context["begin"](spawn_start, deadline)
                process = subprocess.Popen(
                    command,
                    cwd=workdir,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=log_stream,
                    stderr=subprocess.STDOUT,
                    close_fds=True,
                    start_new_session=True,
                )
                child_started = spawn_start
                reaped = False
                if _budget_context is not None:
                    _budget_context["mark_spawned"]()
                while True:
                    return_code = process.poll()
                    current_rss = _sample_rss(process.pid) if return_code is None else 0
                    max_rss = max(max_rss, current_rss)
                    if current_rss > limits.rss_bytes:
                        failure_reason = "rss_budget_exceeded"
                    if output is not None:
                        try:
                            if _tree_size(output) > limits.output_bytes:
                                failure_reason = "output_budget_exceeded"
                        except SupervisorError as exc:
                            failure_reason = str(exc)
                    current_log_size = os.fstat(log_stream.fileno()).st_size
                    if current_log_size > limits.report_bytes:
                        failure_reason = "report_budget_exceeded"
                    current_disk_free = shutil.disk_usage(disk_root).free
                    min_disk_free = min(min_disk_free, current_disk_free)
                    if current_disk_free < limits.minimum_disk_free_bytes:
                        failure_reason = "disk_headroom_below_budget"
                    if time.monotonic() >= deadline:
                        failure_reason = "wall_timeout"
                    if failure_reason is not None:
                        break
                    if return_code is not None:
                        exit_code = return_code
                        if _process_group_exists(process.pid):
                            failure_reason = "orphan_process_group"
                        break
                    time.sleep(limits.poll_seconds)
            except (OSError, SupervisorError) as exc:
                failure_reason = str(exc) or "supervisor_preflight_failed"
            except BaseException as exc:
                remember_exception(exc)
            finally:
                process_group_terminated = process is None
                if process is not None:
                    try:
                        return_code = process.poll()
                        if return_code is None or _process_group_exists(process.pid):
                            _terminate_process_group(process, limits.terminate_grace_seconds)
                        process_group_terminated = True
                    except BaseException as exc:
                        process_group_terminated = False
                        remember_exception(exc)
                    finally:
                        try:
                            if process.returncode is None:
                                process.wait(timeout=limits.terminate_grace_seconds)
                        except BaseException as exc:
                            process_group_terminated = False
                            remember_exception(exc)
                        finally:
                            reaped = process.returncode is not None
                            if not reaped:
                                process_group_terminated = False
                            if exit_code is None and reaped:
                                exit_code = process.returncode

                if (
                    process is not None
                    and child_started is not None
                    and process_group_terminated
                    and _budget_context is not None
                ):
                    try:
                        owned_after = _owned_code_refs()
                        if owned_after != owned_before:
                            failure_reason = "owned_code_changed_during_stage"
                    except (OSError, SupervisorError) as exc:
                        failure_reason = str(exc) or "owned_code_ref_failed"
                    except BaseException as exc:
                        remember_exception(exc)

                if failure_reason is None and exit_code is not None and exit_code != 0:
                    failure_reason = "child_nonzero_exit"

                if child_started is not None and _budget_context is not None:
                    if reaped and process_group_terminated:
                        try:
                            _budget_context["finalize"](
                                max(0.0, time.monotonic() - child_started), reaped=True
                            )
                        except BaseException as exc:
                            remember_exception(exc)
                    else:
                        remember_exception(SupervisorError("child_reap_failed"))

                try:
                    log_stream.flush()
                    os.fsync(log_stream.fileno())
                except BaseException as exc:
                    remember_exception(exc)
                finally:
                    if failure_reason is not None or pending_exception is not None:
                        invalidate_owned_output()
    except BaseException as exc:
        remember_exception(exc)

    if pending_exception is not None:
        invalidate_owned_output()
        exc, traceback = pending_exception
        if failure_reason is not None and failure_reason not in str(exc):
            raise SupervisorError(f"{failure_reason}; {exc}") from exc
        raise exc.with_traceback(traceback)

    elapsed = time.monotonic() - started
    try:
        try:
            log_size, log_sha256 = _file_ref(log_file)
        except (OSError, SupervisorError):
            log_size, log_sha256 = 0, ""
            failure_reason = failure_reason or "supervisor_log_ref_failed"
        output_bytes = 0
        if output is not None and output.exists():
            try:
                output_bytes = _tree_size(output)
            except SupervisorError as exc:
                failure_reason = str(exc)
        status = "complete" if failure_reason is None and exit_code == 0 and reaped else "failed"
        report: dict[str, Any] = {
            "schema_version": 1,
            "task_id": "t_322319de",
            "stage": stage,
            "budget_report_path": str(report_file),
            "status": status,
            "failure_reason": failure_reason,
            "command": command,
            "cwd": str(workdir),
            "exit_code": exit_code,
            "elapsed_seconds": elapsed,
            "max_sampled_rss_bytes": max_rss,
            "minimum_disk_free_bytes_observed": min_disk_free,
            "output_path": str(output) if output is not None else None,
            "output_bytes": output_bytes,
            "log": {"path": str(log_file), "bytes": log_size, "sha256": log_sha256},
            "limits": asdict(limits),
            "poll_seconds": limits.poll_seconds,
            "terminate_grace_seconds": limits.terminate_grace_seconds,
            "reaped": reaped,
            **(
                {
                    "env_keys": sorted(env),
                    "current_owned_refs_before": owned_before,
                    "current_owned_refs_after": owned_after,
                }
                if _budget_context is not None
                else {}
            ),
            **(_budget_context["report_fields"] if _budget_context is not None else {}),
        }
        report_bytes = (json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode(
            "utf-8"
        )
        if status != "complete":
            invalidate_owned_output()
        if len(report_bytes) > limits.report_bytes:
            raise SupervisorError("budget_report_exceeded")
        try:
            _write_exclusive(report_file, report_bytes)
        except OSError as exc:
            raise SupervisorError("budget_report_write_failed") from exc
        return report
    except BaseException:
        invalidate_owned_output()
        raise


def run_stage(
    stage: str,
    *,
    attempt_state: str | Path,
    output: str | None = None,
    manifest: str | None = None,
) -> dict[str, Any]:
    if stage not in {"tests", "generate", "verify"}:
        raise SupervisorError("unknown_stage")
    state_path = _state_path(attempt_state)
    if state_path != ATTEMPT_STATE:
        raise SupervisorError("budget_state_path_mismatch")
    _load_attempt_state(state_path)
    _reject_symlink_path(EVIDENCE)
    attempt_dir = _new_attempt_directory(EVIDENCE / "validation/attempts")
    invocation_id = str(uuid.uuid4())
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    env.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
            "OMP_NUM_THREADS": "1",
            "OPENBLAS_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
            "NUMEXPR_NUM_THREADS": "1",
        }
    )
    if stage == "tests":
        argv = [
            str(PYTHON),
            "-B",
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "tests",
            "-q",
        ]
        output_path = None
        limit = _Limits(
            **{**asdict(_PRODUCTION_LIMITS), "wall_seconds": min(180.0, TEST_AGGREGATE_BUDGET_SECONDS)}
        )
    elif stage == "generate":
        if output is None or manifest is not None:
            raise SupervisorError("generate_requires_only_output")
        output_path = _owned_fresh_output(output)
        argv = [
            str(PYTHON),
            "-B",
            "-m",
            "generation_io",
            "legacy-generate",
            "--source",
            "/opt/data/workspaces/graduation-research-evidence/unified-work/G01/fixtures/selected-fields.jsonl",
            "--source-contract",
            "/opt/data/workspaces/graduation-research-evidence/unified-work/G01/fixtures/fixture-manifest.json",
            "--input-kind",
            "selected_projection",
            "--boundary",
            str(PACKAGE / "tests/fixtures/region-a.geojson"),
            "--region-id",
            "fixture:region-a",
            "--output",
            str(output_path),
            "--ownership-receipt",
            str(attempt_dir / "ownership.json"),
            "--invocation-id",
            invocation_id,
        ]
        limit = _Limits(**{**asdict(_PRODUCTION_LIMITS), "wall_seconds": 120.0})
    else:
        if manifest is None or output is not None:
            raise SupervisorError("verify_requires_only_manifest")
        manifest_path = _owned_manifest(manifest)
        argv = [str(PYTHON), "-B", "-m", "generation_io", "legacy-verify", "--manifest", str(manifest_path)]
        output_path = manifest_path.parent
        limit = _Limits(**{**asdict(_PRODUCTION_LIMITS), "wall_seconds": 60.0})

    return _supervise(
        argv,
        cwd=PACKAGE,
        env=env,
        stage=stage,
        log_path=attempt_dir / f"{stage}.log",
        report_path=attempt_dir / f"{stage}.json",
        output_path=output_path,
        limits=limit,
        attempt_state=state_path,
        invocation_id=invocation_id,
        ownership_receipt=attempt_dir / "ownership.json" if stage == "generate" else None,
        published_receipt=attempt_dir / "published-files.json" if stage == "generate" else None,
    )


def _portable_raw_path(value: str | Path, error_code: str) -> Path:
    try:
        raw = os.fspath(value)
        if (
            not isinstance(raw, str)
            or not raw
            or chr(0) in raw
            or chr(92) in raw
            or PureWindowsPath(raw).drive
            or ".." in Path(raw).parts
        ):
            raise SupervisorError(error_code)
        return Path(raw)
    except SupervisorError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise SupervisorError(error_code) from exc


def _portable_state_directory(value: str | Path, *, create: bool) -> Path:
    raw = _portable_raw_path(value, "state_dir_invalid")
    path = raw if raw.is_absolute() else Path.cwd() / raw
    path = Path(os.path.abspath(path))
    _reject_symlink_path(path)
    if os.path.lexists(path):
        if path.is_symlink() or not path.is_dir():
            raise SupervisorError("state_dir_invalid")
    elif create:
        if not path.parent.is_dir():
            raise SupervisorError("state_dir_parent_missing")
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            if path.is_symlink() or not path.is_dir():
                raise SupervisorError("state_dir_invalid")
        except OSError as exc:
            raise SupervisorError("state_dir_create_failed") from exc
    else:
        raise SupervisorError("state_dir_missing")
    return path.resolve(strict=True)


def _initialize_portable_attempt(state_dir: str | Path) -> tuple[dict[str, Any], Path]:
    root = _portable_state_directory(state_dir, create=True)
    state_path = root / "budget-state.json"
    return _initialize_attempt_state(state_path), state_path


def _portable_data_directory(value: str | Path) -> Path:
    raw = _portable_raw_path(value, "data_dir_invalid")
    path = raw if raw.is_absolute() else Path.cwd() / raw
    path = Path(os.path.abspath(path))
    _reject_symlink_path(path)
    if path.is_symlink() or not path.is_dir():
        raise SupervisorError("data_dir_invalid")
    return path.resolve(strict=True)


def _portable_output_path(value: str | Path) -> Path:
    raw = _portable_raw_path(value, "output_path_invalid")
    path = raw if raw.is_absolute() else Path.cwd() / raw
    path = Path(os.path.abspath(path))
    _reject_symlink_path(path.parent)
    if os.path.lexists(path):
        raise SupervisorError("output_exists")
    if not path.parent.is_dir():
        raise SupervisorError("output_parent_missing")
    return path.parent.resolve(strict=True) / path.name


def _portable_manifest_path(value: str | Path) -> Path:
    raw = _portable_raw_path(value, "manifest_path_invalid")
    path = raw if raw.is_absolute() else Path.cwd() / raw
    path = Path(os.path.abspath(path))
    _reject_symlink_path(path)
    try:
        if path.is_symlink() or not stat.S_ISREG(path.lstat().st_mode):
            raise SupervisorError("manifest_must_be_regular")
        return path.resolve(strict=True)
    except OSError as exc:
        raise SupervisorError("manifest_must_be_regular") from exc


def _portable_manifest_name(value: str) -> str:
    if not isinstance(value, str) or not value or chr(92) in value or "\\\\" in value:
        raise SupervisorError("input_manifest_name_invalid")
    relative = PurePosixPath(value)
    if relative.is_absolute() or PureWindowsPath(value).drive or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise SupervisorError("input_manifest_name_invalid")
    return relative.as_posix()


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _require_separate_paths(*paths: Path) -> None:
    for index, left in enumerate(paths):
        for right in paths[index + 1 :]:
            if _paths_overlap(left, right):
                raise SupervisorError("portable_roots_overlap")


def run_portable_stage(
    stage: str,
    *,
    state_dir: str | Path,
    data_dir: str | Path | None = None,
    input_manifest: str | None = None,
    region_id: str | None = None,
    output: str | Path | None = None,
    manifest: str | Path | None = None,
) -> dict[str, Any]:
    """Run one portable CLI stage under a caller-owned shared state directory."""
    if stage not in {"generate", "verify", "resume"}:
        raise SupervisorError("unknown_stage")
    state_root = _portable_state_directory(state_dir, create=False)
    state_path = _state_path(state_root / "budget-state.json")
    state = _load_attempt_state(state_path)
    now = time.monotonic()
    if state["task_deadline_monotonic"] - now <= 0:
        raise SupervisorError("task_budget_exhausted")
    if state["aggregate_limit_seconds"] - state["aggregate_consumed_seconds"] <= 0:
        raise SupervisorError("aggregate_budget_exhausted")

    data_root: Path | None = None
    manifest_name: str | None = None
    manifest_path: Path | None = None
    output_path: Path | None = None
    if stage == "generate":
        if data_dir is None or input_manifest is None or region_id is None or output is None or manifest is not None:
            raise SupervisorError("generate_requires_data_manifest_region_output")
        data_root = _portable_data_directory(data_dir)
        manifest_name = _portable_manifest_name(input_manifest)
        output_path = _portable_output_path(output)
        _require_separate_paths(state_root, data_root, output_path)
        input_path = data_root / manifest_name
        _reject_symlink_path(input_path)
        try:
            if not stat.S_ISREG(input_path.lstat().st_mode):
                raise SupervisorError("input_manifest_must_be_regular")
        except OSError as exc:
            raise SupervisorError("input_manifest_must_be_regular") from exc
    elif stage == "verify":
        if manifest is None or any(value is not None for value in (data_dir, input_manifest, region_id, output)):
            raise SupervisorError("verify_requires_only_manifest")
        manifest_path = _portable_manifest_path(manifest)
        _require_separate_paths(state_root, manifest_path.parent)
    else:
        if manifest is None or output is None or any(value is not None for value in (data_dir, input_manifest, region_id)):
            raise SupervisorError("resume_requires_manifest_output")
        manifest_path = _portable_manifest_path(manifest)
        output_path = _portable_output_path(output)
        _require_separate_paths(state_root, manifest_path.parent, output_path)

    attempt_dir = _new_attempt_directory(state_root / "attempts")
    invocation_id = str(uuid.uuid4())
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    env.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
            "OMP_NUM_THREADS": "1",
            "OPENBLAS_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
            "NUMEXPR_NUM_THREADS": "1",
        }
    )
    receipt_args: dict[str, str] = {}
    if stage == "generate":
        argv = [
            sys.executable,
            "-B",
            "-m",
            "generation_io",
            "generate",
            "--data-dir",
            str(data_root),
            "--input-manifest",
            manifest_name,
            "--region-id",
            region_id,
            "--output-dir",
            str(output_path),
            "--ownership-receipt",
            str(attempt_dir / "ownership.json"),
            "--published-receipt",
            str(attempt_dir / "published-files.json"),
            "--invocation-id",
            invocation_id,
        ]
        wall_seconds = 120.0
        output_path_arg: Path | None = output_path
        receipt_args = {
            "ownership_receipt": str(attempt_dir / "ownership.json"),
            "published_receipt": str(attempt_dir / "published-files.json"),
        }
    elif stage == "verify":
        argv = [
            sys.executable,
            "-B",
            "-m",
            "generation_io",
            "verify",
            "--manifest",
            str(manifest_path),
        ]
        wall_seconds = 60.0
        output_path_arg = None
    else:
        argv = [
            sys.executable,
            "-B",
            "-m",
            "generation_io",
            "resume",
            "--manifest",
            str(manifest_path),
            "--output-dir",
            str(output_path),
            "--ownership-receipt",
            str(attempt_dir / "ownership.json"),
            "--published-receipt",
            str(attempt_dir / "published-files.json"),
            "--invocation-id",
            invocation_id,
        ]
        wall_seconds = 120.0
        output_path_arg = output_path
        receipt_args = {
            "ownership_receipt": str(attempt_dir / "ownership.json"),
            "published_receipt": str(attempt_dir / "published-files.json"),
        }

    limits = _Limits(**{**asdict(_PRODUCTION_LIMITS), "wall_seconds": wall_seconds})
    return _supervise(
        argv,
        cwd=PACKAGE,
        env=env,
        stage=stage,
        log_path=attempt_dir / f"{stage}.log",
        report_path=attempt_dir / f"{stage}.json",
        output_path=output_path_arg,
        limits=limits,
        attempt_state=state_path,
        invocation_id=invocation_id,
        resource_root=state_root,
        **receipt_args,
    )


def _owned_fresh_output(value: str) -> Path:
    path = Path(value).absolute()
    if path.parent != EVIDENCE or os.path.lexists(path):
        raise SupervisorError("generate_output_must_be_fresh_g02_child")
    _reject_symlink_path(path.parent)
    return path


def _owned_manifest(value: str) -> Path:
    supplied = Path(value).absolute()
    _reject_symlink_path(supplied)
    if supplied.is_symlink():
        raise SupervisorError("verify_manifest_must_be_regular_g02_file")
    path = supplied.resolve(strict=True)
    if not path.is_relative_to(EVIDENCE.resolve(strict=True)) or not path.is_file():
        raise SupervisorError("verify_manifest_must_be_regular_g02_file")
    return path


def _new_attempt_directory(parent: Path) -> Path:
    _reject_symlink_path(parent)
    parent.mkdir(parents=True, exist_ok=True)
    for _ in range(5):
        path = parent / f"attempt-{uuid.uuid4().hex}"
        try:
            path.mkdir(mode=0o700)
            return path
        except FileExistsError:
            continue
    raise SupervisorError("attempt_directory_collision")


def _available_memory_bytes() -> int:
    host_available = _host_available_memory_bytes()
    cgroup_available = _cgroup_memory_headroom_bytes()
    return host_available if cgroup_available is None else min(host_available, cgroup_available)


def _host_available_memory_bytes() -> int:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError) as exc:
        raise SupervisorError("memory_headroom_unavailable") from exc
    raise SupervisorError("memory_headroom_unavailable")


def _cgroup_memory_headroom_bytes() -> int | None:
    try:
        entries = [
            line[3:]
            for line in CGROUP_FILE.read_text(encoding="ascii").splitlines()
            if line.startswith("0::/")
        ]
        if len(entries) != 1:
            raise ValueError("cgroup v2 membership is unavailable")
        parts = PurePosixPath(entries[0]).parts[1:]
        if any(part in {"", ".", ".."} for part in parts):
            raise ValueError("invalid cgroup membership path")
        root = CGROUP_ROOT.resolve(strict=True)
        cgroup = root.joinpath(*parts).resolve(strict=True)
        if not cgroup.is_relative_to(root):
            raise ValueError("cgroup path escaped its mount")
        limit_text = (cgroup / "memory.max").read_text(encoding="ascii").strip()
        if limit_text == "max":
            return None
        limit = int(limit_text)
        current = int((cgroup / "memory.current").read_text(encoding="ascii").strip())
        if limit < 0 or current < 0:
            raise ValueError("invalid cgroup memory counters")
        return max(0, limit - current)
    except (OSError, UnicodeError, ValueError) as exc:
        raise SupervisorError("memory_headroom_unavailable") from exc


def _sample_rss(pid: int) -> int:
    try:
        for line in Path(f"/proc/{pid}/status").read_text(encoding="ascii").splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return 0
    return 0


def _tree_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_symlink():
        raise SupervisorError("output_symlink_detected")
    if path.is_file():
        return path.stat().st_size
    total = 0
    for root, directories, files in os.walk(path, followlinks=False):
        root_path = Path(root)
        for name in directories:
            directory = root_path / name
            try:
                if directory.is_symlink():
                    raise SupervisorError("output_symlink_detected")
            except FileNotFoundError:
                continue
        for name in files:
            file_path = root_path / name
            try:
                info = file_path.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode):
                raise SupervisorError("output_symlink_detected")
            total += info.st_size
    return total


def _process_group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _terminate_process_group(process: subprocess.Popen[bytes], grace_seconds: float) -> None:
    pgid = process.pid

    def group_exists_conservatively() -> bool:
        try:
            return _process_group_exists(pgid)
        except OSError:
            return True

    def signal_group(signum: int) -> None:
        try:
            os.killpg(pgid, signum)
        except ProcessLookupError:
            pass
        except OSError as exc:
            raise SupervisorError("child_reap_failed") from exc

    signal_group(signal.SIGTERM)
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline and group_exists_conservatively():
        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
    if group_exists_conservatively():
        signal_group(signal.SIGKILL)
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        signal_group(signal.SIGKILL)
        try:
            process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired as exc:
            raise SupervisorError("child_reap_failed") from exc

    disappearance_deadline = time.monotonic() + grace_seconds
    while group_exists_conservatively():
        remaining = disappearance_deadline - time.monotonic()
        if remaining <= 0:
            raise SupervisorError("child_reap_failed")
        time.sleep(min(0.05, remaining))


def _file_ref(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _write_exclusive(path: Path, contents: bytes) -> None:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(contents)
        stream.flush()
        os.fsync(stream.fileno())


def _write_generation_failure(output: Path, report: dict[str, Any]) -> None:
    if not output.exists() or output.is_symlink() or not output.is_dir():
        return
    complete_manifest = output / "generation-manifest.json"
    if os.path.lexists(complete_manifest):
        return
    failure_report = output / "failure-report.json"
    if not failure_report.exists():
        payload = {
            "schema_version": 1,
            "status": "failed",
            "error_code": report["failure_reason"] or "child_nonzero_exit",
            "complete_manifest_published": False,
            "budget_report": str(report["budget_report_path"]),
        }
        contents = (json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode(
            "utf-8"
        )
        _write_exclusive(failure_report, contents)


def _invalidate_owned_complete(
    output: Path,
    ownership_receipt: str | Path | None,
    published_receipt: str | Path | None,
    invocation_id: str | None,
) -> bool:
    if ownership_receipt is None or published_receipt is None or not invocation_id:
        return False
    output = Path(output).absolute()
    ownership_path = Path(ownership_receipt).absolute()
    published_path = Path(published_receipt).absolute()
    if ownership_path.parent != published_path.parent:
        return False
    try:
        _reject_symlink_path(output)
        if output.is_symlink() or not stat.S_ISDIR(output.lstat().st_mode):
            return False
        root_stat = output.stat(follow_symlinks=False)
        ownership = _read_receipt(ownership_path)
        published = _read_receipt(published_path)
        expected_root = {
            "receipt_version": 1,
            "invocation_id": invocation_id,
            "output_path": str(output.resolve(strict=True)),
            "root_st_dev": root_stat.st_dev,
            "root_st_ino": root_stat.st_ino,
        }
        if any(ownership.get(key) != value for key, value in expected_root.items()):
            return False
        complete = output / "generation-manifest.json"
        complete_stat = complete.lstat()
        if not stat.S_ISREG(complete_stat.st_mode):
            return False
        body = complete.read_bytes()
        expected_published = {
            **expected_root,
            "complete_path": str(complete.resolve(strict=True)),
            "complete_st_dev": complete_stat.st_dev,
            "complete_st_ino": complete_stat.st_ino,
            "complete_bytes": len(body),
            "complete_sha256": hashlib.sha256(body).hexdigest(),
        }
        if published != expected_published:
            return False
        current_stat = complete.lstat()
        if (
            (current_stat.st_dev, current_stat.st_ino)
            != (published["complete_st_dev"], published["complete_st_ino"])
            or current_stat.st_size != published["complete_bytes"]
            or hashlib.sha256(complete.read_bytes()).hexdigest()
            != published["complete_sha256"]
        ):
            return False
        complete.unlink()
        _fsync_directory(output)
        return True
    except (OSError, RuntimeError, SupervisorError, ValueError, TypeError, KeyError):
        return False


def _read_receipt(path: Path) -> dict[str, Any]:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("receipt_not_regular")
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError("receipt_too_large")
    result = json.loads(raw, object_pairs_hook=_unique_json_object)
    if not isinstance(result, dict):
        raise ValueError("receipt_not_object")
    return result


def _reject_symlink_path(path: Path) -> None:
    current = path.absolute()
    ancestors = []
    while current != current.parent:
        ancestors.append(current)
        current = current.parent
    for candidate in reversed(ancestors):
        if os.path.lexists(candidate) and candidate.is_symlink():
            raise SupervisorError("supervisor_path_symlink")


def _owned_code_refs() -> list[dict[str, object]]:
    runtime_root = PACKAGE / "generation_io"
    candidates: list[Path] = []

    def collect_runtime_files(directory: Path) -> None:
        try:
            with os.scandir(directory) as entries:
                ordered_entries = sorted(entries, key=lambda entry: entry.name)
        except OSError as exc:
            raise SupervisorError("owned_code_ref_unreadable") from exc
        for entry in ordered_entries:
            path = Path(entry.path)
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise SupervisorError("owned_code_ref_unreadable") from exc
            if stat.S_ISLNK(info.st_mode):
                raise SupervisorError("supervisor_path_symlink")
            if stat.S_ISDIR(info.st_mode):
                collect_runtime_files(path)
            elif path.suffix in {".py", ".json"}:
                if not stat.S_ISREG(info.st_mode):
                    raise SupervisorError("owned_code_ref_not_regular")
                candidates.append(path)

    try:
        _reject_symlink_path(runtime_root)
        runtime_info = runtime_root.lstat()
    except OSError as exc:
        raise SupervisorError("owned_code_ref_unreadable") from exc
    if not stat.S_ISDIR(runtime_info.st_mode):
        raise SupervisorError("owned_code_ref_not_regular")
    collect_runtime_files(runtime_root)

    for name in ("pyproject.toml", "uv.lock"):
        path = PACKAGE / name
        try:
            _reject_symlink_path(path)
            info = path.lstat()
        except FileNotFoundError as exc:
            raise SupervisorError("owned_code_manifest_missing") from exc
        except OSError as exc:
            raise SupervisorError("owned_code_ref_unreadable") from exc
        if not stat.S_ISREG(info.st_mode):
            raise SupervisorError("owned_code_ref_not_regular")
        candidates.append(path)

    refs: list[dict[str, object]] = []
    for path in sorted(candidates):
        _reject_symlink_path(path)
        digest = hashlib.sha256()
        size = 0
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise SupervisorError("owned_code_ref_not_regular")
                while chunk := stream.read(1024 * 1024):
                    size += len(chunk)
                    digest.update(chunk)
        except SupervisorError:
            raise
        except OSError as exc:
            raise SupervisorError("owned_code_ref_unreadable") from exc
        refs.append({"path": str(path), "bytes": size, "sha256": digest.hexdigest()})
    return refs


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run portable CLI stages under shared frozen resource budgets."
    )
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--stage", choices=("tests", "generate", "verify", "resume"))
    operation.add_argument("--init-attempt", action="store_true")
    parser.add_argument("--state-dir", help="caller-owned portable supervisor state directory")
    parser.add_argument("--attempt-state", help="legacy fixed G02 budget-state.json path")
    parser.add_argument("--data-dir")
    parser.add_argument("--input-manifest")
    parser.add_argument("--region-id")
    parser.add_argument("--output-dir", "--output", dest="output")
    parser.add_argument("--manifest")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        if args.init_attempt:
            if args.state_dir is not None and args.attempt_state is not None:
                raise SupervisorError("choose_state_dir_or_legacy_attempt_state")
            if args.state_dir is not None:
                state, state_path = _initialize_portable_attempt(args.state_dir)
                payload = {
                    "status": "initialized",
                    "workflow_id": state["workflow_id"],
                    "state_path": str(state_path),
                }
            elif args.attempt_state is not None:
                if Path(args.attempt_state).absolute() != ATTEMPT_STATE:
                    raise SupervisorError("budget_state_path_mismatch")
                state = _initialize_attempt_state(args.attempt_state)
                payload = {
                    "status": "initialized",
                    "workflow_id": state["workflow_id"],
                    "state_path": str(ATTEMPT_STATE),
                    "legacy": True,
                }
            else:
                raise SupervisorError("state_dir_required")
            print(json.dumps(payload, ensure_ascii=False))
            return 0

        if args.state_dir is not None and args.attempt_state is not None:
            raise SupervisorError("choose_state_dir_or_legacy_attempt_state")
        if args.state_dir is not None:
            if args.stage == "tests":
                raise SupervisorError("tests_stage_is_legacy_only")
            report = run_portable_stage(
                args.stage,
                state_dir=args.state_dir,
                data_dir=args.data_dir,
                input_manifest=args.input_manifest,
                region_id=args.region_id,
                output=args.output,
                manifest=args.manifest,
            )
            payload = {
                "status": report["status"],
                "stage": args.stage,
                "budget_report": report["budget_report_path"],
            }
            if args.stage in {"generate", "resume"} and report.get("output_path"):
                payload["manifest"] = str(Path(report["output_path"]) / "generation-manifest.json")
            if report.get("failure_reason"):
                payload["failure_reason"] = report["failure_reason"]
            print(json.dumps(payload, ensure_ascii=False))
            return 0 if report["status"] == "complete" else 2

        if args.attempt_state is None:
            raise SupervisorError("state_dir_required")
        if Path(args.attempt_state).absolute() != ATTEMPT_STATE:
            raise SupervisorError("budget_state_path_mismatch")
        if args.stage == "resume":
            raise SupervisorError("resume_is_portable_only")
        report = run_stage(
            args.stage,
            attempt_state=args.attempt_state,
            output=args.output,
            manifest=args.manifest,
        )
    except (OSError, SupervisorError, TypeError, ValueError) as exc:
        print(json.dumps({"status": "failed", "error_code": str(exc)}, ensure_ascii=False))
        return 2
    print(
        json.dumps(
            {
                "status": report["status"],
                "budget_report": report["budget_report_path"],
                "legacy": True,
            },
            ensure_ascii=False,
        )
    )
    return 0 if report["status"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
