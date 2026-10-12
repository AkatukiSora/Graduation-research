from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from tools import revision3_evidence
from tools.validate_revision2 import (
    ValidationError,
    validate_package_contract,
    validate_owned_ref_set,
    validate_reference,
    validate_release_inputs,
    validate_runtime,
    validate_saved_report,
    write_new_report,
)


PACKAGE = Path("/opt/data/workspaces/graduation-research-g02/backend/generation_io")
P02 = Path("/opt/data/workspaces/graduation-research-evidence/unified-work/P02")
RELEASE_V4 = Path("/opt/data/workspaces/graduation-research-evidence/unified-work/G02-revision4/operator-release.json")


def test_revision4_release_receipt_passes_parent_precheck():
    release = validate_release_inputs(RELEASE_V4)["release"]

    assert release["G02_original_accepted"] is False
    assert release["G02_revision2_accepted"] is False
    assert release["G02_revision3_accepted"] is False
    assert release["whole_project_complete"] is False
    assert release["production_approved"] is False


def test_revision4_fixed_package_runtime_preflight():
    package = validate_package_contract(
        PACKAGE / "pyproject.toml",
        PACKAGE / "uv.lock",
        P02 / "pyproject.toml",
        P02 / "uv.lock",
    )
    runtime = validate_runtime()

    assert package["root_package"] == "graduation-generation-io"
    assert runtime["executable"] == str((P02 / ".venv/bin/python").resolve())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("G02_original_accepted", True),
        ("G02_revision2_accepted", True),
        ("G02_revision2_accepted", None),
        ("G02_revision3_accepted", True),
        ("whole_project_complete", True),
        ("production_approved", True),
        ("branch", "unexpected-branch"),
        ("task_id", "unexpected-task"),
        ("base_commit", "0" * 40),
    ],
)
def test_revision4_release_precheck_rejects_wrong_identity_and_approval_flags(monkeypatch, field, value):
    release = json.loads(RELEASE_V4.read_text(encoding="utf-8"))
    release[field] = value
    monkeypatch.setattr("tools.validate_revision2._read_object", lambda _path: release)
    monkeypatch.setattr(
        "tools.validate_revision2.validate_reference",
        lambda _reference: pytest.fail("identity must be rejected before validating refs"),
    )

    with pytest.raises(ValidationError, match="release_identity_mismatch"):
        validate_release_inputs(RELEASE_V4)


def test_reference_verifier_checks_path_size_and_sha256(tmp_path):
    source = tmp_path / "frozen.json"
    source.write_bytes(b"frozen\n")
    reference = {
        "path": str(source),
        "bytes": source.stat().st_size,
        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
    }

    assert validate_reference(reference) == reference
    with pytest.raises(ValidationError, match="reference_mismatch"):
        validate_reference({**reference, "sha256": "0" * 64})


def test_package_contract_matches_p02_and_rejects_registry_changes(tmp_path):
    project = tmp_path / "pyproject.toml"
    lock = tmp_path / "uv.lock"
    shutil.copyfile(PACKAGE / "pyproject.toml", project)
    shutil.copyfile(PACKAGE / "uv.lock", lock)

    validate_package_contract(project, lock, P02 / "pyproject.toml", P02 / "uv.lock")

    lock.write_text(
        lock.read_text(encoding="utf-8").replace(
            'name = "numpy"\nversion = "2.3.5"',
            'name = "numpy"\nversion = "2.3.4"',
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="registry_package_mismatch"):
        validate_package_contract(project, lock, P02 / "pyproject.toml", P02 / "uv.lock")


def test_write_new_report_does_not_overwrite_existing_report(tmp_path):
    report = tmp_path / "pipeline-manifest.json"
    report.write_bytes(b"saved evidence\n")

    with pytest.raises(ValidationError, match="report_already_exists"):
        write_new_report(report, {"status": "complete"})

    assert report.read_bytes() == b"saved evidence\n"


def test_saved_report_verifier_rejects_tampered_output_reference(tmp_path):
    report = tmp_path / "pipeline-manifest.json"
    report.write_text(
        '{"task_id":"t_322319de","base_commit":"b3776c98d8834c6df7c281b4e1a3a1ad8542af0d",'
        '"input_refs":[],"validation_results":[],"decision":"incomplete",'
        '"outputs":{"artifact":{"path":"/missing/artifact","bytes":1,"sha256":"'
        + "0" * 64
        + '"}}}',
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="reference_mismatch"):
        validate_saved_report(report)


def test_owned_ref_verifier_rejects_missing_changed_and_unknown_paths(tmp_path):
    source = tmp_path / "source.py"
    source.write_bytes(b"value = 1\n")
    expected = [{
        "path": str(source),
        "bytes": source.stat().st_size,
        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
    }]
    assert validate_owned_ref_set(expected, expected) == expected
    with pytest.raises(ValidationError, match="owned_refs_set_mismatch"):
        validate_owned_ref_set([], expected)
    with pytest.raises(ValidationError, match="owned_refs_set_mismatch"):
        validate_owned_ref_set(expected + expected, expected)
    source.write_bytes(b"value = 2\n")
    changed = [{
        "path": str(source),
        "bytes": source.stat().st_size,
        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
    }]
    with pytest.raises(ValidationError, match="owned_ref_content_mismatch"):
        validate_owned_ref_set(changed, expected)


def test_revision3_acceptance_map_includes_independent_process_budget_regression():
    stages = [
        {
            "stage": "tests",
            "log": {"path": "/evidence/tests.log", "bytes": 1, "sha256": "0" * 64},
            "stage_report": {"path": "/evidence/tests.json", "bytes": 1, "sha256": "0" * 64},
        }
    ]

    mapping = revision3_evidence._acceptance_mapping(stages, revision3_evidence.current_owned_refs())

    d4 = next(item for item in mapping if item["id"] == "D4")
    assert "test_shared_budget_survives_independent_supervisor_processes" in d4["test_names"]
    assert d4["final_test_log_ref"] == stages[0]["log"]
