from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import stat
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any


PACKAGE = Path(__file__).resolve().parents[1]
REPO = PACKAGE.parents[1]
sys.path.insert(0, str(PACKAGE))
EVIDENCE = Path(
    "/opt/data/workspaces/graduation-research-evidence/unified-work/G02-revision4"
)
RELEASE = EVIDENCE / "operator-release.json"
ATTEMPT_STATE = EVIDENCE / "validation/budget-state.json"
P02 = Path("/opt/data/workspaces/graduation-research-evidence/unified-work/P02")
EXPECTED_TASK_ID = "t_322319de"
EXPECTED_BASE_COMMIT = "b3776c98d8834c6df7c281b4e1a3a1ad8542af0d"
EXPECTED_BRANCH = "graduation/g02-generation-io"
EXPECTED_PROJECT = {
    "name": "graduation-generation-io",
    "version": "0.1.0",
    "requires-python": "==3.13.5",
    "dependencies": ["numpy>=2.3,<2.4", "pandas>=2.3,<2.4", "scipy>=1.16,<1.17"],
}
EXPECTED_DEV = ["pytest>=8,<9"]
EXPECTED_PACKAGES = {"numpy": "2.3.5", "pandas": "2.3.3", "scipy": "1.16.3"}
EXPECTED_PYTHON = "3.13.5"
EXPECTED_REQUIRES_PYTHON = "==3.13.5"
EXPECTED_PYTEST = "8.4.2"


class ValidationError(RuntimeError):
    pass


def sha256_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def validate_reference(reference: Any) -> dict[str, Any]:
    if not isinstance(reference, dict) or set(reference) != {"path", "bytes", "sha256"}:
        raise ValidationError("reference_mismatch")
    raw_path = reference.get("path")
    if not isinstance(raw_path, str) or not raw_path or not _is_sha256(reference.get("sha256")):
        raise ValidationError("reference_mismatch")
    expected_bytes = reference.get("bytes")
    if isinstance(expected_bytes, bool) or not isinstance(expected_bytes, int) or expected_bytes < 0:
        raise ValidationError("reference_mismatch")
    path = Path(raw_path)
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise ValidationError("reference_mismatch")
        size, digest = sha256_file(path)
    except OSError as exc:
        raise ValidationError("reference_mismatch") from exc
    if size != expected_bytes or digest != reference["sha256"]:
        raise ValidationError("reference_mismatch")
    return reference


