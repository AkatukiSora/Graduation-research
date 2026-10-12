import hashlib
import importlib.util
import json
import math
import sys
import uuid
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

from generation_io import artifacts
from generation_io.artifacts import (
    ArtifactError,
    candidate_feature_rows,
    generate_candidate_artifact,
    jsonl_bytes,
    verify_generation_manifest,
)


ROOT = Path("/opt/data/workspaces/graduation-research-g02/backend/generation_io")
P05_FEATURES = Path("/opt/data/workspaces/graduation-research-p05/backend/generation/generation/features.py")
G01 = Path("/opt/data/workspaces/graduation-research-evidence/unified-work/G01/fixtures")
SOURCE = G01 / "selected-fields.jsonl"
CONTRACT = G01 / "fixture-manifest.json"
BOUNDARY = ROOT / "tests/fixtures/region-a.geojson"


def direct_public_kernel():
    spec = importlib.util.spec_from_file_location("g02_independent_p05_features", P05_FEATURES)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module.build_candidate_features


def normalized_record(uid, lon, lat, source_row):
    return {
        "entity_id": f"jp:aichi:nagoya/202602/{uid}",
        "source_row": source_row,
        "source_month": "202602",
        "raw_uid": uid,
        "record_id": uid,
        "jartic_record_id": uid,
        "ユニークキー": uid,
        "lon": lon,
        "lat": lat,
        "production_approved": False,
    }


def test_jsonl_keeps_signed_zero_unicode_and_rejects_nonfinite_values():
    encoded = jsonl_bytes([{"point": -0.0, "名前": "候補"}])
    assert encoded == b'{"point":-0.0,"\xe5\x90\x8d\xe5\x89\x8d":"\xe5\x80\x99\xe8\xa3\x9c"}\n'
    with pytest.raises(ArtifactError, match="non_finite_json_value"):
        jsonl_bytes([{"invalid": math.nan}])


def test_candidate_rows_match_independent_read_only_p05_public_api_exactly():
    records = [
        normalized_record("A" * 32, 136.9331, 35.04593, 10),
        normalized_record("B" * 32, 136.93315, 35.04594, 11),
        normalized_record("C" * 32, 136.9332, 35.04595, 12),
        normalized_record("D" * 32, 136.93325, 35.04596, 13),
    ]
    records_snapshot = json.loads(json.dumps(records))
    points = pd.DataFrame(
        [
            {
                "lon": row["lon"],
                "lat": row["lat"],
                "jartic_record_id": row["jartic_record_id"],
                "ユニークキー": row["ユニークキー"],
            }
            for row in records
        ]
    )
    snapshot = points.copy(deep=True)
    expected = direct_public_kernel()(
        points.copy(deep=True), max_candidates=6, candidate_radius=80.0
    )

    actual = candidate_feature_rows(records, "1" * 64)
    projected_actual = pd.DataFrame(
        [{column: row[column] for column in expected.columns} for row in actual],
        columns=expected.columns,
    )
    assert_frame_equal(points, snapshot, check_exact=True)
    assert_frame_equal(expected, projected_actual, check_exact=True)
    reloaded = [
        json.loads(line)
        for line in jsonl_bytes(actual).splitlines()
    ]
    assert len(reloaded) == len(actual) == len(expected)
    assert records == records_snapshot
    for before, after in zip(actual, reloaded):
        assert list(before) == list(after)
        for column, value in before.items():
            if isinstance(value, (float, np.floating)):
                assert float(value).hex() == after[column].hex()
            else:
                assert value == after[column]
    for expected_row, actual_row in zip(expected.to_dict(orient="records"), actual):
        for column, value in expected_row.items():
            if isinstance(value, (float, np.floating)):
                assert float(value).hex() == float(actual_row[column]).hex()
    assert all(row["bearing_semantics"] == "candidate_point_neighbor_only" for row in actual)


