from __future__ import annotations

import csv
import hashlib
import io
import json
import shutil
import zlib
from decimal import Decimal
from pathlib import Path
from typing import Callable

import pytest

from generation_io.input_manifest import (
    InputError,
    iter_portable_raw_records,
    load_input_manifest,
)
from generation_io.region import load_boundary


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "synthetic"


def file_hashes(directory: Path) -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.iterdir())
        if path.is_file()
    }


def copy_fixture(tmp_path: Path) -> Path:
    destination = tmp_path / "data"
    shutil.copytree(FIXTURE_DIR, destination)
    return destination


def read_manifest(data_dir: Path) -> dict:
    return json.loads((data_dir / "input-manifest.json").read_text(encoding="utf-8"))


def write_manifest(data_dir: Path, manifest: dict) -> None:
    (data_dir / "input-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def refresh_source_integrity(data_dir: Path, manifest: dict, data: bytes) -> None:
    (data_dir / "source.csv").write_bytes(data)
    manifest["source"]["bytes"] = len(data)
    manifest["source"]["sha256"] = hashlib.sha256(data).hexdigest()
    manifest["source"]["crc32"] = f"{zlib.crc32(data) & 0xffffffff:08x}"
    write_manifest(data_dir, manifest)


def rewrite_csv(data_dir: Path, mutate: Callable[[list[list[str]]], object]) -> None:
    csv_path = data_dir / "source.csv"
    with csv_path.open("r", encoding="cp932", newline="") as stream:
        rows = list(csv.reader(stream))
    mutate(rows)
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerows(rows)
    manifest = read_manifest(data_dir)
    refresh_source_integrity(data_dir, manifest, output.getvalue().encode("cp932"))


def test_synthetic_manifest_loads_and_streams_raw_rows_without_mutation():
    before = file_hashes(FIXTURE_DIR)

    loaded = load_input_manifest(FIXTURE_DIR)
    rows = list(iter_portable_raw_records(loaded.source_path, loaded))

    assert loaded.manifest["synthetic"] is True
    assert loaded.manifest["production_approved"] is False
    assert loaded.source_path == (FIXTURE_DIR / "source.csv").resolve()
    assert loaded.boundary_path == (FIXTURE_DIR / "boundary.geojson").resolve()
    assert loaded.manifest["source"]["source_month"] == "202701"
    assert loaded.manifest["provenance"]["rights_status"] == "unknown"
    assert len(rows) == 2
    assert [row["source_row"] for row in rows] == [2, 3]
    assert [row["selected"]["regulation"] for row in rows] == ["63", "63"]
    assert rows[0]["raw"]["規制場所の経度緯度"] == "1.0 1.0"
    assert rows[1]["raw"]["ユニークキー"] == "SYNTHETIC00000000000000000000002"
    assert all(len(row["raw"]) == 170 for row in rows)
    assert all(
        len(row["selected"]["key"]) == 32
        and row["selected"]["key"].isascii()
        and row["selected"]["key"].isalnum()
        for row in rows
    )
    boundary = load_boundary(loaded.boundary_path)
    assert all(
        boundary.covers(*(Decimal(token) for token in row["selected"]["location"].split()))
        == (True, False)
        for row in rows
    )
    assert rows[0]["provenance"] == loaded.manifest["provenance"]
    assert rows[0]["synthetic"] is True
    assert rows[0]["production_approved"] is False
    assert file_hashes(FIXTURE_DIR) == before


def test_manifest_duplicate_json_keys_fail_with_actionable_error(tmp_path: Path):
    (tmp_path / "input-manifest.json").write_text(
        '{"schema_version":1,"schema_version":1}', encoding="utf-8"
    )

    with pytest.raises(InputError) as caught:
        load_input_manifest(tmp_path)

    assert caught.value.code == "INPUT_MANIFEST_INVALID"
    assert caught.value.cause
    assert caught.value.action
    assert "原因" in str(caught.value)
    assert "対処" in str(caught.value)


@pytest.mark.parametrize(
    ("field", "value", "expected_code"),
    [
        ("source_month", "202613", "INVALID_SOURCE_MONTH"),
        ("source_month", "20260x", "INVALID_SOURCE_MONTH"),
        ("format_version", "k_9.9", "UNSUPPORTED_FORMAT_VERSION"),
        ("sha256", "g" * 64, "INVALID_SHA256"),
        ("crc32", "xyz", "INVALID_CRC32"),
        ("header_sha256", "z" * 64, "INVALID_HEADER_SHA256"),
        ("bytes", True, "INPUT_MANIFEST_INVALID"),
        ("bytes", -1, "INPUT_MANIFEST_INVALID"),
        ("path", "../escape.csv", "INPUT_PATH_INVALID"),
        ("path", "/tmp/outside.csv", "INPUT_PATH_INVALID"),
    ],
)
def test_manifest_rejects_invalid_source_metadata_before_file_lookup(
    tmp_path: Path, field: str, value: object, expected_code: str
):
    data_dir = copy_fixture(tmp_path)
    manifest = read_manifest(data_dir)
    manifest["source"][field] = value
    # A missing CSV proves metadata validation happens before referenced-file lookup.
    (data_dir / "source.csv").unlink()
    write_manifest(data_dir, manifest)

    with pytest.raises(InputError) as caught:
        load_input_manifest(data_dir)

    assert caught.value.code == expected_code
    assert caught.value.cause and caught.value.action
    assert "原因" in str(caught.value) and "対処" in str(caught.value)


def test_manifest_rejects_absolute_manifest_name_and_missing_inputs(tmp_path: Path):
    with pytest.raises(InputError) as absolute:
        load_input_manifest(tmp_path, str((tmp_path / "input-manifest.json").resolve()))
    assert absolute.value.code == "INPUT_PATH_INVALID"

    with pytest.raises(InputError) as missing_manifest:
        load_input_manifest(tmp_path)
    assert missing_manifest.value.code == "INPUT_MANIFEST_NOT_FOUND"

    data_dir = copy_fixture(tmp_path)
    manifest = read_manifest(data_dir)
    (data_dir / "source.csv").unlink()
    with pytest.raises(InputError) as missing_source:
        load_input_manifest(data_dir)
    assert missing_source.value.code == "SOURCE_CSV_NOT_FOUND"

    (data_dir / "source.csv").write_bytes(FIXTURE_DIR.joinpath("source.csv").read_bytes())
    (data_dir / "boundary.geojson").unlink()
    with pytest.raises(InputError) as missing_boundary:
        load_input_manifest(data_dir)
    assert missing_boundary.value.code == "BOUNDARY_NOT_FOUND"


def test_manifest_rejects_symlink_input_and_boundary_hash_mismatch(tmp_path: Path):
    data_dir = copy_fixture(tmp_path)
    original = data_dir / "source.csv"
    linked = data_dir / "linked.csv"
    linked.symlink_to(original.name)
    manifest = read_manifest(data_dir)
    manifest["source"]["path"] = linked.name
    write_manifest(data_dir, manifest)

    with pytest.raises(InputError) as symlink_error:
        load_input_manifest(data_dir)
    assert symlink_error.value.code == "INPUT_PATH_SYMLINK"

    linked.unlink()
    manifest = read_manifest(data_dir)
    manifest["source"]["path"] = "source.csv"
    manifest["boundary"]["sha256"] = "0" * 64
    write_manifest(data_dir, manifest)
    with pytest.raises(InputError) as hash_error:
        load_input_manifest(data_dir)
    assert hash_error.value.code == "BOUNDARY_HASH_MISMATCH"


def test_source_hash_and_crc_mismatches_fail_closed(tmp_path: Path):
    data_dir = copy_fixture(tmp_path)
    manifest = read_manifest(data_dir)
    manifest["source"]["sha256"] = "0" * 64
    write_manifest(data_dir, manifest)
    with pytest.raises(InputError) as hash_error:
        load_input_manifest(data_dir)
    assert hash_error.value.code == "SOURCE_HASH_MISMATCH"

    manifest = read_manifest(data_dir)
    manifest["source"]["sha256"] = hashlib.sha256(
        (data_dir / "source.csv").read_bytes()
    ).hexdigest()
    manifest["source"]["crc32"] = "00000000"
    write_manifest(data_dir, manifest)
    with pytest.raises(InputError) as crc_error:
        load_input_manifest(data_dir)
    assert crc_error.value.code == "SOURCE_CRC_MISMATCH"


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("header", "header_mismatch"),
        ("width", "invalid_row_width"),
        ("uid", "invalid_raw_uid"),
        ("duplicate", "duplicate_raw_uid"),
        ("version", "unknown_version"),
        ("encoding", "invalid_encoding"),
    ],
)
def test_portable_parser_rejects_bad_header_rows_and_encoding(
    tmp_path: Path, mutation: str, expected_code: str
):
    data_dir = copy_fixture(tmp_path)
    manifest = read_manifest(data_dir)
    if mutation == "header":
        rewrite_csv(data_dir, lambda rows: rows[0].__setitem__(0, "not the schema header"))
    elif mutation == "width":
        rewrite_csv(data_dir, lambda rows: rows[1].pop())
    elif mutation == "uid":
        rewrite_csv(
            data_dir,
            lambda rows: rows[1].__setitem__(rows[0].index("ユニークキー"), "bad uid"),
        )
    elif mutation == "duplicate":
        rewrite_csv(
            data_dir,
            lambda rows: rows[2].__setitem__(
                rows[0].index("ユニークキー"), rows[1][rows[0].index("ユニークキー")]
            ),
        )
    elif mutation == "version":
        rewrite_csv(data_dir, lambda rows: rows[1].__setitem__(0, "k_9.9"))
    else:
        data = (data_dir / "source.csv").read_bytes() + b"\x81"
        refresh_source_integrity(data_dir, manifest, data)

    loaded = load_input_manifest(data_dir)
    with pytest.raises(InputError) as caught:
        list(iter_portable_raw_records(loaded.source_path, loaded))

    assert caught.value.code == expected_code
    assert caught.value.__cause__ is not None
    assert caught.value.cause and caught.value.action
    assert "原因" in str(caught.value) and "対処" in str(caught.value)


