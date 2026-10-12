from __future__ import annotations

import csv
import hashlib
import json
import shutil
import zlib
from pathlib import Path

import pandas as pd
import pytest

from generation_io import portable_artifacts
from generation_io.input_manifest import iter_portable_raw_records, load_input_manifest
from generation_io.kernel import build_candidate_features
from generation_io.region import load_boundary
from generation_io.source import (
    SourceError,
    normalize_selected_record,
    portable_normalize_selected_record,
)
from generation_io.portable_artifacts import (
    PortableArtifactError,
    generate_portable_artifact,
    resume_portable_artifact,
    verify_portable_manifest,
)


FIXTURE = Path(__file__).parent / "fixtures" / "synthetic"


def copy_fixture(tmp_path: Path) -> Path:
    data_dir = tmp_path / "data"
    shutil.copytree(FIXTURE, data_dir)
    return data_dir


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def tree_hashes(directory: Path) -> dict[str, str]:
    return {
        path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def set_points_close_together(data_dir: Path) -> None:
    manifest_path = data_dir / "input-manifest.json"
    manifest = read_json(manifest_path)
    source_path = data_dir / manifest["source"]["path"]
    with source_path.open("r", encoding="cp932", newline="") as stream:
        rows = list(csv.reader(stream))
    location_index = rows[0].index("規制場所の経度緯度")
    rows[1][location_index] = "1.0 1.0"
    rows[2][location_index] = "1.0001 1.0"
    from io import StringIO

    buffer = StringIO(newline="")
    csv.writer(buffer, lineterminator="\r\n").writerows(rows)
    contents = buffer.getvalue().encode("cp932")
    source_path.write_bytes(contents)
    manifest["source"]["bytes"] = len(contents)
    manifest["source"]["sha256"] = hashlib.sha256(contents).hexdigest()
    manifest["source"]["crc32"] = f"{zlib.crc32(contents) & 0xffffffff:08x}"
    write_json(manifest_path, manifest)


def set_all_rows_to_non_stop(data_dir: Path, keep_one: bool = False) -> None:
    manifest_path = data_dir / "input-manifest.json"
    manifest = read_json(manifest_path)
    source_path = data_dir / manifest["source"]["path"]
    with source_path.open("r", encoding="cp932", newline="") as stream:
        rows = list(csv.reader(stream))
    regulation_index = rows[0].index("共通規制種別コード")
    for index, row in enumerate(rows[1:]):
        if not (keep_one and index == 0):
            row[regulation_index] = "85"
    from io import StringIO

    buffer = StringIO(newline="")
    csv.writer(buffer, lineterminator="\r\n").writerows(rows)
    contents = buffer.getvalue().encode("cp932")
    source_path.write_bytes(contents)
    manifest["source"]["bytes"] = len(contents)
    manifest["source"]["sha256"] = hashlib.sha256(contents).hexdigest()
    manifest["source"]["crc32"] = f"{zlib.crc32(contents) & 0xffffffff:08x}"
    write_json(manifest_path, manifest)


def test_portable_normalizer_preserves_legacy_result_and_uses_portable_identity():
    data = load_input_manifest(FIXTURE)
    item = next(iter_portable_raw_records(data.source_path, data))
    selected_hash = hashlib.sha256(
        json.dumps(item["selected"], ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    boundary = load_boundary(data.boundary_path)

    portable = portable_normalize_selected_record(
        item["selected"],
        source_row=item["source_row"],
        source_month="202701",
        region_id="synthetic:rectangle",
        boundary=boundary,
        selected_hash=selected_hash,
    )
    legacy = normalize_selected_record(
        {
            "version": "k_2.1", "prefecture": "23", "police": "101",
            "regulation": "63", "update": "", "key": "A" * 32,
            "branch": "", "location": "1 1", "entrance": "",
        },
        source_row=5,
        source_month="202602",
        region_id="synthetic:rectangle",
        boundary=boundary,
        selected_hash="a" * 64,
    )

    assert portable["classification"] == "normalized"
    assert portable["record"]["source_month"] == "202701"
    assert portable["record"]["entity_id"] == f"synthetic:rectangle/202701/{item['selected']['key']}"
    assert portable["record"]["source_active_status"] == "unknown"
    assert portable["record"]["update_semantics"] == "unknown"
    assert portable["record"]["production_approved"] is False
    assert legacy == {
        "classification": "normalized",
        "record": {
            "entity_id": "jp:aichi:nagoya/202602/" + "A" * 32,
            "region_id": "synthetic:rectangle",
            "source_row": 5,
            "selected_field_sha256": "a" * 64,
            "source_month": "202602",
            "prefecture": "23",
            "raw_uid": "A" * 32,
            "record_id": "A" * 32,
            "jartic_record_id": "A" * 32,
            "ユニークキー": "A" * 32,
            "police": "101",
            "regulation": "63",
            "branch": "",
            "raw_update": "",
            "update_semantics": "unknown",
            "source_active_status": "unknown",
            "source_location_token": "1 1",
            "source_entrance_tokens": [],
            "entrance_points": [],
            "lon": 1.0,
            "lat": 1.0,
            "normalization_status": "valid_candidate_only",
            "included_boundary": False,
            "production_approved": False,
        },
    }


@pytest.mark.parametrize("month", ["202700", "202713", "000001", "2027-01"])
def test_portable_normalizer_rejects_invalid_months(month: str):
    with pytest.raises(SourceError, match="invalid_source_month"):
        portable_normalize_selected_record(
            {}, source_row=2, source_month=month, region_id="synthetic:rectangle",
            boundary=None, selected_hash="0" * 64,
        )


def test_generate_verify_and_resume_are_portable_and_preserve_inputs(tmp_path: Path):
    data_dir = copy_fixture(tmp_path)
    set_points_close_together(data_dir)
    before_inputs = tree_hashes(data_dir)
    output = tmp_path / "generated"

    manifest_path = generate_portable_artifact(
        data_dir, region_id="synthetic:rectangle", output=output
    )
    manifest = verify_portable_manifest(manifest_path)
    normalized = [json.loads(line) for line in (output / "normalized.jsonl").read_text().splitlines()]
    candidates = [json.loads(line) for line in (output / "candidates.jsonl").read_text().splitlines()]
    points = pd.DataFrame(
        [{
            "lon": row["lon"], "lat": row["lat"],
            "jartic_record_id": row["jartic_record_id"], "ユニークキー": row["ユニークキー"],
        } for row in normalized]
    )
    direct = build_candidate_features(points.copy(deep=True), max_candidates=6, candidate_radius=80.0)

    assert set(path.name for path in output.iterdir()) == {
        "normalized.jsonl", "rejected.jsonl", "candidates.jsonl",
        "source-manifest.json", "generation-manifest.json",
    }
    assert manifest["schema_version"] == 3
    assert manifest["region_id"] == "synthetic:rectangle"
    assert manifest["counts"]["rows_total"] == 2
    assert manifest["counts"]["normalized"] == 2
    assert manifest["counts"]["candidate_rows"] == 2
    assert manifest["counts"]["rows_total"] == sum(
        manifest["counts"][name]
        for name in ("excluded_non_stop", "outside_region", "rejected", "normalized")
    )
    assert [row["entity_id"] for row in normalized] == [
        f"synthetic:rectangle/202701/{row['raw_uid']}" for row in normalized
    ]
    assert len(candidates) == len(direct) == 2
    for index, kernel_row in enumerate(direct.to_dict(orient="records")):
        for key, expected_value in kernel_row.items():
            actual_value = candidates[index][key]
            if isinstance(expected_value, float):
                assert float(actual_value).hex() == expected_value.hex()
            else:
                assert actual_value == expected_value
    assert all(row["bearing_semantics"] == "candidate_point_neighbor_only" for row in candidates)
    assert all(row["production_approved"] is False for row in candidates)
    assert manifest["production_approved"] is False
    source_manifest = read_json(output / "source-manifest.json")
    assert source_manifest["synthetic"] is True
    assert source_manifest["source"]["path"] == "source.csv"
    assert Path(source_manifest["source"]["path"]).is_relative_to(Path("."))
    assert tree_hashes(data_dir) == before_inputs

    old_output_hashes = tree_hashes(output)
    old_manifest_hash = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    resumed_output = tmp_path / "resumed"
    unavailable_data_dir = tmp_path / "data-unavailable"
    data_dir.rename(unavailable_data_dir)
    try:
        resumed_path = resume_portable_artifact(manifest_path, resumed_output)
        resumed = verify_portable_manifest(resumed_path)
    finally:
        unavailable_data_dir.rename(data_dir)
    assert resumed["resume"]["parent_generation_manifest_sha256"] == old_manifest_hash
    assert resumed["resume"]["raw_scan_resumed"] is False
    assert resumed["resume"]["region_clip_recomputed"] is False
    assert resumed["counts"] == manifest["counts"]
    assert tree_hashes(output) == old_output_hashes

    moved = tmp_path / "moved-resume"
    shutil.move(resumed_output, moved)
    assert verify_portable_manifest(moved / "generation-manifest.json")["resume"] == resumed["resume"]
    assert tree_hashes(data_dir) == before_inputs


@pytest.mark.parametrize("keep_one", [False, True])
def test_zero_or_one_normalized_point_succeeds_with_empty_candidate_jsonl(
    tmp_path: Path, keep_one: bool
):
    data_dir = copy_fixture(tmp_path)
    set_all_rows_to_non_stop(data_dir, keep_one=keep_one)
    output = tmp_path / "output"

    manifest_path = generate_portable_artifact(
        data_dir, region_id="synthetic:rectangle", output=output
    )
    manifest = verify_portable_manifest(manifest_path)

    assert manifest["counts"]["normalized"] == int(keep_one)
    assert manifest["counts"]["candidate_rows"] == 0
    assert (output / "candidates.jsonl").read_bytes() == b""


def test_region_mismatch_missing_input_boundary_hash_and_fresh_output_fail_closed(tmp_path: Path):
    data_dir = copy_fixture(tmp_path)
    with pytest.raises(PortableArtifactError, match="region_id_mismatch"):
        generate_portable_artifact(data_dir, region_id="other:region", output=tmp_path / "wrong-region")
    with pytest.raises(PortableArtifactError, match="input_output_overlap"):
        generate_portable_artifact(
            data_dir, region_id="synthetic:rectangle", output=data_dir / "nested-output"
        )
    with pytest.raises(PortableArtifactError, match="output_exists"):
        generate_portable_artifact(data_dir, region_id="synthetic:rectangle", output=data_dir)

    empty_output = tmp_path / "empty-existing"
    empty_output.mkdir()
    with pytest.raises(PortableArtifactError, match="output_exists"):
        generate_portable_artifact(data_dir, region_id="synthetic:rectangle", output=empty_output)

    with pytest.raises(PortableArtifactError, match="INPUT_MANIFEST_NOT_FOUND"):
        generate_portable_artifact(data_dir, input_manifest="missing.json", region_id="synthetic:rectangle", output=tmp_path / "missing")

    changed_boundary = data_dir / "boundary.geojson"
    changed_boundary.write_bytes(changed_boundary.read_bytes() + b" ")
    with pytest.raises(PortableArtifactError, match="BOUNDARY_SIZE_MISMATCH|BOUNDARY_HASH_MISMATCH"):
        generate_portable_artifact(data_dir, region_id="synthetic:rectangle", output=tmp_path / "bad-boundary")


def test_manifest_crs_and_source_hash_mismatch_fail_closed(tmp_path: Path):
    data_dir = copy_fixture(tmp_path)
    input_manifest = read_json(data_dir / "input-manifest.json")
    input_manifest["boundary"]["crs"] = "EPSG:3857"
    write_json(data_dir / "input-manifest.json", input_manifest)
    with pytest.raises(PortableArtifactError, match="BOUNDARY_CRS_UNSUPPORTED"):
        generate_portable_artifact(data_dir, region_id="synthetic:rectangle", output=tmp_path / "bad-crs")

    data_dir = copy_fixture(tmp_path / "second")
    input_manifest = read_json(data_dir / "input-manifest.json")
    input_manifest["source"]["sha256"] = "0" * 64
    write_json(data_dir / "input-manifest.json", input_manifest)
    with pytest.raises(PortableArtifactError, match="SOURCE_HASH_MISMATCH"):
        generate_portable_artifact(data_dir, region_id="synthetic:rectangle", output=tmp_path / "bad-source")


def test_output_symlinks_and_path_escape_are_rejected(tmp_path: Path):
    data_dir = copy_fixture(tmp_path)
    target = tmp_path / "target"
    target.mkdir()
    output_link = tmp_path / "linked-output"
    output_link.symlink_to(target, target_is_directory=True)
    with pytest.raises(PortableArtifactError, match="output_exists|output_path_symlink"):
        generate_portable_artifact(data_dir, region_id="synthetic:rectangle", output=output_link)

    nested_parent = tmp_path / "nested" / "destination"
    nested_parent.mkdir(parents=True)
    disguised_parent = tmp_path / "disguised-parent"
    disguised_parent.symlink_to(nested_parent, target_is_directory=True)
    with pytest.raises(PortableArtifactError, match="path_invalid|output_path_symlink"):
        generate_portable_artifact(
            data_dir, region_id="synthetic:rectangle", output=disguised_parent / ".." / "escaped"
        )

    output = tmp_path / "valid"
    manifest_path = generate_portable_artifact(data_dir, region_id="synthetic:rectangle", output=output)
    manifest = read_json(manifest_path)
    manifest["files"]["normalized"]["path"] = "../outside.jsonl"
    write_json(manifest_path, manifest)
    with pytest.raises(PortableArtifactError, match="invalid_artifact_path"):
        verify_portable_manifest(manifest_path)


def test_verifier_detects_tampered_artifact_malformed_counts_and_approval(tmp_path: Path):
    data_dir = copy_fixture(tmp_path)
    output = tmp_path / "output"
    manifest_path = generate_portable_artifact(data_dir, region_id="synthetic:rectangle", output=output)
    (output / "normalized.jsonl").write_bytes((output / "normalized.jsonl").read_bytes() + b" ")
    with pytest.raises(PortableArtifactError, match="artifact_hash_mismatch"):
        verify_portable_manifest(manifest_path)

    manifest_path = generate_portable_artifact(copy_fixture(tmp_path / "counts"), region_id="synthetic:rectangle", output=tmp_path / "counts-output")
    manifest = read_json(manifest_path)
    manifest["counts"]["rows_total"] = 999
    write_json(manifest_path, manifest)
    with pytest.raises(PortableArtifactError, match="row_count_reconciliation_failed"):
        verify_portable_manifest(manifest_path)

    manifest_path = generate_portable_artifact(copy_fixture(tmp_path / "approval"), region_id="synthetic:rectangle", output=tmp_path / "approval-output")
    manifest = read_json(manifest_path)
    manifest["production_approved"] = True
    write_json(manifest_path, manifest)
    with pytest.raises(PortableArtifactError, match="production_approval_must_remain_false"):
        verify_portable_manifest(manifest_path)


def test_verify_manifest_path_must_be_regular_and_resume_requires_fresh_output(tmp_path: Path):
    data_dir = copy_fixture(tmp_path)
    output = tmp_path / "output"
    manifest_path = generate_portable_artifact(data_dir, region_id="synthetic:rectangle", output=output)
    alias = tmp_path / "manifest-link.json"
    alias.symlink_to(manifest_path)
    with pytest.raises(PortableArtifactError, match="manifest_not_regular"):
        verify_portable_manifest(alias)
    with pytest.raises(PortableArtifactError, match="output_exists"):
        resume_portable_artifact(manifest_path, output)


@pytest.mark.parametrize(
    ("limit_name", "error_code"),
    [("MAX_OUTPUT_BYTES", "output_budget_exceeded"), ("MAX_REPORT_BYTES", "report_budget_exceeded")],
)
def test_output_and_report_budgets_fail_without_leaving_owned_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limit_name: str, error_code: str
):
    data_dir = copy_fixture(tmp_path)
    output = tmp_path / "budget-output"
    monkeypatch.setattr(portable_artifacts, limit_name, 1)

    with pytest.raises(PortableArtifactError, match=error_code):
        generate_portable_artifact(data_dir, region_id="synthetic:rectangle", output=output)

    assert not output.exists()


def test_failure_cleanup_preserves_non_owned_file_and_never_leaves_complete_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    data_dir = copy_fixture(tmp_path)
    set_points_close_together(data_dir)
    output = tmp_path / "interrupted"

    def create_foreign_file_then_fail(points, normalized_sha256):
        (output / "foreign.txt").write_text("not owned by generator", encoding="utf-8")
        raise PortableArtifactError("injected_kernel_failure")

    monkeypatch.setattr(portable_artifacts, "_candidate_records", create_foreign_file_then_fail)
    with pytest.raises(PortableArtifactError, match="injected_kernel_failure"):
        generate_portable_artifact(data_dir, region_id="synthetic:rectangle", output=output)

    assert (output / "foreign.txt").read_text(encoding="utf-8") == "not owned by generator"
    assert not (output / "generation-manifest.json").exists()
    assert sorted(path.name for path in output.iterdir()) == ["foreign.txt"]


def test_complete_manifest_is_published_only_after_draft_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    data_dir = copy_fixture(tmp_path)
    output = tmp_path / "publish-last"
    real_verify = portable_artifacts.verify_portable_manifest
    observations = []

    def observe_draft(path):
        path = Path(path)
        if path.name == ".generation-manifest.draft.json":
            observations.append(not (output / "generation-manifest.json").exists())
        return real_verify(path)

    monkeypatch.setattr(portable_artifacts, "verify_portable_manifest", observe_draft)
    generate_portable_artifact(data_dir, region_id="synthetic:rectangle", output=output)

    assert observations == [True]


def test_verifier_rejects_symlinked_artifact_file(tmp_path: Path):
    data_dir = copy_fixture(tmp_path)
    output = tmp_path / "output"
    manifest_path = generate_portable_artifact(data_dir, region_id="synthetic:rectangle", output=output)
    normalized = output / "normalized.jsonl"
    outside = tmp_path / "outside.jsonl"
    outside.write_bytes(normalized.read_bytes())
    normalized.unlink()
    normalized.symlink_to(outside)

    with pytest.raises(PortableArtifactError, match="artifact_not_regular"):
        verify_portable_manifest(manifest_path)


def test_api_rejects_missing_csv_and_invalid_boundary_geometry(tmp_path: Path):
    missing_source = copy_fixture(tmp_path)
    (missing_source / "source.csv").unlink()
    with pytest.raises(PortableArtifactError, match="SOURCE_CSV_NOT_FOUND"):
        generate_portable_artifact(
            missing_source, region_id="synthetic:rectangle", output=tmp_path / "missing-csv"
        )

    invalid_boundary = copy_fixture(tmp_path / "bad-geometry")
    manifest = read_json(invalid_boundary / "input-manifest.json")
    boundary_path = invalid_boundary / manifest["boundary"]["path"]
    boundary_path.write_text(
        json.dumps({"type": "Feature", "properties": {}, "geometry": {"type": "Point", "coordinates": [1, 1]}}),
        encoding="utf-8",
    )
    boundary_bytes = boundary_path.read_bytes()
    manifest["boundary"]["bytes"] = len(boundary_bytes)
    manifest["boundary"]["sha256"] = hashlib.sha256(boundary_bytes).hexdigest()
    write_json(invalid_boundary / "input-manifest.json", manifest)
    with pytest.raises(PortableArtifactError, match="unsupported_boundary_geometry"):
        generate_portable_artifact(
            invalid_boundary, region_id="synthetic:rectangle", output=tmp_path / "bad-geometry-output"
        )
