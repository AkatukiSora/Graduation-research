from __future__ import annotations

import json
import math
import stat
from pathlib import Path
from typing import Any, NoReturn

from tools import validate_revision2 as base

STAGES = ("tests", "generate", "verify")
CAPS = {"tests": 180.0, "generate": 120.0, "verify": 60.0}
SUFFIXES = {".py", ".json", ".toml", ".lock", ".yaml", ".yml", ".csv", ".jsonl"}
ENV_KEYS = {"PYTHONDONTWRITEBYTECODE", "PYTHONHASHSEED", "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"}
OUTPUTS = {"generation-manifest.json", "normalized-points.jsonl", "candidate-features.jsonl", "rejected-records.jsonl", "source-manifest.json"}


def _fail(code: str) -> NoReturn:
    raise base.ValidationError(code)


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _object(path: Path) -> dict[str, Any]:
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            _fail("invalid_json_object")
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_pairs)
    except base.ValidationError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise base.ValidationError("invalid_json_object") from exc
    if not isinstance(value, dict):
        _fail("invalid_json_object")
    return value


def current_owned_refs() -> list[dict[str, Any]]:
    refs = []
    for path in sorted(base.PACKAGE.rglob("*")):
        relative = path.relative_to(base.PACKAGE)
        if "__pycache__" in relative.parts or ".pytest_cache" in relative.parts:
            continue
        if path.is_symlink():
            _fail("owned_path_not_regular")
        if path.is_file() and (path.suffix.lower() in SUFFIXES or "fixtures" in relative.parts):
            if not stat.S_ISREG(path.lstat().st_mode):
                _fail("owned_path_not_regular")
            refs.append(base._ref(path))
    return refs


def _state() -> dict[str, Any]:
    state = _object(base.ATTEMPT_STATE)
    required = {"state_version", "workflow_id", "task_id", "boot_id", "started_monotonic", "task_deadline_monotonic", "aggregate_limit_seconds", "aggregate_consumed_seconds", "stages_completed", "in_flight"}
    if set(state) != required or state.get("state_version") != 1 or isinstance(state.get("state_version"), bool):
        _fail("budget_state_unusable")
    if state.get("task_id") != base.EXPECTED_TASK_ID or not isinstance(state.get("workflow_id"), str) or not state["workflow_id"] or state.get("in_flight") is not None or state.get("stages_completed") != 3 or isinstance(state.get("stages_completed"), bool):
        _fail("budget_state_unusable")
    numbers = [state.get(k) for k in ("started_monotonic", "task_deadline_monotonic", "aggregate_limit_seconds", "aggregate_consumed_seconds")]
    if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in numbers):
        _fail("budget_state_unusable")
    try:
        boot = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as exc:
        raise base.ValidationError("budget_state_unusable") from exc
    if state["boot_id"] != boot or state["started_monotonic"] < 0 or state["task_deadline_monotonic"] != state["started_monotonic"] + 3600.0 or state["aggregate_limit_seconds"] != 600.0 or not 0 <= state["aggregate_consumed_seconds"] <= 600.0:
        _fail("budget_state_unusable")
    return state


def _reports(evidence: Path) -> list[tuple[Path, dict[str, Any]]]:
    attempts = evidence / "validation/attempts"
    if not attempts.is_dir() or attempts.is_symlink():
        _fail("stage_attempts_missing")
    found = []
    for path in attempts.glob("attempt-*/*.json"):
        if path.name in {"tests.json", "generate.json", "verify.json"}:
            try:
                found.append((path, _object(path)))
            except base.ValidationError:
                continue
    return found