def test_manifest_crs_and_production_approval_are_not_inferred(tmp_path: Path):
    data_dir = copy_fixture(tmp_path)
    manifest = read_manifest(data_dir)
    manifest["boundary"]["crs"] = "EPSG:3857"
    write_manifest(data_dir, manifest)
    with pytest.raises(InputError) as crs_error:
        load_input_manifest(data_dir)
    assert crs_error.value.code == "BOUNDARY_CRS_UNSUPPORTED"

    manifest["boundary"]["crs"] = "EPSG:4326"
    manifest["boundary"]["region_id"] = "   "
    write_manifest(data_dir, manifest)
    with pytest.raises(InputError) as region_error:
        load_input_manifest(data_dir)
    assert region_error.value.code == "BOUNDARY_REGION_ID_INVALID"

    manifest["boundary"]["region_id"] = "synthetic:rectangle"
    manifest["production_approved"] = True
    write_manifest(data_dir, manifest)
    with pytest.raises(InputError) as approval_error:
        load_input_manifest(data_dir)
    assert approval_error.value.code == "PRODUCTION_APPROVAL_FORBIDDEN"


def test_portable_iterator_requires_its_validated_source_path(tmp_path: Path):
    loaded = load_input_manifest(FIXTURE_DIR)

    with pytest.raises(InputError) as caught:
        list(iter_portable_raw_records(FIXTURE_DIR / "boundary.geojson", loaded))

    assert caught.value.code == "SOURCE_PATH_MISMATCH"
