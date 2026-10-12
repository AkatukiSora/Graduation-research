from pathlib import Path

import pytest

from generation_io.source import SourceError, load_selected_projection


G01 = Path("/opt/data/workspaces/graduation-research-evidence/unified-work/G01/fixtures")
SOURCE = G01 / "selected-fields.jsonl"
CONTRACT = G01 / "fixture-manifest.json"


def test_selected_projection_is_frozen_and_preserves_all_row_provenance():
    source_manifest, rows = load_selected_projection(SOURCE, CONTRACT)

    assert source_manifest["input_kind"] == "selected_projection"
    assert source_manifest["not_original_full_csv_rows"] is True
    assert source_manifest["production_approved"] is False
    assert len(rows) == 64
    assert rows[0]["source_row"] == 163804
    assert rows[0]["selected"]["key"] == "23202602016380300000000015000209"
    assert all(row["selected_utf8_sha256"] for row in rows)


def test_selected_projection_rejects_a_changed_fixture(tmp_path):
    changed = tmp_path / "selected-fields.jsonl"
    changed.write_bytes(SOURCE.read_bytes() + b"\n")

    with pytest.raises(SourceError, match="source_hash_mismatch"):
        load_selected_projection(changed, CONTRACT)
