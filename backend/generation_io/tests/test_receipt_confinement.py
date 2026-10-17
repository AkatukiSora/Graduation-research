from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from generation_io.portable_artifacts import (
    PortableArtifactError,
    generate_portable_artifact,
    resume_portable_artifact,
)


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "synthetic"


def _copy_fixture(root: Path) -> Path:
    data_dir = root / "data"
    shutil.copytree(FIXTURE_DIR, data_dir)
    return data_dir


def _tree_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _valid_receipt_hook(state_root: Path, stage: str) -> tuple[Path, Path, str]:
    invocation_id = f"overlap-{stage}-test"
    attempt_dir = state_root / "attempts" / ("attempt-" + "a" * 32)
    attempt_dir.mkdir(parents=True)
    (attempt_dir / f"{stage}.log").write_bytes(b"synthetic test log\n")
    (state_root / "budget-state.json").write_text(
        json.dumps(
            {
                "in_flight": {
                    "stage": stage,
                    "invocation_id": invocation_id,
                    "owned_evidence_dir": str(attempt_dir.resolve()),
                }
            }
        ),
        encoding="utf-8",
    )
    return (
        attempt_dir / "ownership.json",
        attempt_dir / "published-files.json",
        invocation_id,
    )


def test_generate_rejects_valid_receipt_state_inside_data_tree_without_mutation(tmp_path: Path):
    data_dir = _copy_fixture(tmp_path)
    receipts = _valid_receipt_hook(data_dir / "receipt-state", "generate")
    input_before = _tree_hashes(data_dir)
    output = tmp_path / "generated"

    with pytest.raises(PortableArtifactError, match="invalid_ownership_receipt_path"):
        generate_portable_artifact(
            data_dir,
            region_id="synthetic:rectangle",
            output=output,
            ownership_receipt=receipts[0],
            published_receipt=receipts[1],
            invocation_id=receipts[2],
        )

    assert not output.exists()
    assert not receipts[0].exists()
    assert not receipts[1].exists()
    assert _tree_hashes(data_dir) == input_before


def test_resume_rejects_receipt_state_root_overlapping_parent_artifact_without_mutation(
    tmp_path: Path,
):
    data_dir = _copy_fixture(tmp_path)
    parent_artifact = tmp_path / "parent-artifact"
    parent_manifest = generate_portable_artifact(
        data_dir,
        region_id="synthetic:rectangle",
        output=parent_artifact,
    )
    # The receipt state root encloses the artifact root, so the roots overlap.
    receipts = _valid_receipt_hook(tmp_path, "resume")
    parent_before = _tree_hashes(parent_artifact)
    output = tmp_path.parent / f"{tmp_path.name}-resumed"

    with pytest.raises(PortableArtifactError, match="invalid_ownership_receipt_path"):
        resume_portable_artifact(
            parent_manifest,
            output,
            ownership_receipt=receipts[0],
            published_receipt=receipts[1],
            invocation_id=receipts[2],
        )

    assert not output.exists()
    assert not receipts[0].exists()
    assert not receipts[1].exists()
    assert _tree_hashes(parent_artifact) == parent_before


def test_resume_rejects_receipt_state_inside_parent_artifact_before_verification(
    tmp_path: Path,
):
    data_dir = _copy_fixture(tmp_path)
    parent_artifact = tmp_path / "parent-artifact"
    parent_manifest = generate_portable_artifact(
        data_dir,
        region_id="synthetic:rectangle",
        output=parent_artifact,
    )
    receipts = _valid_receipt_hook(parent_artifact / "receipt-state", "resume")
    parent_before = _tree_hashes(parent_artifact)
    output = tmp_path / "resumed"

    with pytest.raises(PortableArtifactError, match="invalid_ownership_receipt_path"):
        resume_portable_artifact(
            parent_manifest,
            output,
            ownership_receipt=receipts[0],
            published_receipt=receipts[1],
            invocation_id=receipts[2],
        )

    assert not output.exists()
    assert not receipts[0].exists()
    assert not receipts[1].exists()
    assert _tree_hashes(parent_artifact) == parent_before