def test_empty_single_isolated_coincident_and_tied_point_sets_keep_expected_counts():
    single = [normalized_record("S" * 32, 136.0, 35.0, 20)]
    isolated = [
        normalized_record("I" * 32, 136.0, 35.0, 21),
        normalized_record("J" * 32, 137.0, 35.0, 22),
    ]
    assert candidate_feature_rows([], "2" * 64) == []
    assert candidate_feature_rows(single, "3" * 64) == []
    assert candidate_feature_rows(isolated, "4" * 64) == []

    tied = [
        normalized_record("A" * 32, 136.0, 35.0, 31),
        normalized_record("B" * 32, 136.0001, 35.0, 32),
        normalized_record("C" * 32, 136.0, 35.0001, 33),
        normalized_record("D" * 32, 136.0001, 35.0001, 34),
    ]
    coincident = [
        normalized_record("E" * 32, 136.0, 35.0, 35),
        normalized_record("F" * 32, 136.0, 35.0, 36),
    ]
    for records in (tied, coincident):
        points = pd.DataFrame(
            [
                {
                    "lon": row["lon"],
                    "lat": row["lat"],
                    "jartic_record_id": row["jartic_record_id"],
                    "ユニークキー": row["ユニークキー"],
                }
                for row in records
            ]
        )
        expected = direct_public_kernel()(
            points.copy(deep=True), max_candidates=6, candidate_radius=80.0
        )
        actual = candidate_feature_rows(records, "5" * 64)
        projected = pd.DataFrame(
            [{column: row[column] for column in expected.columns} for row in actual],
            columns=expected.columns,
        )
        assert_frame_equal(expected, projected, check_exact=True)
        assert all(row["production_approved"] is False for row in actual)


def test_generate_verify_is_fresh_only_and_reconciles_candidate_counts(tmp_path):
    output = tmp_path / "selected-projection"
    manifest_path = generate_candidate_artifact(
        source=SOURCE,
        source_contract=CONTRACT,
        input_kind="selected_projection",
        boundary=BOUNDARY,
        region_id="fixture:region-a",
        output=output,
    )
    manifest = verify_generation_manifest(manifest_path)

    counts = manifest["counts"]
    assert counts["source_rows"] == 64
    assert counts["source_rows"] == (
        counts["excluded_non_stop"]
        + counts["rejected_stop"]
        + counts["outside_region"]
        + counts["normalized_points"]
    )
    assert manifest["schema_version"] == 2
    assert manifest["status"] == "complete_candidate_artifact_not_quality_approved"
    assert manifest["production_approved"] is False
    assert manifest["boundary"]["synthetic_fixture"] is True
    assert counts["normalized_points"] == 1
    assert counts["candidate_rows"] == 0
    assert manifest["parameters"]["kernel_invocations"] == 0
    assert (output / "generation-manifest.json").is_file()
    with pytest.raises(ArtifactError, match="output_exists"):
        generate_candidate_artifact(
            source=SOURCE,
            source_contract=CONTRACT,
            input_kind="selected_projection",
            boundary=BOUNDARY,
            region_id="fixture:region-a",
            output=output,
        )


def test_generation_failure_publishes_only_failure_report(tmp_path):
    changed_source = tmp_path / "changed.jsonl"
    changed_source.write_bytes(SOURCE.read_bytes() + b"\n")
    output = tmp_path / "failed-run"

    with pytest.raises(ArtifactError, match="source_hash_mismatch"):
        generate_candidate_artifact(
            source=changed_source,
            source_contract=CONTRACT,
            input_kind="selected_projection",
            boundary=BOUNDARY,
            region_id="fixture:region-a",
            output=output,
        )

    assert not (output / "generation-manifest.json").exists()
    assert sorted(path.name for path in output.iterdir()) == ["failure-report.json"]
    failure = json.loads((output / "failure-report.json").read_text())
    assert failure["error_code"] == "source_hash_mismatch"


def test_verifier_rejects_candidate_count_not_matching_hashed_jsonl(tmp_path):
    output = tmp_path / "count-tamper"
    manifest_path = generate_candidate_artifact(
        source=SOURCE,
        source_contract=CONTRACT,
        input_kind="selected_projection",
        boundary=BOUNDARY,
        region_id="fixture:region-a",
        output=output,
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["counts"]["candidate_rows"] = 1
    manifest["candidate_rows"] = 1
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ArtifactError, match="candidate_count_reconciliation_failed"):
        verify_generation_manifest(manifest_path)


def test_verifier_rejects_frozen_parameter_tampering(tmp_path):
    output = tmp_path / "parameter-tamper"
    manifest_path = generate_candidate_artifact(
        source=SOURCE,
        source_contract=CONTRACT,
        input_kind="selected_projection",
        boundary=BOUNDARY,
        region_id="fixture:region-a",
        output=output,
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["parameters"]["max_candidates"] = 999
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ArtifactError, match="manifest_parameter_mismatch"):
        verify_generation_manifest(manifest_path)