def _check_command(stage: str, report: dict[str, Any], attempt: Path) -> None:
    python = str(base.P02 / ".venv/bin/python")
    command, keys = report.get("command"), report.get("env_keys")
    if not isinstance(command, list) or not all(isinstance(x, str) for x in command) or not isinstance(keys, list) or not all(isinstance(x, str) for x in keys) or keys != sorted(set(keys)) or not ENV_KEYS.issubset(keys) or "PYTHONPATH" in keys or report.get("cwd") != str(base.PACKAGE):
        _fail("stage_command_contract_mismatch")
    if stage == "tests":
        valid = command == [python, "-B", "-m", "pytest", "-p", "no:cacheprovider", "tests", "-q"] and report.get("output_path") is None
    elif stage == "generate":
        prefix = [python, "-B", "-m", "generation_io", "legacy-generate", "--source", "/opt/data/workspaces/graduation-research-evidence/unified-work/G01/fixtures/selected-fields.jsonl", "--source-contract", "/opt/data/workspaces/graduation-research-evidence/unified-work/G01/fixtures/fixture-manifest.json", "--input-kind", "selected_projection", "--boundary", str(base.PACKAGE / "tests/fixtures/region-a.geojson"), "--region-id", "fixture:region-a", "--output", str(base.EVIDENCE / "selected-projection-01")]
        valid = len(command) == len(prefix) + 4 and command[:len(prefix)] == prefix
        if valid:
            suffix = command[len(prefix):]
            valid = suffix[0] == "--ownership-receipt" and Path(suffix[1]) == attempt / "ownership.json" and suffix[2] == "--invocation-id" and bool(suffix[3]) and report.get("invocation_id") == suffix[3]
        valid = valid and report.get("output_path") == str(base.EVIDENCE / "selected-projection-01")
    else:
        valid = command == [python, "-B", "-m", "generation_io", "legacy-verify", "--manifest", str(base.EVIDENCE / "selected-projection-01/generation-manifest.json")] and report.get("output_path") == str(base.EVIDENCE / "selected-projection-01")
    if not valid:
        _fail("stage_command_contract_mismatch")


def _generation_receipts(attempt: Path, report: dict[str, Any]) -> list[dict[str, Any]]:
    output = base.EVIDENCE / "selected-projection-01"
    owner_path, published_path = attempt / "ownership.json", attempt / "published-files.json"
    owner, published = _object(owner_path), _object(published_path)
    owner_keys = {"receipt_version", "invocation_id", "output_path", "root_st_dev", "root_st_ino"}
    full_keys = owner_keys | {"complete_path", "complete_st_dev", "complete_st_ino", "complete_bytes", "complete_sha256"}
    if set(owner) != owner_keys or set(published) != full_keys:
        _fail("generation_ownership_receipt_invalid")
    root = output.lstat()
    complete = output / "generation-manifest.json"
    info = complete.lstat()
    size, digest = base.sha256_file(complete)
    for item in (owner, published):
        if item.get("receipt_version") != 1 or item.get("invocation_id") != report.get("invocation_id") or item.get("output_path") != str(output) or item.get("root_st_dev") != root.st_dev or item.get("root_st_ino") != root.st_ino:
            _fail("generation_ownership_receipt_invalid")
    if not stat.S_ISDIR(root.st_mode) or not stat.S_ISREG(info.st_mode) or published.get("complete_path") != str(complete) or published.get("complete_st_dev") != info.st_dev or published.get("complete_st_ino") != info.st_ino or published.get("complete_bytes") != size or published.get("complete_sha256") != digest:
        _fail("generation_ownership_receipt_invalid")
    return [base._ref(owner_path), base._ref(published_path)]