def validate_package_contract(
    project_path: Path,
    lock_path: Path,
    p02_project_path: Path,
    p02_lock_path: Path,
) -> dict[str, Any]:
    try:
        project = tomllib.loads(project_path.read_text(encoding="utf-8"))
        p02_project = tomllib.loads(p02_project_path.read_text(encoding="utf-8"))
        lock = tomllib.loads(lock_path.read_text(encoding="utf-8"))
        p02_lock = tomllib.loads(p02_lock_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ValidationError("package_contract_invalid") from exc

    project_meta = project.get("project")
    p02_meta = p02_project.get("project")
    if not isinstance(project_meta, dict) or not isinstance(p02_meta, dict):
        raise ValidationError("package_project_metadata_missing")
    for key, expected in EXPECTED_PROJECT.items():
        if project_meta.get(key) != expected:
            raise ValidationError("package_project_contract_mismatch")
    if project_meta.get("description") != "Candidate-only graduation research generation I/O":
        raise ValidationError("package_project_contract_mismatch")
    if project_meta.get("dependencies") != p02_meta.get("dependencies"):
        raise ValidationError("package_dependency_contract_mismatch")
    if {k: v for k, v in project_meta.items() if k not in {"name", "description"}} != {
        k: v for k, v in p02_meta.items() if k not in {"name", "description"}
    }:
        raise ValidationError("package_project_contract_mismatch")
    if project.get("dependency-groups", {}).get("dev") != EXPECTED_DEV:
        raise ValidationError("package_dev_dependencies_mismatch")
    if project.get("tool", {}).get("pytest", {}).get("ini_options", {}).get("testpaths") != [
        "tests"
    ]:
        raise ValidationError("package_pytest_config_mismatch")

    if lock.get("requires-python") != EXPECTED_REQUIRES_PYTHON or p02_lock.get(
        "requires-python"
    ) != EXPECTED_REQUIRES_PYTHON:
        raise ValidationError("lock_python_contract_mismatch")
    packages = lock.get("package")
    p02_packages = p02_lock.get("package")
    if not isinstance(packages, list) or not isinstance(p02_packages, list):
        raise ValidationError("lock_packages_missing")
    virtual = [entry for entry in packages if entry.get("source") == {"virtual": "."}]
    p02_virtual = [entry for entry in p02_packages if entry.get("source") == {"virtual": "."}]
    if len(virtual) != 1 or len(p02_virtual) != 1:
        raise ValidationError("lock_virtual_root_mismatch")
    if virtual[0].get("name") != EXPECTED_PROJECT["name"] or virtual[0].get(
        "version"
    ) != EXPECTED_PROJECT["version"]:
        raise ValidationError("lock_virtual_root_mismatch")
    if {k: v for k, v in virtual[0].items() if k != "name"} != {
        k: v for k, v in p02_virtual[0].items() if k != "name"
    }:
        raise ValidationError("lock_virtual_root_mismatch")
    registry = sorted(
        (entry for entry in packages if entry.get("source") != {"virtual": "."}),
        key=lambda entry: entry.get("name", ""),
    )
    p02_registry = sorted(
        (entry for entry in p02_packages if entry.get("source") != {"virtual": "."}),
        key=lambda entry: entry.get("name", ""),
    )
    if registry != p02_registry:
        raise ValidationError("registry_package_mismatch")
    return {"registry_package_count": len(registry), "root_package": virtual[0]["name"]}


def validate_runtime() -> dict[str, Any]:
    versions = {name: importlib.metadata.version(name) for name in EXPECTED_PACKAGES}
    pytest_version = importlib.metadata.version("pytest")
    interpreter = Path(sys.executable).resolve()
    expected_interpreter = (P02 / ".venv/bin/python").resolve()
    if (
        platform.python_version() != EXPECTED_PYTHON
        or versions != EXPECTED_PACKAGES
        or pytest_version != EXPECTED_PYTEST
        or interpreter != expected_interpreter
    ):
        raise ValidationError("runtime_contract_mismatch")
    return {
        "python": platform.python_version(),
        "executable": str(interpreter),
        "byteorder": sys.byteorder,
        "platform": platform.platform(),
        "packages": versions,
        "pytest": pytest_version,
    }


def write_new_report(path: Path, document: dict[str, Any]) -> None:
    path = Path(path)
    if not path.parent.is_dir() or path.is_symlink():
        raise ValidationError("report_parent_invalid")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    payload = (json.dumps(document, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode(
        "utf-8"
    )
    fd = None
    try:
        fd = os.open(temporary, flags, 0o600)
        with os.fdopen(fd, "wb") as stream:
            fd = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path, follow_symlinks=False)
        temporary.unlink()
    except FileExistsError as exc:
        if fd is not None:
            os.close(fd)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise ValidationError("report_already_exists") from exc
    except OSError as exc:
        if fd is not None:
            os.close(fd)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise ValidationError("report_write_failed") from exc


def current_owned_refs(package_root: Path = PACKAGE) -> list[dict[str, Any]]:
    owned_suffixes = {".py", ".json", ".toml", ".lock", ".yaml", ".yml", ".csv", ".jsonl"}
    refs = []
    for candidate in sorted(package_root.rglob("*")):
        relative = candidate.relative_to(package_root)
        if "__pycache__" in relative.parts or ".pytest_cache" in relative.parts:
            continue
        if not candidate.is_file() or (
            candidate.suffix.lower() not in owned_suffixes and "fixtures" not in relative.parts
        ):
            continue
        refs.append(_ref(candidate))
    return refs


def validate_owned_ref_set(references: Any, expected: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(references, list):
        raise ValidationError("owned_refs_set_mismatch")
    by_path: dict[str, dict[str, Any]] = {}
    for reference in references:
        if not isinstance(reference, dict) or not isinstance(reference.get("path"), str):
            raise ValidationError("owned_refs_set_mismatch")
        if reference["path"] in by_path:
            raise ValidationError("owned_refs_set_mismatch")
        by_path[reference["path"]] = reference
    expected_by_path = {item["path"]: item for item in expected}
    if set(by_path) != set(expected_by_path):
        raise ValidationError("owned_refs_set_mismatch")
    result = []
    for path in sorted(expected_by_path):
        reference = validate_reference(by_path[path])
        if reference != expected_by_path[path]:
            raise ValidationError("owned_ref_content_mismatch")
        result.append(reference)
    return result


def _legacy_validate_saved_report(path: Path) -> dict[str, Any]:
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise ValidationError("invalid_saved_report")
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError("invalid_saved_report") from exc
    if (
        not isinstance(document, dict)
        or document.get("task_id") != EXPECTED_TASK_ID
        or document.get("base_commit") != EXPECTED_BASE_COMMIT
        or not isinstance(document.get("input_refs"), list)
        or not isinstance(document.get("validation_results"), list)
        or not isinstance(document.get("decision"), str)
    ):
        raise ValidationError("invalid_saved_report")
    outputs = document.get("outputs")
    if not isinstance(outputs, dict) or not outputs:
        raise ValidationError("saved_report_outputs_missing")
    for reference in outputs.values():
        validate_reference(reference)
    release_info = validate_release_inputs()
    if (
        document["input_refs"] != release_info["input_refs"]
        or document.get("input_ref_count") != release_info["input_ref_count"]
        or document.get("external_input_refs_verified")
        != release_info["external_input_refs_verified"]
        or document.get("old_G02_files_verified") != release_info["old_G02_files_verified"]
        or document.get("changed_files") != release_info["changed_files"]
    ):
        raise ValidationError("saved_report_input_refs_mismatch")
    package = validate_package_contract(
        PACKAGE / "pyproject.toml",
        PACKAGE / "uv.lock",
        P02 / "pyproject.toml",
        P02 / "uv.lock",
    )
    if document.get("package_contract") != package:
        raise ValidationError("saved_report_package_contract_mismatch")
    runtime = validate_runtime()
    if document.get("runtime") != runtime:
        raise ValidationError("saved_report_runtime_mismatch")
    expected_output_root = EVIDENCE / "selected-projection-01"
    expected_names = {
        "generation-manifest.json",
        "normalized-points.jsonl",
        "candidate-features.jsonl",
        "rejected-records.jsonl",
        "source-manifest.json",
    }
    if set(outputs) != expected_names:
        raise ValidationError("saved_report_output_set_mismatch")
    for name, reference in outputs.items():
        if reference.get("path") != str(expected_output_root / name):
            raise ValidationError("saved_report_output_path_mismatch")
    try:
        from generation_io.artifacts import verify_generation_manifest

        verify_generation_manifest(expected_output_root / "generation-manifest.json")
    except Exception as exc:
        raise ValidationError(f"saved_report_generation_verification_failed:{exc}") from exc
    results = document["validation_results"]
    if not isinstance(results, list) or [result.get("stage") for result in results] != [
        "tests",
        "generate",
        "verify",
    ]:
        raise ValidationError("saved_report_validation_results_mismatch")
    for result in results:
        if not isinstance(result, dict) or result.get("exit_code") != 0:
            raise ValidationError("saved_report_validation_result_failed")
        log_ref = {
            "path": result.get("log_path"),
            "bytes": result.get("log_bytes"),
            "sha256": result.get("log_sha256"),
        }
        validate_reference(log_ref)
        budget_path = Path(result.get("budget_report_path", ""))
        if not budget_path.is_relative_to(EVIDENCE / "validation/attempts"):
            raise ValidationError("saved_report_budget_path_mismatch")
        if sha256_file(budget_path)[1] != result.get("budget_report_sha256"):
            raise ValidationError("saved_report_budget_hash_mismatch")
        budget = _read_object(budget_path)
        if (
            budget.get("status") != "complete"
            or budget.get("exit_code") != 0
            or budget.get("command") != result.get("command")
            or budget.get("cwd") != result.get("cwd")
            or budget.get("log", {}).get("path") != result.get("log_path")
            or budget.get("log", {}).get("sha256") != result.get("log_sha256")
        ):
            raise ValidationError("saved_report_budget_contract_mismatch")
    return document


def validate_release_inputs(release_path: Path = RELEASE) -> dict[str, Any]:
    release = _read_object(release_path)
    if (
        release.get("task_id") != EXPECTED_TASK_ID
        or release.get("base_commit") != EXPECTED_BASE_COMMIT
        or release.get("branch") != EXPECTED_BRANCH
        or release.get("whole_project_complete") is not False
        or release.get("production_approved") is not False
        or any(
            release.get(key) is not False
            for key in (
                "G02_original_accepted",
                "G02_revision2_accepted",
                "G02_revision3_accepted",
            )
        )
    ):
        raise ValidationError("release_identity_mismatch")
    input_refs = release.get("input_refs")
    old_refs = release.get("old_G02_files")
    baseline = release.get("implementation_baseline")
    if not all(isinstance(value, list) for value in (input_refs, old_refs, baseline)):
        raise ValidationError("release_reference_sets_missing")
    if len({item.get("path") for item in input_refs if isinstance(item, dict)}) != len(input_refs):
        raise ValidationError("duplicate_release_reference")

    owned_prefix = str(PACKAGE) + os.sep
    checked = 0
    for ref in input_refs:
        if not isinstance(ref, dict) or set(ref) != {"path", "bytes", "sha256"}:
            raise ValidationError("invalid_release_reference")
        if str(ref["path"]).startswith(owned_prefix):
            if not _is_sha256(ref.get("sha256")) or not isinstance(ref.get("bytes"), int):
                raise ValidationError("invalid_release_reference")
        else:
            validate_reference(ref)
            checked += 1
    for ref in old_refs:
        validate_reference(ref)
    for ref in baseline:
        if not isinstance(ref, dict) or set(ref) != {"path", "bytes", "sha256"}:
            raise ValidationError("invalid_implementation_baseline")
        if not str(ref["path"]).startswith(owned_prefix) or not _is_sha256(ref.get("sha256")):
            raise ValidationError("invalid_implementation_baseline")

    freeze = next(
        (ref for ref in old_refs if ref["path"].endswith("/G02/operator-input-freeze.json")),
        None,
    )
    if freeze is None:
        raise ValidationError("operator_freeze_reference_missing")
    validate_reference(freeze)

    current_commit = subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    branch = subprocess.run(
        ["git", "-C", str(REPO), "branch", "--show-current"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if current_commit != EXPECTED_BASE_COMMIT or branch != EXPECTED_BRANCH:
        raise ValidationError("worktree_identity_mismatch")

    baseline_by_path = {item["path"]: item for item in baseline}
    present_paths = set()
    for path in PACKAGE.rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        absolute = str(path.resolve())
        present_paths.add(absolute)
    changed = []
    for path in sorted(present_paths | set(baseline_by_path)):
        if path not in present_paths:
            changed.append(_relative_package_path(path))
            continue
        current = _ref(Path(path))
        original = baseline_by_path.get(path)
        if original is None or current != original:
            changed.append(_relative_package_path(path))
    return {
        "release": release,
        "input_refs": input_refs,
        "old_G02_files": old_refs,
        "input_ref_count": len(input_refs),
        "external_input_refs_verified": checked,
        "old_G02_files_verified": len(old_refs),
        "changed_files": changed,
        "worktree": {"base_commit": current_commit, "branch": branch},
    }


def _legacy_collect_stage_results(evidence_dir: Path = EVIDENCE) -> list[dict[str, Any]]:
    attempts = evidence_dir / "validation/attempts"
    reports = []
    if not attempts.is_dir():
        raise ValidationError("stage_attempts_missing")
    for report_path in sorted(attempts.glob("attempt-*/*.json"), key=lambda item: item.stat().st_mtime_ns):
        if report_path.name not in {"tests.json", "generate.json", "verify.json"}:
            continue
        try:
            report = _read_object(report_path)
        except ValidationError:
            continue
        if report.get("stage") not in {"tests", "generate", "verify"}:
            continue
        reports.append((report_path, report))
    selected: dict[str, tuple[Path, dict[str, Any]]] = {}
    for report_path, report in reports:
        stage = report["stage"]
        if report.get("status") == "complete" and report.get("exit_code") == 0:
            if stage == "generate" and report.get("output_path") != str(
                evidence_dir / "selected-projection-01"
            ):
                continue
            if stage == "verify" and report.get("output_path") != str(
                evidence_dir / "selected-projection-01"
            ):
                continue
            selected[stage] = (report_path, report)
    required = {"tests", "generate", "verify"}
    if set(selected) != required:
        raise ValidationError("required_successful_stage_evidence_missing")
    result = []
    for stage in ("tests", "generate", "verify"):
        path, report = selected[stage]
        log = report.get("log")
        if not isinstance(log, dict):
            raise ValidationError("stage_log_reference_missing")
        log_ref = {"path": log.get("path"), "bytes": log.get("bytes"), "sha256": log.get("sha256")}
        validate_reference(log_ref)
        if report.get("log", {}).get("path") != str(path.with_suffix(".log")):
            raise ValidationError("stage_log_path_mismatch")
        result.append(
            {
                "stage": stage,
                "command": report.get("command"),
                "cwd": report.get("cwd"),
                "exit_code": report.get("exit_code"),
                "log_path": log_ref["path"],
                "log_bytes": log_ref["bytes"],
                "log_sha256": log_ref["sha256"],
                "budget_report_path": str(path),
                "budget_report_sha256": sha256_file(path)[1],
            }
        )
    return result


def _legacy_build_report() -> dict[str, Any]:
    release_info = validate_release_inputs()
    package = validate_package_contract(
        PACKAGE / "pyproject.toml",
        PACKAGE / "uv.lock",
        P02 / "pyproject.toml",
        P02 / "uv.lock",
    )
    runtime = validate_runtime()
    stage_results = collect_stage_results()
    output_root = EVIDENCE / "selected-projection-01"
    manifest_path = output_root / "generation-manifest.json"
    manifest_ref = _ref(manifest_path)
    manifest = _read_object(manifest_path)
    if (
        manifest.get("schema_version") != 2
        or manifest.get("counts")
        != {
            "source_rows": 64,
            "excluded_non_stop": 0,
            "rejected_stop": 0,
            "outside_region": 63,
            "normalized_points": 1,
            "candidate_rows": 0,
        }
        or manifest.get("production_approved") is not False
    ):
        raise ValidationError("generated_fixture_contract_mismatch")
    output_refs = {
        name: _ref(output_root / name)
        for name in (
            "generation-manifest.json",
            "normalized-points.jsonl",
            "candidate-features.jsonl",
            "rejected-records.jsonl",
            "source-manifest.json",
        )
    }
    return {
        "schema_version": 1,
        "task_id": EXPECTED_TASK_ID,
        "base_commit": EXPECTED_BASE_COMMIT,
        "decision": "implementation_complete_parent_Sol_acceptance_required",
        "whole_project_complete": False,
        "input_refs": release_info["input_refs"],
        "input_ref_count": release_info["input_ref_count"],
        "external_input_refs_verified": release_info["external_input_refs_verified"],
        "old_G02_files_verified": release_info["old_G02_files_verified"],
        "changed_files": release_info["changed_files"],
        "package_contract": package,
        "runtime": runtime,
        "outputs": output_refs,
        "validation_results": stage_results,
        "acceptance_mapping": _acceptance_mapping(),
        "residual_risks": [
            "This report is a handoff for parent Sol adoption, not an integration acceptance.",
            "Only the fixed 64-row selected-field projection and synthetic rectangle were executed.",
            "No complete 170-column raw CSV, full-month run, real Nagoya clip, quality approval, PR, database, or production claim is made.",
        ],
        "blocker": None,
    }


def _legacy_acceptance_mapping() -> list[dict[str, Any]]:
    return [
        {
            "criterion": "original G02 AC: provenance, parser, geometry, exact kernel, and failure handling",
            "evidence": [
                "tests/test_raw_source.py",
                "tests/test_source.py",
                "tests/test_normalize.py",
                "tests/test_region.py",
                "tests/test_artifacts.py",
                "tests/test_resume.py",
                "tests/test_supervisor.py",
            ],
            "status": "verified_by_full_test_stage",
        },
        {
            "criterion": "revision2 manifest/ref/params/resume/draft publication contracts",
            "evidence": ["tests/test_artifacts.py", "tests/test_resume.py"],
            "status": "verified_by_full_test_stage",
        },
        {
            "criterion": "supervisor cgroup headroom, direct-child RSS, timeout, output/report caps, and read-only verify",
            "evidence": ["tests/test_supervisor.py"],
            "status": "verified_by_full_test_stage",
        },
        {
            "criterion": "P02 package registry lock and fixed runtime contract",
            "evidence": ["tools/validate_revision2.py", "pyproject.toml", "uv.lock"],
            "status": "verified_by_revision2_verifier",
        },
    ]


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"invalid_json:{path.name}") from exc
    if not isinstance(value, dict):
        raise ValidationError(f"invalid_json_object:{path.name}")
    return value


def _ref(path: Path) -> dict[str, Any]:
    size, digest = sha256_file(path)
    return {"path": str(path), "bytes": size, "sha256": digest}


def _relative_package_path(path: str) -> str:
    try:
        return Path(path).relative_to(PACKAGE).as_posix()
    except ValueError:
        return path


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        char in "0123456789abcdef" for char in value
    )


def validate_saved_report(path: Path) -> dict[str, Any]:
    from tools.revision3_evidence import validate_saved_report as verify

    return verify(path)


def build_report() -> dict[str, Any]:
    from tools.revision3_evidence import build_report as build

    return build()


def collect_stage_results(evidence_dir: Path = EVIDENCE) -> list[dict[str, Any]]:
    from tools.revision3_evidence import collect_stage_results as collect

    return collect(evidence_dir)[0]


def _acceptance_mapping() -> list[dict[str, Any]]:
    from tools.revision3_evidence import _acceptance_mapping as acceptance, current_owned_refs, collect_stage_results as collect

    stages, _ = collect()
    return acceptance(stages, current_owned_refs())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.report.exists() or args.report.is_symlink():
            report = validate_saved_report(args.report)
        else:
            report = build_report()
            write_new_report(args.report, report)
    except (ValidationError, OSError, subprocess.CalledProcessError) as exc:
        print(json.dumps({"status": "FAIL", "error": str(exc)}, ensure_ascii=False))
        return 2
    print(
        json.dumps(
            {
                "status": "PASS",
                "task_id": report["task_id"],
                "input_ref_count": report.get("input_ref_count"),
                "changed_file_count": len(report.get("changed_files", [])),
                "decision": report["decision"],
                "report": str(args.report),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