def test_verifier_rejects_missing_required_parent_reference(tmp_path):
    output = tmp_path / "missing-parent-ref"
    manifest_path = generate_candidate_artifact(
        source=SOURCE,
        source_contract=CONTRACT,
        input_kind="selected_projection",
        boundary=BOUNDARY,
        region_id="fixture:region-a",
        output=output,
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    del manifest["parent_refs"]["selected_source"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ArtifactError, match="parent_reference_roles_mismatch"):
        verify_generation_manifest(manifest_path)


def test_verifier_rejects_saved_legacy_v1_manifest_without_mutating_it():
    saved_manifest = Path(
        "/opt/data/workspaces/graduation-research-evidence/unified-work/G02/"
        "selected-projection-01/generation-manifest.json"
    )
    before = saved_manifest.read_bytes()

    with pytest.raises(ArtifactError, match="unsupported_manifest_schema"):
        verify_generation_manifest(saved_manifest)

    assert saved_manifest.read_bytes() == before


def test_complete_manifest_is_not_published_before_draft_verification(
    tmp_path, monkeypatch
):
    output = tmp_path / "draft-verification-failure"
    observed = []

    def fail_draft_verification(path):
        observed.append(
            (Path(path).name, (output / "generation-manifest.json").exists())
        )
        raise ArtifactError("injected_manifest_validation_failure")

    monkeypatch.setattr("generation_io.artifacts.verify_generation_manifest", fail_draft_verification)

    with pytest.raises(ArtifactError, match="injected_manifest_validation_failure"):
        generate_candidate_artifact(
            source=SOURCE,
            source_contract=CONTRACT,
            input_kind="selected_projection",
            boundary=BOUNDARY,
            region_id="fixture:region-a",
            output=output,
        )

    assert observed == [(".generation-manifest.draft.json", False)]
    assert not (output / "generation-manifest.json").exists()
    assert (output / "failure-report.json").is_file()


def test_output_symlink_and_lock_collision_are_rejected(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    symlink = tmp_path / "linked-output"
    symlink.symlink_to(target, target_is_directory=True)
    with pytest.raises(ArtifactError, match="output_exists"):
        generate_candidate_artifact(
            source=SOURCE,
            source_contract=CONTRACT,
            input_kind="selected_projection",
            boundary=BOUNDARY,
            region_id="fixture:region-a",
            output=symlink,
        )

    collision_output = tmp_path / "collision"
    collision_lock = tmp_path / ".collision.lock"
    collision_lock.write_text("owned by another process")
    with pytest.raises(ArtifactError, match="output_lock_exists"):
        generate_candidate_artifact(
            source=SOURCE,
            source_contract=CONTRACT,
            input_kind="selected_projection",
            boundary=BOUNDARY,
            region_id="fixture:region-a",
            output=collision_output,
        )
    assert not collision_output.exists()


def test_supervised_generation_publishes_invocation_bound_ownership_receipts(tmp_path, monkeypatch):
    output = tmp_path / "selected-projection"
    attempt_root = tmp_path / "validation/attempts"
    attempt = attempt_root / f"attempt-{uuid.uuid4().hex}"
    attempt.mkdir(parents=True)
    monkeypatch.setattr(artifacts, "ATTEMPT_ROOT", attempt_root)
    ownership_receipt = attempt / "ownership.json"
    invocation_id = "invocation-0123456789abcdef"

    manifest_path = generate_candidate_artifact(
        source=SOURCE,
        source_contract=CONTRACT,
        input_kind="selected_projection",
        boundary=BOUNDARY,
        region_id="fixture:region-a",
        output=output,
        ownership_receipt=ownership_receipt,
        invocation_id=invocation_id,
    )

    ownership = json.loads(ownership_receipt.read_text(encoding="utf-8"))
    published = json.loads((attempt / "published-files.json").read_text(encoding="utf-8"))
    root_stat = output.stat()
    manifest_stat = manifest_path.stat()
    assert ownership == {
        "receipt_version": 1,
        "invocation_id": invocation_id,
        "output_path": str(output.resolve()),
        "root_st_dev": root_stat.st_dev,
        "root_st_ino": root_stat.st_ino,
    }
    assert published == {
        "receipt_version": 1,
        "invocation_id": invocation_id,
        "output_path": str(output.resolve()),
        "root_st_dev": root_stat.st_dev,
        "root_st_ino": root_stat.st_ino,
        "complete_path": str(manifest_path.resolve()),
        "complete_st_dev": manifest_stat.st_dev,
        "complete_st_ino": manifest_stat.st_ino,
        "complete_bytes": manifest_stat.st_size,
        "complete_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    }


def test_public_generation_does_not_require_or_create_supervisor_receipts(tmp_path):
    output = tmp_path / "public-generation"

    manifest_path = generate_candidate_artifact(
        source=SOURCE,
        source_contract=CONTRACT,
        input_kind="selected_projection",
        boundary=BOUNDARY,
        region_id="fixture:region-a",
        output=output,
    )

    assert manifest_path.is_file()
    assert not (output / "ownership.json").exists()