def collect_stage_results(evidence: Path = base.EVIDENCE) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    state, owned = _state(), current_owned_refs()
    reports = [(path, doc) for path, doc in _reports(evidence) if doc.get("workflow_id") == state["workflow_id"]]
    reports.sort(key=lambda pair: pair[1].get("started_monotonic", -1))
    if tuple(doc.get("stage") for _, doc in reports) != STAGES or len({doc.get("invocation_id") for _, doc in reports}) != 3:
        _fail("workflow_stage_chain_mismatch")
    results, chain = [], []
    spent, previous_start = 0.0, state["started_monotonic"]
    for index, ((path, doc), stage) in enumerate(zip(reports, STAGES, strict=True), 1):
        attempt, limits = path.parent, doc.get("limits")
        if not isinstance(limits, dict) or path != attempt / f"{stage}.json" or doc.get("task_id") != base.EXPECTED_TASK_ID or doc.get("status") != "complete" or doc.get("exit_code") != 0 or doc.get("failure_reason") is not None or doc.get("state_path") != str(base.ATTEMPT_STATE) or doc.get("stages_completed") != index or doc.get("finalized") is not True or doc.get("reaped") is not True or limits.get("wall_seconds") != CAPS[stage]:
            _fail("stage_report_contract_mismatch")
        started, deadline, remaining, before, consumed, after = (doc.get(key) for key in ("started_monotonic", "deadline_monotonic", "remaining_task_before", "aggregate_before", "consumed_this_stage", "aggregate_after"))
        values = (started, deadline, remaining, before, consumed, after)
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in values) or started < previous_start or started < state["started_monotonic"] or deadline < started or deadline - started > CAPS[stage] or deadline > state["task_deadline_monotonic"] or remaining <= 0 or before != spent or consumed <= 0 or after != before + consumed or after > 600.0:
            _fail("stage_budget_chain_mismatch")
        previous_start, spent = started, after
        log = doc.get("log")
        if not isinstance(log, dict):
            _fail("stage_log_reference_missing")
        log_ref = {"path": log.get("path"), "bytes": log.get("bytes"), "sha256": log.get("sha256")}
        base.validate_reference(log_ref)
        if log_ref["path"] != str(attempt / f"{stage}.log") or doc.get("current_owned_refs_before") != owned or doc.get("current_owned_refs_after") != owned:
            _fail("stage_owned_ref_or_log_mismatch")
        _check_command(stage, doc, attempt)
        receipts = _generation_receipts(attempt, doc) if stage == "generate" else []
        budget = {"workflow_id": doc["workflow_id"], "invocation_id": doc["invocation_id"], "state_path": doc["state_path"], "started_monotonic": started, "deadline_monotonic": deadline, "remaining_task_before": remaining, "aggregate_before": before, "consumed_this_stage": consumed, "aggregate_after": after, "stages_completed": index, "finalized": True, "reaped": True}
        report_ref = base._ref(path)
        chain.append(budget | {"stage": stage, "stage_report": report_ref})
        results.append({"stage": stage, "command": doc["command"], "cwd": doc["cwd"], "env_keys": doc["env_keys"], "exit_code": 0, "log": log_ref, "stage_report": report_ref, "current_owned_refs_before": owned, "current_owned_refs_after": owned, "budget": budget, "ownership_receipts": receipts})
    if spent != state["aggregate_consumed_seconds"] or spent > 600.0:
        _fail("workflow_final_budget_mismatch")
    workflow = {"workflow_id": state["workflow_id"], "state": state, "state_ref": base._ref(base.ATTEMPT_STATE), "stage_chain": chain, "immutable_clearance_receipts": [ref for result in results for ref in result["ownership_receipts"]]}
    return results, workflow


def _preserved_refs(release: dict[str, Any]) -> dict[str, Any]:
    inputs, old = release["input_refs"], release["old_G02_files"]
    if len(old) != 25:
        _fail("old_g02_reference_count_mismatch")
    all_refs = {item["path"]: item for item in [*inputs, *old]}
    freeze = next((item for item in old if item["path"].endswith("/G02/operator-input-freeze.json")), None)
    if freeze is None:
        _fail("upstream_freeze_reference_missing")
    revision2 = [item for item in all_refs.values() if any(x in item["path"] for x in ("/G02-revision2/", "/G02/selected-projection", "/G02/validation/"))]
    plans = [item for item in inputs if "/unified-plan/" in item["path"]]
    runtime = [item for item in inputs if "/P02/" in item["path"] or "/P05/" in item["path"]]
    runtime.extend(base._ref(path) for path in (base.P02 / "pyproject.toml", base.P02 / "uv.lock"))
    return {"old_G02_25_files": old, "revision2_saved_outputs_reports_logs": sorted(revision2, key=lambda item: item["path"]), "upstream_131_freeze_ref": freeze, "upstream_input_refs": inputs, "P02_P05_refs": sorted({item["path"]: item for item in runtime}.values(), key=lambda item: item["path"]), "47_node_70_edge_plan_refs": plans}


