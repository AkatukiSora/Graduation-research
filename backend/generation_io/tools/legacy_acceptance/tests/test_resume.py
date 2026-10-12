import hashlib
import json
from pathlib import Path

import pytest

from generation_io.artifacts import (
    ArtifactError,
    generate_candidate_artifact,
    resume_candidate_artifact,
    verify_generation_manifest,
)


ROOT = Path("/opt/data/workspaces/graduation-research-g02/backend/generation_io")
G01 = Path("/opt/data/workspaces/graduation-research-evidence/unified-work/G01/fixtures")
SOURCE = G01 / "selected-fields.jsonl"
CONTRACT = G01 / "fixture-manifest.json"
BOUNDARY = ROOT / "tests/fixtures/region-a.geojson"


def test_resume_revalidates_all_inputs_and_publishes_fresh_kernel_stage(tmp_path):
    original = tmp_path / "original"
    original_manifest = generate_candidate_artifact(
        source=SOURCE,
        source_contract=CONTRACT,
        input_kind="selected_projection",
        boundary=BOUNDARY,
        region_id="fixture:region-a",
        output=original,
    )
    resumed = tmp_path / "resumed"

    resumed_manifest_path = resume_candidate_artifact(
        manifest=original_manifest, output=resumed
    )
    original_manifest_data = verify_generation_manifest(original_manifest)
    resumed_manifest_data = verify_generation_manifest(resumed_manifest_path)

    assert resumed_manifest_data["resume"]["stage"] == "whole_region_kernel_only"
    assert resumed_manifest_data["resume"]["source_generation_manifest_sha256"] == hashlib.sha256(original_manifest.read_bytes()).hexdigest()
    for name in ("normalized-points.jsonl", "rejected-records.jsonl", "source-manifest.json"):
        assert (resumed / name).read_bytes() == (original / name).read_bytes()
    assert resumed_manifest_data["outputs"]["candidate-features.jsonl"]["sha256"] == original_manifest_data["outputs"]["candidate-features.jsonl"]["sha256"]


def test_resume_rejects_modified_verified_input_before_creating_output(tmp_path):
    original = tmp_path / "original"
    original_manifest = generate_candidate_artifact(
        source=SOURCE,
        source_contract=CONTRACT,
        input_kind="selected_projection",
        boundary=BOUNDARY,
        region_id="fixture:region-a",
        output=original,
    )
    with (original / "normalized-points.jsonl").open("ab") as stream:
        stream.write(b"{}\n")
    fresh = tmp_path / "resume-after-tamper"

    with pytest.raises(ArtifactError, match="output_hash_mismatch"):
        resume_candidate_artifact(manifest=original_manifest, output=fresh)

    assert not fresh.exists()
    assert not (fresh / "generation-manifest.json").exists()


def test_verifier_rejects_tampered_source_manifest_sha256(tmp_path):
    original = tmp_path / "original"
    original_manifest = generate_candidate_artifact(
        source=SOURCE,
        source_contract=CONTRACT,
        input_kind="selected_projection",
        boundary=BOUNDARY,
        region_id="fixture:region-a",
        output=original,
    )
    resumed = tmp_path / "resumed"
    resumed_manifest = resume_candidate_artifact(
        manifest=original_manifest, output=resumed
    )
    document = json.loads(resumed_manifest.read_text(encoding="utf-8"))
    document["resume"]["source_generation_manifest_sha256"] = "0" * 64
    resumed_manifest.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ArtifactError, match="resume_source_manifest_ref_mismatch"):
        verify_generation_manifest(resumed_manifest)