def _acceptance_mapping(stages: list[dict[str, Any]], owned: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cases = [
        ("AC1", "source provenance and frozen selected projection", ["test_selected_projection_is_frozen_and_preserves_all_row_provenance", "test_raw_parser_streams_cp932_csv_and_preserves_literal_selected_values"], ["tests/test_source.py", "tests/test_raw_source.py"], ["generation_io/source.py:57-155"]),
        ("AC2", "parser, normalization, and candidate-only semantics", ["test_raw_parser_rejects_unknown_contract_and_duplicate_keys", "test_normalized_stop_preserves_raw_tokens_and_marks_candidate_only"], ["tests/test_raw_source.py", "tests/test_normalize.py"], ["generation_io/source.py:57-155", "generation_io/artifacts.py:275-330"]),
        ("AC3", "region containment and boundary validation", ["test_covers_outer_edges_and_vertices_but_excludes_hole_interior", "test_rejects_self_intersecting_ring"], ["tests/test_region.py"], ["generation_io/region.py:1-100"]),
        ("AC4", "exact read-only P05 kernel parity and artifact publication", ["test_candidate_rows_match_independent_read_only_p05_public_api_exactly", "test_complete_manifest_is_not_published_before_draft_verification"], ["tests/test_artifacts.py"], ["generation_io/artifacts.py:275-330"]),
        ("AC5", "failure matrix and fresh-complete ownership cleanup", ["test_verify_failure_matrix_does_not_mutate_inputs", "test_late_supervisor_failure_invalidates_only_receipt_owned_complete", "test_late_supervisor_failure_invalidates_complete_when_report_is_refused", "test_supervisor_terminates_timed_out_child_and_records_failure"], ["tests/test_supervisor.py"], ["generation_io/supervisor.py:242-542", "generation_io/artifacts.py:1256-1314"]),
        ("AC6", "final test provenance and saved-evidence verification", ["test_owned_ref_verifier_rejects_missing_changed_and_unknown_paths", "test_saved_report_verifier_rejects_tampered_output_reference"], ["tests/test_revision2_validator.py"], ["tools/validate_revision2.py:222-241", "tools/revision3_evidence.py:240-277"]),
        ("D1", "verify and resume failure contracts", ["test_verify_failure_matrix_does_not_mutate_inputs", "test_resume_revalidates_all_inputs_and_publishes_fresh_kernel_stage"], ["tests/test_supervisor.py", "tests/test_resume.py"], ["generation_io/artifacts.py:850-1100"]),
        ("D2", "content-bound refs for code, tests, tools, and fixtures", ["test_owned_ref_verifier_rejects_missing_changed_and_unknown_paths"], ["tests/test_revision2_validator.py"], ["tools/revision3_evidence.py:45-57"]),
        ("D3", "late failure removes only receipt-owned complete output", ["test_late_supervisor_failure_invalidates_only_receipt_owned_complete", "test_late_supervisor_failure_invalidates_complete_when_report_is_refused", "test_late_supervisor_failure_preserves_complete_without_matching_receipts"], ["tests/test_supervisor.py"], ["generation_io/supervisor.py:796-853"]),
        ("D4", "shared task and aggregate budgets", ["test_attempt_state_is_initialized_once_with_shared_fixed_budgets", "test_shared_budget_is_cumulative_across_stage_invocations", "test_shared_budget_survives_independent_supervisor_processes"], ["tests/test_supervisor.py"], ["generation_io/supervisor.py:47-343"]),
        ("D5", "fixed package and runtime contract", ["test_package_contract_matches_p02_and_rejects_registry_changes", "test_revision4_fixed_package_runtime_preflight"], ["tests/test_revision2_validator.py"], ["tools/validate_revision2.py:76-164", "pyproject.toml:1-17"]),
    ]
    test_refs = {item["path"]: item for item in owned if "/tests/" in item["path"]}
    test_stage = next(item for item in stages if item["stage"] == "tests")
    result = []
    for ident, criterion, names, files, implementations in cases:
        refs = []
        for relative in files:
            path = str(base.PACKAGE / relative)
            if path not in test_refs or not any(name in Path(path).read_text(encoding="utf-8") for name in names):
                _fail("acceptance_map_test_evidence_missing")
            refs.append(test_refs[path])
        for reference in implementations:
            relative, _, bounds = reference.rpartition(":")
            try:
                first, _, last = bounds.partition("-")
                line_count = len((base.PACKAGE / relative).read_text(encoding="utf-8").splitlines())
                if int(first) < 1 or int(last or first) > line_count:
                    _fail("acceptance_map_implementation_ref_invalid")
            except (OSError, ValueError) as exc:
                raise base.ValidationError("acceptance_map_implementation_ref_invalid") from exc
        result.append({"id": ident, "criterion": criterion, "test_names": names, "test_file_refs": refs, "implementation_refs": implementations, "final_test_log_ref": test_stage["log"], "final_stage_report_ref": test_stage["stage_report"]})
    return result


def build_report() -> dict[str, Any]:
    release = base.validate_release_inputs()
    package = base.validate_package_contract(base.PACKAGE / "pyproject.toml", base.PACKAGE / "uv.lock", base.P02 / "pyproject.toml", base.P02 / "uv.lock")
    runtime, owned = base.validate_runtime(), current_owned_refs()
    stages, budget = collect_stage_results()
    for stage in stages:
        base.validate_owned_ref_set(stage["current_owned_refs_before"], owned)
        base.validate_owned_ref_set(stage["current_owned_refs_after"], owned)
    output_root = base.EVIDENCE / "selected-projection-01"
    try:
        from generation_io.artifacts import verify_generation_manifest
        manifest = verify_generation_manifest(output_root / "generation-manifest.json")
    except Exception as exc:
        raise base.ValidationError("generated_manifest_verification_failed") from exc
    counts = {"source_rows": 64, "excluded_non_stop": 0, "rejected_stop": 0, "outside_region": 63, "normalized_points": 1, "candidate_rows": 0}
    if manifest.get("schema_version") != 2 or manifest.get("counts") != counts or manifest.get("production_approved") is not False:
        _fail("generated_fixture_contract_mismatch")
    outputs = {name: base._ref(output_root / name) for name in sorted(OUTPUTS)}
    return {"schema_version": 2, "task_id": base.EXPECTED_TASK_ID, "base_commit": base.EXPECTED_BASE_COMMIT, "decision": "implementation_complete_parent_Sol_acceptance_required", "whole_project_complete": False, "production_approved": False, "input_refs": release["input_refs"], "input_ref_count": release["input_ref_count"], "external_input_refs_verified": release["external_input_refs_verified"], "old_G02_files_verified": release["old_G02_files_verified"], "changed_files": release["changed_files"], "package_contract": package, "runtime": runtime, "current_owned_refs": owned, "outputs": outputs, "validation_results": stages, "budget_workflow": budget, "acceptance_mapping": _acceptance_mapping(stages, owned), "preserved_refs": _preserved_refs(release), "residual_risks": ["This report is a handoff for parent Sol adoption, not an integration acceptance.", "Only the fixed 64-row selected-field projection and synthetic rectangle were executed.", "No complete 170-column raw CSV, full-month run, real Nagoya clip, quality approval, PR, database, or production claim is made."], "blocker": None}


def validate_saved_report(path: Path) -> dict[str, Any]:
    document = _object(path)
    output_refs = document.get("outputs")
    if isinstance(output_refs, dict):
        for reference in output_refs.values():
            base.validate_reference(reference)
    if document.get("schema_version") != 2 or document.get("task_id") != base.EXPECTED_TASK_ID or document.get("base_commit") != base.EXPECTED_BASE_COMMIT or document.get("whole_project_complete") is not False or document.get("production_approved") is not False or document.get("decision") != "implementation_complete_parent_Sol_acceptance_required":
        _fail("invalid_saved_report")
    release = base.validate_release_inputs()
    for key in ("input_refs", "input_ref_count", "external_input_refs_verified", "old_G02_files_verified", "changed_files"):
        if document.get(key) != release[key]:
            _fail("saved_report_input_refs_mismatch")
    owned = current_owned_refs()
    base.validate_owned_ref_set(document.get("current_owned_refs"), owned)
    package = base.validate_package_contract(base.PACKAGE / "pyproject.toml", base.PACKAGE / "uv.lock", base.P02 / "pyproject.toml", base.P02 / "uv.lock")
    if document.get("package_contract") != package or document.get("runtime") != base.validate_runtime():
        _fail("saved_report_runtime_or_package_mismatch")
    outputs, output_root = document.get("outputs"), base.EVIDENCE / "selected-projection-01"
    if not isinstance(outputs, dict) or set(outputs) != OUTPUTS:
        _fail("saved_report_output_set_mismatch")
    for name, reference in outputs.items():
        if base.validate_reference(reference) != base._ref(output_root / name):
            _fail("saved_report_output_reference_mismatch")
    try:
        from generation_io.artifacts import verify_generation_manifest
        verify_generation_manifest(output_root / "generation-manifest.json")
    except Exception as exc:
        raise base.ValidationError("saved_report_generation_verification_failed") from exc
    stages, workflow = collect_stage_results()
    if document.get("validation_results") != stages or document.get("budget_workflow") != workflow:
        _fail("saved_report_stage_or_budget_mismatch")
    if document.get("acceptance_mapping") != _acceptance_mapping(stages, owned) or document.get("preserved_refs") != _preserved_refs(release):
        _fail("saved_report_mapping_or_preserved_refs_mismatch")
    if not isinstance(document.get("residual_risks"), list) or document.get("blocker") is not None:
        _fail("saved_report_residual_risk_contract_mismatch")
    return document
