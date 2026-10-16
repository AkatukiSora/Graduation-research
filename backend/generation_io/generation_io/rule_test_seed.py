"""Build and verify the temporary Nagoya nearest-neighbor rule-test seed bundle.

The bundle contains JARTIC-derived coordinates and synthetic, unverified
stop-to-neighbor proxy directions. It is not an operational traffic dataset.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zlib
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Iterator

# Set numerical libraries to deterministic single-thread defaults before the
# existing portable kernel is imported. The CLI still requires PYTHONHASHSEED=0.
for _thread_var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ.setdefault(_thread_var, "1")

from . import portable_artifacts
from .kernel import FEATURES as KERNEL_FEATURES
from .portable_artifacts import PortableArtifactError, verify_portable_manifest


DATASET_ID = "nagoya-rule-test-202602-v1"
REGION_ID = "jp:aichi:nkc-2km"
SOURCE_MONTH = "202602"
EXPECTED_GENERATION_COUNTS = {
    "rows_total": 551_863,
    "excluded_non_stop": 383_211,
    "outside_region": 166_713,
    "rejected": 0,
    "normalized": 1_939,
}
SOURCE_MEMBER = "typeD_aichi/愛知県警_202602_k_2.1.csv"
SOURCE_ZIP_SHA256 = "f12388fca9283f0abd9104a3b265f88caac56f43a9add92404d22a34de0e6ea5"
SOURCE_ZIP_BYTES = 17_656_901
SOURCE_CSV_SHA256 = "0d902b472980d40fa7b472998e3424c73ad4730108fac9655ac1b23e82ee573d"
SOURCE_CSV_BYTES = 363_440_778
SOURCE_CSV_CRC32 = "6f7ba6eb"
SOURCE_HEADER_SHA256 = "74334997340bd11605f3cb3edbc8cea93782df73af3cdc2492b9c48cb6825ca1"
EXPECTED_BOUNDARY_SHA256 = "ba9cc6cebf0481618357a6d5c143025f74d836cb206381f1383659fb97c4469a"
TERMS_REFERENCE = "https://www.jartic.or.jp/d/opendata/riyou_kiyaku.pdf"
TERMS_SHA256 = "b1ada2fc58d068ed95b1973932f4882dfb5d49e888f17ded934f777d545cd285"
SOURCE_URL = "https://www.jartic.or.jp/service/opendata/"
PUBLISHER = "公益財団法人日本道路交通情報センター"
RIGHTS_STATUS = "owner-accepted-jartic-rule-test"
METHOD_VERSION = "nagoya-nearest-rule-test-v1"
DIRECTION_METHOD = "nearest_stop_reference"
DIRECTION_REASON = "topology_proxy_unverified"
DIRECTION_SOURCE = "nearest_stop"
DIRECTION_SEMANTICS = "stop_to_neighbor_proxy"
BEARING_SEMANTICS = "candidate_point_neighbor_only"
MAX_POINTS = 10_000
MAX_CANDIDATES = 250_000
MAX_CANDIDATES_PER_POINT = 6
CANDIDATE_RADIUS_M = 80.0
EPSILON_M = 1e-6
MAX_MANIFEST_BYTES = 1 << 20
MAX_JSONL_LINE_BYTES = 64 << 10
MAX_SHARD_BYTES = 4 << 20
MAX_BUNDLE_UNCOMPRESSED = 64 << 20
MAX_BUNDLE_COMPRESSED = 64 << 20
MAX_GENERATED_BYTES = 32 << 20
MAX_REPORT_BYTES = 8 << 20
MAX_BUNDLE_FILES = 64
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
UID_RE = re.compile(r"[A-Za-z0-9]{32}\Z", re.ASCII)

POINT_KEYS = (
    "entity_id", "raw_uid", "lon", "lat", "direction_degrees", "direction_status",
    "imputation_method", "imputation_reason", "direction_source", "direction_evidence",
    "validity_status", "validity_reason", "validity_evidence", "normalization_status",
)
CANDIDATE_KEYS = (
    "source_entity_id", "neighbor_entity_id", "rank", "distance_m", "candidate_bearing",
    "features", "bearing_semantics",
)
KERNEL_COLUMNS = tuple(portable_artifacts._KERNEL_COLUMNS)
CANDIDATE_INPUT_KEYS = frozenset((*KERNEL_COLUMNS, "entity_id", "candidate_entity_id", "source_row",
                                  "candidate_source_row", "source_month", "parent_normalized_sha256",
                                  "bearing_semantics", "production_approved"))
NORMALIZED_REQUIRED = frozenset(("entity_id", "raw_uid", "record_id", "jartic_record_id", "ユニークキー",
                                 "region_id", "source_row", "source_month", "entrance_points", "lon", "lat",
                                 "normalization_status"))
FEATURE_KEYS = frozenset(KERNEL_FEATURES)
DIRECTION_STATUS_KEYS = ("unknown", "inferred", "confirmed")
VALIDITY_STATUS_KEYS = ("unknown", "valid", "invalid")
METHOD_KEYS = ("none", "source_entrance_geometry", "nearest_stop_reference", "osm_fusion_cautious", "field_observation")
DIRECTION_REASON_KEYS = (
    "none", "missing_entrance_coordinates", "unsupported_entrance_geometry", "topology_only_unverified",
    "source_identity_unverified", "legacy_unrecorded", "source_geometry_derived", "model_direction_inference",
    "topology_proxy_unverified", "no_neighbor_within_radius", "zero_length_geometry",
    "ambiguous_nearest_neighbor", "multiple_entrance_directions",
)
VALIDITY_REASON_KEYS = (
    "source_validity_unverified", "active_regulation_evidence", "withdrawn_regulation_evidence",
    "synthetic_fixture", "legacy_unrecorded",
)
UNKNOWN_DIRECTION_REASONS = frozenset((
    "unsupported_entrance_geometry", "multiple_entrance_directions", "no_neighbor_within_radius",
    "zero_length_geometry", "ambiguous_nearest_neighbor",
))
PROVENANCE_KEYS = (
    "dataset", "publisher", "edition", "source_url", "terms_reference", "terms_sha256", "rights_status",
    "attribution", "modification_notice", "identity_scope", "source_identity_note", "data_purpose",
)


class RuleTestError(ValueError):
    """Actionable validation or generation error."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RuleTestError("duplicate_json_key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise RuleTestError(f"nonstandard_json_number:{value}")


def _loads(raw: bytes | str) -> Any:
    try:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        return json.loads(text, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except RuleTestError:
        raise
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise RuleTestError("invalid_json") from exc


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                          allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise RuleTestError("invalid_canonical_json_value") from exc


def _document(value: Any) -> bytes:
    try:
        return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise RuleTestError("invalid_document_json_value") from exc


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha256_path(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                digest.update(chunk)
    except OSError as exc:
        raise RuleTestError("input_read_error") from exc
    return size, digest.hexdigest()


def _reject_symlink_components(path: Path) -> None:
    absolute = path.absolute()
    parts = list(reversed(absolute.parents)) + [absolute]
    for component in parts:
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise RuleTestError("path_inspection_failed") from exc
        if stat.S_ISLNK(info.st_mode):
            raise RuleTestError("symlink_path_rejected")


def _regular_file(path_value: str | os.PathLike[str]) -> Path:
    path = Path(path_value).expanduser().absolute()
    _reject_symlink_components(path)
    try:
        info = path.lstat()
    except OSError as exc:
        raise RuleTestError("input_file_not_found") from exc
    if not stat.S_ISREG(info.st_mode):
        raise RuleTestError("input_not_regular_file")
    return path


def _output_location(path_value: str | os.PathLike[str], dataset_id: str) -> tuple[Path, Path]:
    output = Path(path_value).expanduser().absolute()
    if output.name != dataset_id:
        raise RuleTestError("output_directory_name_must_match_dataset_id")
    _reject_symlink_components(output.parent)
    if not output.parent.is_dir():
        raise RuleTestError("output_parent_not_directory")
    if os.path.lexists(output):
        raise RuleTestError("output_directory_already_exists")
    return output, output.parent.resolve(strict=True)


def _paths_overlap(output: Path, inputs: Iterable[Path]) -> bool:
    for source in inputs:
        resolved = source.resolve(strict=True)
        if output == resolved or output in resolved.parents or resolved in output.parents:
            return True
    return False


def _validate_runtime() -> dict[str, str]:
    if os.environ.get("PYTHONHASHSEED") != "0":
        raise RuleTestError("PYTHONHASHSEED_must_be_0")
    for key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        if os.environ.get(key) != "1":
            raise RuleTestError(f"{key}_must_be_1")
    versions = {
        "python": platform.python_version(),
        "numpy": importlib.metadata.version("numpy"),
        "pandas": importlib.metadata.version("pandas"),
        "scipy": importlib.metadata.version("scipy"),
    }
    expected = {"python": "3.13.5", "numpy": "2.3.5", "pandas": "2.3.3", "scipy": "1.16.3"}
    if versions != expected:
        raise RuleTestError("runtime_versions_do_not_match_locked_contract")
    return versions


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _current_commit() -> str:
    result = subprocess.run(["git", "-C", str(_repo_root()), "rev-parse", "HEAD"],
                            capture_output=True, text=True, check=False)
    value = result.stdout.strip()
    if result.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", value):
        raise RuleTestError("repository_base_commit_unavailable")
    return value


def _validate_notice(path: Path) -> dict[str, str]:
    info = path.lstat()
    if info.st_size > MAX_REPORT_BYTES:
        raise RuleTestError("notice_config_too_large")
    value = _loads(path.read_bytes())
    if not isinstance(value, dict) or set(value) != set(PROVENANCE_KEYS):
        raise RuleTestError("notice_config_schema_mismatch")
    if any(not isinstance(item, str) or not item.strip() for item in value.values()):
        raise RuleTestError("notice_config_has_empty_value")
    for field, item in value.items():
        if any(ord(char) < 0x20 for char in item):
            raise RuleTestError(f"notice_config_control_character:{field}")
    expected = {
        "publisher": PUBLISHER,
        "edition": "2026年02月分",
        "source_url": SOURCE_URL,
        "terms_reference": TERMS_REFERENCE,
        "terms_sha256": TERMS_SHA256,
        "rights_status": RIGHTS_STATUS,
        "identity_scope": "source_month",
        "data_purpose": "rule_test",
    }
    for field, wanted in expected.items():
        if value.get(field) != wanted:
            raise RuleTestError(f"notice_config_value_mismatch:{field}")
    if "2026年02月分" not in value["modification_notice"] or "推定値は原提供者" not in value["modification_notice"]:
        raise RuleTestError("notice_config_modification_notice_incomplete")
    if "取得日時不明" not in value["source_identity_note"] or "取得時の規約不明" not in value["source_identity_note"]:
        raise RuleTestError("notice_config_must_preserve_historical_unknowns")
    return value


def _verify_source_zip(path: Path) -> dict[str, Any]:
    size, digest = _sha256_path(path)
    if size != SOURCE_ZIP_BYTES or digest != SOURCE_ZIP_SHA256:
        raise RuleTestError("source_zip_identity_mismatch")
    try:
        with zipfile.ZipFile(path, "r") as archive:
            matches = [item for item in archive.infolist() if item.filename == SOURCE_MEMBER]
            if len(matches) != 1:
                raise RuleTestError("source_zip_member_missing_or_duplicated")
            member = matches[0]
            if member.file_size != SOURCE_CSV_BYTES or member.CRC != int(SOURCE_CSV_CRC32, 16) or member.flag_bits & 1:
                raise RuleTestError("source_zip_member_contract_mismatch")
            member_hash = hashlib.sha256()
            member_crc = 0
            member_size = 0
            with archive.open(member, "r") as stream:
                while chunk := stream.read(1024 * 1024):
                    member_size += len(chunk)
                    if member_size > SOURCE_CSV_BYTES:
                        raise RuleTestError("source_zip_member_size_limit")
                    member_hash.update(chunk)
                    member_crc = zlib.crc32(chunk, member_crc)
            if member_size != SOURCE_CSV_BYTES or member_hash.hexdigest() != SOURCE_CSV_SHA256:
                raise RuleTestError("source_csv_identity_mismatch")
            if f"{member_crc & 0xffffffff:08x}" != SOURCE_CSV_CRC32:
                raise RuleTestError("source_csv_crc_mismatch")
    except RuleTestError:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError, EOFError) as exc:
        raise RuleTestError("source_zip_invalid") from exc
    return {"zip_bytes": size, "zip_sha256": digest, "csv_bytes": member_size,
            "csv_sha256": member_hash.hexdigest(), "csv_crc32": SOURCE_CSV_CRC32}


def _read_generation(generation_path: Path) -> tuple[dict[str, Any], dict[str, Any], Path, Path, Path]:
    try:
        manifest = verify_portable_manifest(generation_path)
    except PortableArtifactError as exc:
        raise RuleTestError(f"generation_manifest_invalid:{exc.code}") from exc
    if manifest.get("schema_version") != 3 or manifest.get("region_id") != REGION_ID or manifest.get("source_month") != SOURCE_MONTH:
        raise RuleTestError("generation_manifest_scope_mismatch")
    files = manifest.get("files")
    if not isinstance(files, dict) or set(files) != {"normalized", "rejected", "candidates", "source_manifest"}:
        raise RuleTestError("generation_manifest_file_set_mismatch")
    root = generation_path.parent
    paths: dict[str, Path] = {}
    for kind, entry in files.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise RuleTestError("generation_manifest_file_entry_invalid")
        relative = PurePosixPath(entry["path"])
        if relative.is_absolute() or len(relative.parts) != 1 or relative.name in {"", ".", ".."}:
            raise RuleTestError("generation_manifest_path_invalid")
        path = root / relative.name
        _reject_symlink_components(path)
        if not path.is_file():
            raise RuleTestError("generation_manifest_file_missing")
        paths[kind] = path
    source_manifest = _loads(paths["source_manifest"].read_bytes())
    if not isinstance(source_manifest, dict) or source_manifest.get("schema_version") != 1:
        raise RuleTestError("source_manifest_invalid")
    source = source_manifest.get("source")
    boundary = source_manifest.get("boundary")
    input_manifest = source_manifest.get("input_manifest")
    if not isinstance(source, dict) or not isinstance(boundary, dict) or not isinstance(input_manifest, dict):
        raise RuleTestError("source_manifest_refs_missing")
    if (source_manifest.get("source_month") != SOURCE_MONTH or source_manifest.get("format_version") != "k_2.1"
            or source_manifest.get("region_id") != REGION_ID or source_manifest.get("synthetic") is not False):
        raise RuleTestError("source_manifest_scope_mismatch")
    if source.get("bytes") != SOURCE_CSV_BYTES or source.get("sha256") != SOURCE_CSV_SHA256 or source.get("crc32") != SOURCE_CSV_CRC32:
        raise RuleTestError("source_manifest_csv_mismatch")
    if boundary.get("region_id") != REGION_ID or boundary.get("sha256") != EXPECTED_BOUNDARY_SHA256:
        raise RuleTestError("source_manifest_boundary_mismatch")
    if not isinstance(input_manifest.get("sha256"), str) or not SHA256_RE.fullmatch(input_manifest["sha256"]):
        raise RuleTestError("source_manifest_input_digest_invalid")
    counts = manifest.get("counts", {})
    if any(counts.get(name) != value for name, value in EXPECTED_GENERATION_COUNTS.items()):
        raise RuleTestError("generation_counts_differ_from_source_observation")
    if counts.get("normalized") != manifest.get("files", {}).get("normalized", {}).get("rows"):
        raise RuleTestError("generation_normalized_count_mismatch")
    return manifest, source_manifest, paths["normalized"], paths["candidates"], root


def _iter_jsonl(path: Path, *, max_rows: int) -> Iterator[dict[str, Any]]:
    count = 0
    try:
        with path.open("rb") as stream:
            for raw in stream:
                count += 1
                if count > max_rows or len(raw) > MAX_JSONL_LINE_BYTES or not raw.endswith(b"\n"):
                    raise RuleTestError("generation_jsonl_limit_or_ending")
                line = raw[:-1]
                if not line or b"\r" in line:
                    raise RuleTestError("generation_jsonl_empty_or_cr")
                value = _loads(line)
                if not isinstance(value, dict):
                    raise RuleTestError("generation_jsonl_row_not_object")
                yield value
    except RuleTestError:
        raise
    except OSError as exc:
        raise RuleTestError("generation_jsonl_read_error") from exc


def _load_points(path: Path, expected_count: int) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    if expected_count <= 0 or expected_count > MAX_POINTS:
        raise RuleTestError("point_count_outside_supported_limit")
    records: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    seen_uids: set[str] = set()
    for row in _iter_jsonl(path, max_rows=MAX_POINTS):
        if not NORMALIZED_REQUIRED.issubset(row):
            raise RuleTestError("normalized_record_missing_required_field")
        entity_id, uid = row["entity_id"], row["raw_uid"]
        source_row = row["source_row"]
        if (not isinstance(entity_id, str) or not entity_id or not isinstance(uid, str) or not UID_RE.fullmatch(uid)
                or row["record_id"] != uid or row["jartic_record_id"] != uid or row["ユニークキー"] != uid
                or isinstance(source_row, bool) or not isinstance(source_row, int) or source_row < 2
                or row["source_month"] != SOURCE_MONTH or row["region_id"] != REGION_ID
                or row["normalization_status"] != "valid_candidate_only"):
            raise RuleTestError("normalized_identity_or_scope_invalid")
        if entity_id in by_id or uid in seen_uids:
            raise RuleTestError("normalized_identity_duplicate")
        lon, lat = row["lon"], row["lat"]
        if (isinstance(lon, bool) or not isinstance(lon, (int, float)) or not math.isfinite(lon) or not -180 <= lon <= 180
                or isinstance(lat, bool) or not isinstance(lat, (int, float)) or not math.isfinite(lat) or not -90 <= lat <= 90):
            raise RuleTestError("normalized_coordinate_invalid")
        entrances = row["entrance_points"]
        if not isinstance(entrances, list) or any(not isinstance(item, dict) for item in entrances):
            raise RuleTestError("normalized_entrance_shape_invalid")
        for entrance in entrances:
            if any(key not in entrance for key in ("lon", "lat")):
                raise RuleTestError("normalized_entrance_coordinate_missing")
            if (isinstance(entrance["lon"], bool) or not isinstance(entrance["lon"], (int, float))
                    or not math.isfinite(entrance["lon"]) or not -180 <= entrance["lon"] <= 180
                    or isinstance(entrance["lat"], bool) or not isinstance(entrance["lat"], (int, float))
                    or not math.isfinite(entrance["lat"]) or not -90 <= entrance["lat"] <= 90):
                raise RuleTestError("normalized_entrance_coordinate_invalid")
        records.append(row)
        by_id[entity_id] = row
        seen_uids.add(uid)
    if len(records) != expected_count:
        raise RuleTestError("normalized_record_count_mismatch")
    return records, by_id


def _valid_number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _candidate_rows(path: Path, normalized_sha256: str, point_by_id: dict[str, dict[str, Any]]) -> tuple[list[bytes], dict[str, list[tuple[str, float, float]]], int]:
    encoded_rows: list[tuple[str, int, bytes]] = []
    first_two: dict[str, list[tuple[str, float, float]]] = {}
    point_order = {entity_id: index for index, entity_id in enumerate(point_by_id)}
    previous_source_row = 0
    previous_rank = 0
    previous_source = ""
    candidate_count = 0
    for row in _iter_jsonl(path, max_rows=MAX_CANDIDATES):
        if set(row) != CANDIDATE_INPUT_KEYS:
            raise RuleTestError("candidate_input_schema_mismatch")
        source_id, neighbor_id = row["entity_id"], row["candidate_entity_id"]
        rank, source_row, neighbor_source_row = row["candidate_rank"], row["source_row"], row["candidate_source_row"]
        if (not isinstance(source_id, str) or source_id not in point_by_id or not isinstance(neighbor_id, str)
                or neighbor_id not in point_by_id or source_id == neighbor_id or isinstance(rank, bool)
                or not isinstance(rank, int) or not 1 <= rank <= MAX_CANDIDATES_PER_POINT
                or isinstance(source_row, bool) or not isinstance(source_row, int)
                or isinstance(neighbor_source_row, bool) or not isinstance(neighbor_source_row, int)):
            raise RuleTestError("candidate_identity_or_rank_invalid")
        if (source_row != point_by_id[source_id]["source_row"]
                or neighbor_source_row != point_by_id[neighbor_id]["source_row"]
                or row["source_month"] != SOURCE_MONTH or row["bearing_semantics"] != BEARING_SEMANTICS
                or row["production_approved"] is not False or row["parent_normalized_sha256"] != normalized_sha256):
            raise RuleTestError("candidate_provenance_mismatch")
        distance, bearing = row["candidate_distance_m"], row["candidate_bearing"]
        if not _valid_number(distance) or not 0 <= distance <= CANDIDATE_RADIUS_M or not _valid_number(bearing) or not 0 <= bearing < 360:
            raise RuleTestError("candidate_geometry_invalid")
        source_point = point_by_id[source_id]
        source_index, neighbor_index = point_order[source_id], point_order[neighbor_id]
        if (isinstance(row["point_index"], bool) or not isinstance(row["point_index"], int)
                or isinstance(row["neighbor_index"], bool) or not isinstance(row["neighbor_index"], int)
                or row["point_index"] != source_index or row["neighbor_index"] != neighbor_index
                or row["record_id"] != source_point["jartic_record_id"]
                or row["uniqueKey"] != source_point["ユニークキー"]
                or row["lon"] != source_point["lon"] or row["lat"] != source_point["lat"]):
            raise RuleTestError("candidate_kernel_identity_mismatch")
        features: dict[str, int | float] = {}
        for key in FEATURE_KEYS:
            value = row.get(key)
            if not _valid_number(value):
                raise RuleTestError(f"candidate_feature_invalid:{key}")
            features[key] = value
        if set(features) != FEATURE_KEYS:
            raise RuleTestError("candidate_feature_projection_mismatch")
        if (features["candidate_rank"] != rank or features["candidate_distance_m"] != distance
                or features["candidate_bearing"] != bearing):
            raise RuleTestError("candidate_feature_projection_mismatch")
        if source_id != previous_source:
            if source_row < previous_source_row:
                raise RuleTestError("candidate_input_source_order_invalid")
            previous_source, previous_source_row, previous_rank = source_id, source_row, 0
        elif source_row != previous_source_row:
            raise RuleTestError("candidate_source_row_changed")
        if rank != previous_rank + 1:
            raise RuleTestError("candidate_rank_sequence_invalid")
        previous_rank = rank
        first_two.setdefault(source_id, []).append((neighbor_id, float(distance), float(bearing)))
        if len(first_two[source_id]) > 2:
            del first_two[source_id][2:]
        candidate = {
            "source_entity_id": source_id,
            "neighbor_entity_id": neighbor_id,
            "rank": rank,
            "distance_m": distance,
            "candidate_bearing": bearing,
            "features": features,
            "bearing_semantics": BEARING_SEMANTICS,
        }
        line = _canonical(candidate) + b"\n"
        if len(line) > MAX_JSONL_LINE_BYTES:
            raise RuleTestError("candidate_output_line_too_large")
        encoded_rows.append((source_id, rank, line))
        candidate_count += 1
        if candidate_count > MAX_CANDIDATES:
            raise RuleTestError("candidate_count_limit_exceeded")
    encoded_rows.sort(key=lambda value: (value[0], value[1]))
    return [entry[2] for entry in encoded_rows], first_two, candidate_count


def _project_points(points: list[dict[str, Any]]) -> tuple[list[tuple[float, float]], float]:
    import numpy as np

    latitudes = np.asarray([point["lat"] for point in points], dtype=np.float64)
    lat0 = float(np.nanmedian(latitudes))
    scale_x = 111_320.0 * math.cos(math.radians(lat0))
    return [(float(point["lon"]) * scale_x, float(point["lat"]) * 111_320.0) for point in points], lat0


def _nearest_two(index: int, xy: list[tuple[float, float]], ids: list[str]) -> tuple[float, float, str | None]:
    distances = []
    x0, y0 = xy[index]
    for other_index, (x, y) in enumerate(xy):
        if other_index == index:
            continue
        distances.append((math.hypot(x - x0, y - y0), ids[other_index]))
    distances.sort(key=lambda item: (item[0], item[1]))
    if not distances:
        return math.inf, math.inf, None
    return distances[0][0], distances[1][0] if len(distances) > 1 else math.inf, distances[0][1]


def _direction_for_point(point: dict[str, Any], nearest: tuple[str, float, float] | None,
                         second: tuple[str, float, float] | None, source_csv_sha256: str,
                         nearest_actual: tuple[float, float, str | None] | None = None) -> dict[str, Any]:
    entrances = point.get("entrance_points", [])
    if len(entrances) > 1:
        reason = "multiple_entrance_directions"
    elif len(entrances) == 1:
        reason = "unsupported_entrance_geometry"
    elif nearest is None:
        reason = "no_neighbor_within_radius"
    elif nearest[1] <= EPSILON_M:
        reason = "zero_length_geometry"
    elif second is not None and abs(second[1] - nearest[1]) <= EPSILON_M:
        reason = "ambiguous_nearest_neighbor"
    else:
        if nearest_actual is not None:
            actual_distance, second_distance, actual_id = nearest_actual
            if (actual_id != nearest[0] or abs(actual_distance - nearest[1]) > EPSILON_M
                    or actual_distance <= EPSILON_M or actual_distance > CANDIDATE_RADIUS_M
                    or second_distance - actual_distance <= EPSILON_M):
                raise RuleTestError("nearest_candidate_does_not_match_all_points")
        evidence = {
            "schema_version": 2,
            "kind": "nearest_stop_reference",
            "source_csv_sha256": source_csv_sha256,
            "method_version": "nearest-stop-proxy-v1",
            "direction_semantics": DIRECTION_SEMANTICS,
            "neighbor_entity_id": nearest[0],
            "candidate_rank": 1,
        }
        return {
            "direction_degrees": nearest[2], "direction_status": "inferred",
            "imputation_method": DIRECTION_METHOD, "imputation_reason": DIRECTION_REASON,
            "direction_source": DIRECTION_SOURCE, "direction_evidence": evidence,
        }
    if reason not in UNKNOWN_DIRECTION_REASONS:
        raise RuleTestError("unknown_direction_reason_invalid")
    if reason == "no_neighbor_within_radius" and nearest_actual is not None and nearest_actual[0] <= CANDIDATE_RADIUS_M:
        raise RuleTestError("missing_near_candidate")
    if reason == "zero_length_geometry" and nearest_actual is not None and nearest_actual[0] > EPSILON_M:
        raise RuleTestError("zero_length_reason_mismatch")
    if reason == "ambiguous_nearest_neighbor" and nearest_actual is not None and nearest_actual[1] - nearest_actual[0] > EPSILON_M:
        raise RuleTestError("ambiguous_reason_mismatch")
    return {
        "direction_degrees": None, "direction_status": "unknown", "imputation_method": "none",
        "imputation_reason": reason, "direction_source": "unknown", "direction_evidence": {},
    }


def _build_points(normalized: list[dict[str, Any]], point_by_id: dict[str, dict[str, Any]],
                  first_two: dict[str, list[tuple[str, float, float]]], source_csv_sha256: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    xy, _ = _project_points(normalized)
    ordered_ids = [point["entity_id"] for point in normalized]
    points: list[dict[str, Any]] = []
    entrance_counts: dict[str, int] = {}
    for index, raw in enumerate(normalized):
        pair = first_two.get(raw["entity_id"], [])
        nearest = pair[0] if pair else None
        second = pair[1] if len(pair) > 1 else None
        nearest_actual = _nearest_two(index, xy, ordered_ids)
        direction = _direction_for_point(raw, nearest, second, source_csv_sha256, nearest_actual)
        point = {
            "entity_id": raw["entity_id"], "raw_uid": raw["raw_uid"], "lon": raw["lon"], "lat": raw["lat"],
            **direction, "validity_status": "unknown", "validity_reason": "source_validity_unverified",
            "validity_evidence": {}, "normalization_status": "valid_candidate_only",
        }
        if tuple(point) != POINT_KEYS:
            raise RuleTestError("point_output_schema_mismatch")
        points.append(point)
        count_key = str(len(raw["entrance_points"]))
        entrance_counts[count_key] = entrance_counts.get(count_key, 0) + 1
    points.sort(key=lambda item: item["entity_id"])
    direction_counts = {key: 0 for key in DIRECTION_STATUS_KEYS}
    reason_counts = {key: 0 for key in DIRECTION_REASON_KEYS}
    for point in points:
        direction_counts[point["direction_status"]] += 1
        reason_counts[point["imputation_reason"]] += 1
    if direction_counts["inferred"] == 0:
        raise RuleTestError("inferred_count_zero_requires_design_review")
    if direction_counts["confirmed"] != 0:
        raise RuleTestError("confirmed_direction_forbidden")
    return points, {"direction_status_counts": direction_counts, "imputation_reason_counts": reason_counts,
                    "entrance_count_counts": dict(sorted(entrance_counts.items(), key=lambda item: int(item[0])))}


def _counts(points: list[dict[str, Any]], candidates: int, source_counts: dict[str, Any], state_counts: dict[str, Any]) -> dict[str, Any]:
    validity_reasons = {key: 0 for key in VALIDITY_REASON_KEYS}
    validity_reasons["source_validity_unverified"] = len(points)
    methods = {key: 0 for key in METHOD_KEYS}
    methods["nearest_stop_reference"] = state_counts["direction_status_counts"]["inferred"]
    methods["none"] = state_counts["direction_status_counts"]["unknown"]
    validity_statuses = {key: 0 for key in VALIDITY_STATUS_KEYS}
    validity_statuses["unknown"] = len(points)
    return {
        "points": len(points), "candidates": candidates,
        "rejected": source_counts["rejected"], "outside_region": source_counts["outside_region"],
        "rows_total": source_counts["rows_total"], "excluded_non_stop": source_counts["excluded_non_stop"],
        "direction_status_counts": state_counts["direction_status_counts"],
        "validity_status_counts": validity_statuses,
        "imputation_method_counts": methods,
        "imputation_reason_counts": state_counts["imputation_reason_counts"],
        "validity_reason_counts": validity_reasons,
    }


def _gzip_bytes(raw: bytes) -> bytes:
    import io

    output = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", compresslevel=9, fileobj=output, mtime=0) as stream:
        stream.write(raw)
    return output.getvalue()


def _write_shards(root: Path, kind: str, lines: Iterable[bytes]) -> tuple[list[dict[str, Any]], int, int]:
    descriptors: list[dict[str, Any]] = []
    rows: list[bytes] = []
    raw_size = 0
    total_raw = 0
    total_compressed = 0

    def publish() -> None:
        nonlocal rows, raw_size, total_raw, total_compressed
        if not rows:
            return
        index = len(descriptors) + 1
        basename = f"{kind}-{index:05d}.jsonl.gz"
        raw = b"".join(rows)
        compressed = _gzip_bytes(raw)
        if len(raw) > MAX_SHARD_BYTES:
            raise RuleTestError("shard_expanded_limit_exceeded")
        if total_compressed + len(compressed) > MAX_GENERATED_BYTES:
            raise RuleTestError("generated_artifact_budget_exceeded")
        path = root / basename
        _write_exclusive(path, compressed)
        descriptors.append({
            "kind": kind, "basename": basename, "rows": len(rows),
            "compressed_bytes": len(compressed), "compressed_sha256": _sha256_bytes(compressed),
            "uncompressed_bytes": len(raw), "uncompressed_sha256": _sha256_bytes(raw),
        })
        total_raw += len(raw)
        total_compressed += len(compressed)
        rows, raw_size = [], 0

    for line in lines:
        if not line.endswith(b"\n") or not line[:-1] or len(line) > MAX_JSONL_LINE_BYTES:
            raise RuleTestError("output_jsonl_line_invalid")
        if len(line) > MAX_SHARD_BYTES:
            raise RuleTestError("output_line_exceeds_shard_limit")
        if rows and raw_size + len(line) > MAX_SHARD_BYTES:
            publish()
        rows.append(line)
        raw_size += len(line)
    publish()
    if len(descriptors) > MAX_BUNDLE_FILES:
        raise RuleTestError("bundle_file_count_limit_exceeded")
    return descriptors, total_raw, total_compressed


def _write_exclusive(path: Path, data: bytes) -> None:
    try:
        with path.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise RuleTestError("output_file_write_failed") from exc


def _format_report(report: dict[str, Any]) -> str:
    direction = report["direction_status_counts"]
    reasons = report["imputation_reason_counts"]
    lines = [
        "# Generation report",
        "",
        f"- Dataset: `{report['dataset_id']}`",
        f"- Source month: `{report['source_month']}`",
        f"- Region: `{report['region_id']}`",
        f"- Points: {report['points']}",
        f"- Candidates: {report['candidates']}",
        f"- Direction coverage: {direction['inferred']}/{report['points']} inferred ({report['inferred_ratio']:.8f})",
        f"- Direction states: unknown={direction['unknown']}, inferred={direction['inferred']}, confirmed={direction['confirmed']}",
        f"- Validity states: unknown={report['validity_status_counts']['unknown']}, valid={report['validity_status_counts']['valid']}, invalid={report['validity_status_counts']['invalid']}",
        "- Imputation reasons:",
    ]
    for reason in DIRECTION_REASON_KEYS:
        if reasons[reason]:
            lines.append(f"  - `{reason}`: {reasons[reason]}")
    lines.extend((
        "", "The direction values are synthetic nearest-neighbor proxies (`stop_to_neighbor_proxy`), not verified vehicle approach directions.",
        "All validity states remain unknown. This report does not establish ground-truth accuracy or notification safety.", "",
    ))
    return "\n".join(lines)


def _make_manifest(source_manifest: dict[str, Any], notice: dict[str, str], points: list[dict[str, Any]],
                   candidates: int, counts: dict[str, Any], files: list[dict[str, Any]],
                   runtime_versions: dict[str, str], code_sha: str, lock_sha: str, base_commit: str) -> dict[str, Any]:
    source = source_manifest["source"]
    boundary = source_manifest["boundary"]
    input_manifest = source_manifest["input_manifest"]
    manifest: dict[str, Any] = {
        "schema_version": 2,
        "dataset_id": DATASET_ID,
        "source_month": SOURCE_MONTH,
        "region_id": REGION_ID,
        "synthetic": True,
        "counts": counts,
        "geometry": {
            "region_id": REGION_ID,
            "center_lat": "35.1202166",
            "center_lon": "136.9121477",
            "center_source": "名古屋工学院専門学校1号館公式アクセス地図のplace座標",
            "radius_m": 2000,
            "sphere_radius_m": "6371008.8",
            "vertices": 360,
            "coordinate_decimal_places": 9,
            "algorithm": "spherical-destination-v1",
            "boundary_inclusion": "covers",
            "boundary_sha256": boundary["sha256"],
        },
        "inputs": {
            "source_zip_sha256": SOURCE_ZIP_SHA256,
            "source_csv_sha256": source["sha256"],
            "source_csv_bytes": source["bytes"],
            "source_header_sha256": SOURCE_HEADER_SHA256,
            "boundary_sha256": boundary["sha256"],
            "input_manifest_sha256": input_manifest["sha256"],
            "enrichment_sha256": None,
        },
        "generator": {
            "code_ref": Path(__file__).resolve().relative_to(_repo_root()).as_posix(),
            "code_sha256": code_sha,
            "method_implementation_sha256": code_sha,
            "method_version": METHOD_VERSION,
            "source_selection_order": ["nearest_stop_reference", "unknown"],
            "commit": base_commit,
            "uv_lock_sha256": lock_sha,
            "python_version": runtime_versions["python"],
            "dependency_versions": {
                "numpy": runtime_versions["numpy"], "pandas": runtime_versions["pandas"],
                "scipy": runtime_versions["scipy"],
            },
            "zlib_version": zlib.ZLIB_VERSION,
            "kernel": portable_artifacts.KERNEL_VERSION,
            "max_candidates": MAX_CANDIDATES_PER_POINT,
            "candidate_radius_m": int(CANDIDATE_RADIUS_M),
            "thread_policy": {
                "PYTHONHASHSEED": "0", "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1",
                "MKL_NUM_THREADS": "1", "NUMEXPR_NUM_THREADS": "1",
            },
        },
        "provenance": {key: notice[key] for key in PROVENANCE_KEYS},
        "files": files,
        "content_sha256": "",
    }
    manifest["content_sha256"] = _sha256_bytes(_canonical({key: value for key, value in manifest.items() if key != "content_sha256"}))
    return manifest


def build_bundle(generation_manifest: str | os.PathLike[str], source_zip: str | os.PathLike[str],
                 dataset_id: str, notice_config: str | os.PathLike[str], output_dir: str | os.PathLike[str]) -> dict[str, Any]:
    if dataset_id != DATASET_ID:
        raise RuleTestError("dataset_id_mismatch")
    runtime_versions = _validate_runtime()
    generation_path = _regular_file(generation_manifest)
    zip_path = _regular_file(source_zip)
    notice_path = _regular_file(notice_config)
    output, output_parent = _output_location(output_dir, dataset_id)
    if _paths_overlap(output, (generation_path, generation_path.parent, zip_path, notice_path)):
        raise RuleTestError("output_overlaps_input")
    notice = _validate_notice(notice_path)
    zip_receipt = _verify_source_zip(zip_path)
    generation, source_manifest, normalized_path, candidates_path, generation_root = _read_generation(generation_path)
    if source_manifest["source"]["sha256"] != zip_receipt["csv_sha256"]:
        raise RuleTestError("source_zip_generation_input_mismatch")
    source_counts = generation.get("counts", {})
    expected_source_counts = ("rows_total", "excluded_non_stop", "outside_region", "rejected", "normalized")
    if any(isinstance(source_counts.get(key), bool) or not isinstance(source_counts.get(key), int) or source_counts[key] < 0 for key in expected_source_counts):
        raise RuleTestError("generation_counts_invalid")
    if source_counts["normalized"] <= 0 or source_counts["rows_total"] != sum(source_counts[key] for key in ("excluded_non_stop", "outside_region", "rejected", "normalized")):
        raise RuleTestError("generation_counts_do_not_reconcile")
    normalized, point_by_id = _load_points(normalized_path, source_counts["normalized"])
    normalized_sha = generation["files"]["normalized"]["sha256"]
    candidate_lines, first_two, candidate_count = _candidate_rows(candidates_path, normalized_sha, point_by_id)
    points, state_counts = _build_points(normalized, point_by_id, first_two, SOURCE_CSV_SHA256)
    counts = _counts(points, candidate_count, source_counts, state_counts)
    if counts["rows_total"] != counts["points"] + counts["rejected"] + counts["outside_region"] + counts["excluded_non_stop"]:
        raise RuleTestError("source_counts_do_not_reconcile")
    if counts["direction_status_counts"]["inferred"] == 0:
        raise RuleTestError("inferred_count_zero_requires_design_review")

    temp_dir = Path(tempfile.mkdtemp(prefix=f".{dataset_id}.", dir=output_parent))
    try:
        candidate_files, candidate_raw, candidate_compressed = _write_shards(temp_dir, "candidates", candidate_lines)
        point_lines = (_canonical(point) + b"\n" for point in points)
        point_files, point_raw, point_compressed = _write_shards(temp_dir, "points", point_lines)
        uncompressed_total = candidate_raw + point_raw
        compressed_total = candidate_compressed + point_compressed
        if uncompressed_total > MAX_BUNDLE_UNCOMPRESSED or compressed_total > MAX_BUNDLE_COMPRESSED:
            raise RuleTestError("bundle_size_limit_exceeded")
        files = sorted(candidate_files + point_files, key=lambda item: (item["kind"], item["basename"]))
        if len(files) > MAX_BUNDLE_FILES:
            raise RuleTestError("bundle_file_count_limit_exceeded")
        code_sha = _sha256_bytes(Path(__file__).read_bytes())
        lock_path = _repo_root() / "backend" / "generation_io" / "uv.lock"
        lock_size, lock_sha = _sha256_path(lock_path)
        if lock_size <= 0:
            raise RuleTestError("uv_lock_missing")
        manifest = _make_manifest(source_manifest, notice, points, candidate_count, counts, files,
                                  runtime_versions, code_sha, lock_sha, _current_commit())
        manifest_bytes = _document(manifest)
        if len(manifest_bytes) > MAX_MANIFEST_BYTES:
            raise RuleTestError("manifest_size_limit_exceeded")
        report = {
            "schema_version": 1, "dataset_id": DATASET_ID, "source_month": SOURCE_MONTH, "region_id": REGION_ID,
            "source_zip_sha256": SOURCE_ZIP_SHA256, "source_csv_sha256": SOURCE_CSV_SHA256,
            "source_csv_bytes": SOURCE_CSV_BYTES, "boundary_sha256": source_manifest["boundary"]["sha256"],
            "input_manifest_sha256": source_manifest["input_manifest"]["sha256"],
            "rows_total": counts["rows_total"], "excluded_non_stop": counts["excluded_non_stop"],
            "outside_region": counts["outside_region"], "rejected": counts["rejected"],
            "points": counts["points"], "candidates": counts["candidates"],
            "direction_status_counts": counts["direction_status_counts"],
            "validity_status_counts": counts["validity_status_counts"],
            "imputation_method_counts": counts["imputation_method_counts"],
            "imputation_reason_counts": counts["imputation_reason_counts"],
            "validity_reason_counts": counts["validity_reason_counts"],
            "entrance_count_counts": state_counts["entrance_count_counts"],
            "inferred_ratio": counts["direction_status_counts"]["inferred"] / counts["points"],
            "limitations": [
                "Direction is a synthetic stop-to-neighbor proxy, not a verified vehicle approach direction.",
                "Validity remains unknown; no confirmed direction, ground-truth accuracy, or notification safety is claimed.",
                "Coordinates derive from the JARTIC 202602 source; synthetic=true marks this test-only derived dataset and its proxy directions.",
                "Historical acquisition time and acquisition-time terms are unknown; current terms are not historical evidence.",
            ],
        }
        report_bytes = _document(report)
        readme_bytes = _bundle_readme().encode("utf-8")
        if len(report_bytes) > MAX_REPORT_BYTES:
            raise RuleTestError("report_size_limit_exceeded")
        _write_exclusive(temp_dir / "manifest.json", manifest_bytes)
        _write_exclusive(temp_dir / "REPORT.json", report_bytes)
        _write_exclusive(temp_dir / "README.md", readme_bytes)
        verified = verify_bundle(temp_dir)
        if verified["content_sha256"] != manifest["content_sha256"]:
            raise RuleTestError("post_build_verification_digest_mismatch")
        if os.path.lexists(output):
            raise RuleTestError("output_directory_already_exists")
        os.rename(temp_dir, output)
        temp_dir = Path()
        return {"dataset_id": DATASET_ID, "content_sha256": manifest["content_sha256"],
                "points": counts["points"], "candidates": candidate_count,
                "inferred": counts["direction_status_counts"]["inferred"],
                "unknown": counts["direction_status_counts"]["unknown"],
                "bundle_compressed_bytes": compressed_total}
    finally:
        if temp_dir != Path() and temp_dir.exists():
            shutil.rmtree(temp_dir)


def _bundle_readme() -> str:
    return """# 名古屋 rule_test 一時データセット\n\nこのbundleは `nagoya-rule-test-202602-v1` というテスト専用のimmutable版です。JARTICの2026年02月分に由来する地域内実在地点を正規化し、欠損方向へ最近傍地点proxyを付けています。`synthetic=true` は座標がすべて架空という意味ではなく、テスト用に生成した方向値を含むデータであることを示します。\n\n方向の意味は `stop_to_neighbor_proxy`（停止地点から最近傍の別停止地点への方位）です。実道路の進行方向、車両の接近方向、標識面の向き、規制の現在有効性を示しません。confirmedは含まず、有効性はすべてunknownです。推定精度・現地確認・安全運転通知への適合性は保証しません。\n\n## 出典・加工・利用条件\n\n交通規制情報（公益財団法人日本道路交通情報センター、2026年02月分）を本プロジェクトが地域抽出・正規化し、近傍点proxyを付与しました。推定値は原提供者が作成・確認したものではありません。提供元案内: https://www.jartic.or.jp/service/opendata/ 。参照した現行利用条件: https://www.jartic.or.jp/d/opendata/riyou_kiyaku.pdf （2026年10月14日参照）。保存原本の取得日時と取得時に適用された規約は不明です。現在参照する規約は過去の取得条件の証拠ではありません。公開責任者の受容判断は法的保証を意味しません。\n\n## 生成・検証\n\n1. 保存原本のSHA-256が `f12388fca9283f0abd9104a3b265f88caac56f43a9add92404d22a34de0e6ea5` と一致し、member `typeD_aichi/愛知県警_202602_k_2.1.csv` のSHA-256が `0d902b472980d40fa7b472998e3424c73ad4730108fac9655ac1b23e82ee573d` と一致することを確認します。ZIP全体を展開せず、このmemberだけをprivate data directoryへ逐次展開し、元ZIP/CSVをGitへ追加しません。\n2. 固定B1境界（中心 35.1202166, 136.9121477、球面半径2000m、360頂点、9桁、`covers`）を用いて既存 `generation_io prepare-input` と `generate` を実行し、schema 3 `generation-manifest.json` をverifyします。source CSV SHA、boundary SHA、input manifest SHAは生成物から記録されます。\n3. lock環境と単一threadを固定して実行します。\n\n```sh\ncd backend/generation_io\nuv sync --locked --all-groups\nexport PYTHONHASHSEED=0 OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1\npython -m generation_io.rule_test_seed build --generation-manifest <verified-schema3-generation-manifest> --source-zip <private-path-to-typeD_aichi(1).zip> --dataset-id nagoya-rule-test-202602-v1 --notice-config <private-notice-config.json> --output-dir ../app/internal/seeddata/data/nagoya-rule-test-202602-v1\npython -m generation_io.rule_test_seed verify --bundle ../app/internal/seeddata/data/nagoya-rule-test-202602-v1\n```\n\n`REPORT.json` は入力件数、coverage、状態・理由件数、digestを記録します。manifestとJSONL shardは決定的に生成されます。同じ入力・lock環境で別の新規output directoryへ再生成し、全ファイルのSHA-256が一致することを確認してください。\n\n## 一時データの撤去・置換\n\n本番用データが完成したら、この一時datasetを本番へ昇格させず、新しいdataset IDのimmutable版を追加して置き換えます。追加投入、API selector切替、rollback、任意退役は別操作です。確認手順と退役時の人手ゲートは [`backend/app/db/seed/README.md` の「immutable dataset の切替・退役手順`](../../../../../db/seed/README.md#immutable-dataset-の切替退役手順) を参照してください。全reader停止、seed設定からのID除去、backup/rollback期間、archive保全を人が確認するまで削除しません。\n"""


def _manifest_keys() -> set[str]:
    return {"schema_version", "dataset_id", "source_month", "region_id", "synthetic", "counts", "geometry",
            "inputs", "generator", "provenance", "files", "content_sha256"}


def _validate_manifest(document: dict[str, Any]) -> None:
    if set(document) != _manifest_keys() or document.get("schema_version") != 2 or document.get("dataset_id") != DATASET_ID:
        raise RuleTestError("bundle_manifest_schema_or_identity_invalid")
    if document.get("source_month") != SOURCE_MONTH or document.get("region_id") != REGION_ID or document.get("synthetic") is not True:
        raise RuleTestError("bundle_manifest_scope_invalid")
    if not isinstance(document.get("content_sha256"), str) or not SHA256_RE.fullmatch(document["content_sha256"]):
        raise RuleTestError("bundle_manifest_digest_invalid")
    actual_digest = _sha256_bytes(_canonical({key: value for key, value in document.items() if key != "content_sha256"}))
    if actual_digest != document["content_sha256"]:
        raise RuleTestError("bundle_manifest_digest_mismatch")
    counts = document.get("counts")
    required_count_keys = {"points", "candidates", "rejected", "outside_region", "rows_total", "excluded_non_stop",
                           "direction_status_counts", "validity_status_counts", "imputation_method_counts",
                           "imputation_reason_counts", "validity_reason_counts"}
    if not isinstance(counts, dict) or set(counts) != required_count_keys:
        raise RuleTestError("bundle_counts_schema_invalid")
    for key in ("points", "candidates", "rejected", "outside_region", "rows_total", "excluded_non_stop"):
        if isinstance(counts[key], bool) or not isinstance(counts[key], int) or counts[key] < 0:
            raise RuleTestError("bundle_count_invalid")
    if not 0 < counts["points"] <= MAX_POINTS or counts["candidates"] > MAX_CANDIDATES:
        raise RuleTestError("bundle_count_outside_supported_limit")
    if counts["rows_total"] != counts["points"] + counts["rejected"] + counts["outside_region"] + counts["excluded_non_stop"]:
        raise RuleTestError("bundle_source_counts_do_not_reconcile")
    for field, keys in (("direction_status_counts", DIRECTION_STATUS_KEYS), ("validity_status_counts", VALIDITY_STATUS_KEYS),
                        ("imputation_method_counts", METHOD_KEYS), ("imputation_reason_counts", DIRECTION_REASON_KEYS),
                        ("validity_reason_counts", VALIDITY_REASON_KEYS)):
        values = counts[field]
        if not isinstance(values, dict) or set(values) != set(keys) or any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in values.values()):
            raise RuleTestError(f"bundle_count_map_invalid:{field}")
        if sum(values.values()) != counts["points"]:
            raise RuleTestError(f"bundle_count_map_total_mismatch:{field}")
    if counts["direction_status_counts"]["inferred"] <= 0 or counts["direction_status_counts"]["confirmed"] != 0:
        raise RuleTestError("bundle_direction_status_counts_invalid")
    if counts["validity_status_counts"] != {"unknown": counts["points"], "valid": 0, "invalid": 0}:
        raise RuleTestError("bundle_validity_must_remain_unknown")
    if counts["validity_reason_counts"] != {"source_validity_unverified": counts["points"], "active_regulation_evidence": 0,
                                             "withdrawn_regulation_evidence": 0, "synthetic_fixture": 0, "legacy_unrecorded": 0}:
        raise RuleTestError("bundle_validity_reason_invalid")
    geometry = document.get("geometry")
    geometry_keys = {"region_id", "center_lat", "center_lon", "center_source", "radius_m", "sphere_radius_m", "vertices",
                     "coordinate_decimal_places", "algorithm", "boundary_inclusion", "boundary_sha256"}
    if not isinstance(geometry, dict) or set(geometry) != geometry_keys:
        raise RuleTestError("bundle_geometry_schema_invalid")
    if (geometry["region_id"] != REGION_ID or geometry["center_lat"] != "35.1202166" or geometry["center_lon"] != "136.9121477"
            or geometry["radius_m"] != 2000 or geometry["sphere_radius_m"] != "6371008.8" or geometry["vertices"] != 360
            or geometry["coordinate_decimal_places"] != 9 or geometry["algorithm"] != "spherical-destination-v1"
            or geometry["boundary_inclusion"] != "covers" or not isinstance(geometry["boundary_sha256"], str)
            or not SHA256_RE.fullmatch(geometry["boundary_sha256"])):
        raise RuleTestError("bundle_geometry_contract_mismatch")
    inputs = document.get("inputs")
    input_keys = {"source_zip_sha256", "source_csv_sha256", "source_csv_bytes", "source_header_sha256", "boundary_sha256",
                  "input_manifest_sha256", "enrichment_sha256"}
    if not isinstance(inputs, dict) or set(inputs) != input_keys:
        raise RuleTestError("bundle_inputs_schema_invalid")
    if (inputs["source_zip_sha256"] != SOURCE_ZIP_SHA256 or inputs["source_csv_sha256"] != SOURCE_CSV_SHA256
            or inputs["source_csv_bytes"] != SOURCE_CSV_BYTES or inputs["source_header_sha256"] != SOURCE_HEADER_SHA256
            or inputs["boundary_sha256"] != geometry["boundary_sha256"] or not SHA256_RE.fullmatch(inputs["input_manifest_sha256"])
            or inputs["enrichment_sha256"] is not None):
        raise RuleTestError("bundle_input_identity_invalid")
    generator = document.get("generator")
    generator_keys = {"code_ref", "code_sha256", "method_implementation_sha256", "method_version", "source_selection_order",
                      "commit", "uv_lock_sha256", "python_version", "dependency_versions", "zlib_version", "kernel",
                      "max_candidates", "candidate_radius_m", "thread_policy"}
    if not isinstance(generator, dict) or set(generator) != generator_keys:
        raise RuleTestError("bundle_generator_schema_invalid")
    for key in ("code_sha256", "method_implementation_sha256", "uv_lock_sha256"):
        if not isinstance(generator[key], str) or not SHA256_RE.fullmatch(generator[key]):
            raise RuleTestError("bundle_generator_digest_invalid")
    if (not isinstance(generator["code_ref"], str) or not generator["code_ref"] or not isinstance(generator["commit"], str)
            or not generator["commit"] or generator["method_version"] != METHOD_VERSION
            or generator["source_selection_order"] != ["nearest_stop_reference", "unknown"]
            or generator["max_candidates"] != MAX_CANDIDATES_PER_POINT or generator["candidate_radius_m"] != 80):
        raise RuleTestError("bundle_generator_contract_invalid")
    provenance = document.get("provenance")
    if not isinstance(provenance, dict) or set(provenance) != set(PROVENANCE_KEYS):
        raise RuleTestError("bundle_provenance_schema_invalid")
    for field in PROVENANCE_KEYS:
        if not isinstance(provenance[field], str) or not provenance[field].strip():
            raise RuleTestError("bundle_provenance_value_invalid")
    if (provenance["publisher"] != PUBLISHER or provenance["edition"] != "2026年02月分"
            or provenance["source_url"] != SOURCE_URL or provenance["terms_reference"] != TERMS_REFERENCE
            or provenance["terms_sha256"] != TERMS_SHA256 or provenance["rights_status"] != RIGHTS_STATUS
            or provenance["identity_scope"] != "source_month" or provenance["data_purpose"] != "rule_test"):
        raise RuleTestError("bundle_provenance_contract_invalid")
    files = document.get("files")
    if not isinstance(files, list) or not 1 <= len(files) <= MAX_BUNDLE_FILES:
        raise RuleTestError("bundle_file_list_invalid")
    previous = ("", "")
    seen_names: set[str] = set()
    kind_counts = {"points": 0, "candidates": 0}
    total_rows = total_comp = total_uncomp = 0
    for item in files:
        expected_fields = {"kind", "basename", "rows", "compressed_bytes", "compressed_sha256", "uncompressed_bytes", "uncompressed_sha256"}
        if not isinstance(item, dict) or set(item) != expected_fields or item["kind"] not in kind_counts:
            raise RuleTestError("bundle_file_entry_invalid")
        basename = item["basename"]
        if not isinstance(basename, str) or PurePosixPath(basename).name != basename or basename in seen_names:
            raise RuleTestError("bundle_file_basename_invalid")
        current = (item["kind"], basename)
        if current <= previous:
            raise RuleTestError("bundle_file_order_invalid")
        previous = current
        seen_names.add(basename)
        for number in ("rows", "compressed_bytes", "uncompressed_bytes"):
            if isinstance(item[number], bool) or not isinstance(item[number], int) or item[number] <= 0:
                raise RuleTestError("bundle_file_size_or_rows_invalid")
        for digest_key in ("compressed_sha256", "uncompressed_sha256"):
            if not isinstance(item[digest_key], str) or not SHA256_RE.fullmatch(item[digest_key]):
                raise RuleTestError("bundle_file_digest_invalid")
        if not basename.startswith(item["kind"] + "-") or not basename.endswith(".jsonl.gz"):
            raise RuleTestError("bundle_file_name_kind_mismatch")
        if item["uncompressed_bytes"] > MAX_SHARD_BYTES:
            raise RuleTestError("bundle_shard_limit_exceeded")
        kind_counts[item["kind"]] += item["rows"]
        total_rows += item["rows"]
        total_comp += item["compressed_bytes"]
        total_uncomp += item["uncompressed_bytes"]
    if kind_counts["points"] != counts["points"] or kind_counts["candidates"] != counts["candidates"]:
        raise RuleTestError("bundle_files_count_mismatch")
    if total_comp > MAX_BUNDLE_COMPRESSED or total_uncomp > MAX_BUNDLE_UNCOMPRESSED:
        raise RuleTestError("bundle_total_size_limit_exceeded")


def _read_gzip_shard(path: Path, descriptor: dict[str, Any]) -> list[dict[str, Any]]:
    compressed = path.read_bytes()
    if len(compressed) != descriptor["compressed_bytes"] or _sha256_bytes(compressed) != descriptor["compressed_sha256"]:
        raise RuleTestError("bundle_shard_compressed_digest_mismatch")
    try:
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        raw = decoder.decompress(compressed, MAX_SHARD_BYTES + 1)
        if len(raw) > MAX_SHARD_BYTES or decoder.unconsumed_tail:
            raise RuleTestError("bundle_shard_expanded_limit_exceeded")
        raw += decoder.flush(MAX_SHARD_BYTES + 1 - len(raw))
    except zlib.error as exc:
        raise RuleTestError("bundle_shard_gzip_invalid") from exc
    if len(raw) > MAX_SHARD_BYTES or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
        raise RuleTestError("bundle_shard_multiple_member_or_trailing_data")
    if len(raw) != descriptor["uncompressed_bytes"] or _sha256_bytes(raw) != descriptor["uncompressed_sha256"]:
        raise RuleTestError("bundle_shard_uncompressed_digest_mismatch")
    records: list[dict[str, Any]] = []
    for line in raw.splitlines(keepends=True):
        if len(line) > MAX_JSONL_LINE_BYTES or not line.endswith(b"\n") or b"\r" in line:
            raise RuleTestError("bundle_jsonl_line_invalid")
        value = _loads(line[:-1])
        if not isinstance(value, dict):
            raise RuleTestError("bundle_jsonl_row_not_object")
        records.append(value)
    if not records or len(records) != descriptor["rows"]:
        raise RuleTestError("bundle_shard_row_count_mismatch")
    return records


def _validate_point_row(point: dict[str, Any], csv_sha: str) -> None:
    if set(point) != set(POINT_KEYS):
        raise RuleTestError("bundle_point_schema_mismatch")
    if (not isinstance(point["entity_id"], str) or not point["entity_id"] or not isinstance(point["raw_uid"], str)
            or not UID_RE.fullmatch(point["raw_uid"]) or point["normalization_status"] != "valid_candidate_only"):
        raise RuleTestError("bundle_point_identity_invalid")
    if not _valid_number(point["lon"]) or not -180 <= point["lon"] <= 180 or not _valid_number(point["lat"]) or not -90 <= point["lat"] <= 90:
        raise RuleTestError("bundle_point_coordinate_invalid")
    if point["direction_status"] == "unknown":
        if (point["direction_degrees"] is not None or point["imputation_method"] != "none" or point["direction_source"] != "unknown"
                or point["imputation_reason"] not in UNKNOWN_DIRECTION_REASONS or point["direction_evidence"] != {}):
            raise RuleTestError("bundle_point_unknown_state_invalid")
    elif point["direction_status"] == "inferred":
        evidence = point["direction_evidence"]
        if (not _valid_number(point["direction_degrees"]) or not 0 <= point["direction_degrees"] < 360
                or point["imputation_method"] != DIRECTION_METHOD or point["imputation_reason"] != DIRECTION_REASON
                or point["direction_source"] != DIRECTION_SOURCE or not isinstance(evidence, dict)
                or set(evidence) != {"schema_version", "kind", "source_csv_sha256", "method_version", "direction_semantics", "neighbor_entity_id", "candidate_rank"}
                or evidence["schema_version"] != 2 or evidence["kind"] != "nearest_stop_reference"
                or evidence["source_csv_sha256"] != csv_sha or evidence["method_version"] != "nearest-stop-proxy-v1"
                or evidence["direction_semantics"] != DIRECTION_SEMANTICS or evidence["candidate_rank"] != 1
                or not isinstance(evidence["neighbor_entity_id"], str)):
            raise RuleTestError("bundle_point_inferred_state_invalid")
    else:
        raise RuleTestError("bundle_confirmed_or_unknown_status_forbidden")
    if (point["validity_status"] != "unknown" or point["validity_reason"] != "source_validity_unverified"
            or point["validity_evidence"] != {}):
        raise RuleTestError("bundle_point_validity_must_remain_unknown")


def _validate_candidate_row(candidate: dict[str, Any], point_by_id: dict[str, dict[str, Any]]) -> None:
    if set(candidate) != set(CANDIDATE_KEYS):
        raise RuleTestError("bundle_candidate_schema_mismatch")
    source_id, neighbor_id = candidate["source_entity_id"], candidate["neighbor_entity_id"]
    rank = candidate["rank"]
    if (not isinstance(source_id, str) or source_id not in point_by_id or not isinstance(neighbor_id, str)
            or neighbor_id not in point_by_id or source_id == neighbor_id or isinstance(rank, bool)
            or not isinstance(rank, int) or not 1 <= rank <= MAX_CANDIDATES_PER_POINT):
        raise RuleTestError("bundle_candidate_reference_or_rank_invalid")
    if (not _valid_number(candidate["distance_m"]) or not 0 <= candidate["distance_m"] <= CANDIDATE_RADIUS_M
            or not _valid_number(candidate["candidate_bearing"]) or not 0 <= candidate["candidate_bearing"] < 360
            or candidate["bearing_semantics"] != BEARING_SEMANTICS):
        raise RuleTestError("bundle_candidate_geometry_invalid")
    features = candidate["features"]
    if not isinstance(features, dict) or not features or not set(features).issubset(FEATURE_KEYS):
        raise RuleTestError("bundle_candidate_features_invalid")
    if any(not _valid_number(value) for value in features.values()):
        raise RuleTestError("bundle_candidate_feature_non_finite")
    for key, expected in (("candidate_rank", rank), ("candidate_distance_m", candidate["distance_m"]),
                          ("candidate_bearing", candidate["candidate_bearing"])):
        if features.get(key) != expected:
            raise RuleTestError("bundle_candidate_feature_mismatch")


def verify_bundle(bundle: str | os.PathLike[str]) -> dict[str, Any]:
    root = Path(bundle).expanduser().absolute()
    _reject_symlink_components(root)
    if not root.is_dir():
        raise RuleTestError("bundle_directory_not_found")
    manifest_path = root / "manifest.json"
    _regular_file(manifest_path)
    if manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
        raise RuleTestError("manifest_size_limit_exceeded")
    manifest = _loads(manifest_path.read_bytes())
    if not isinstance(manifest, dict):
        raise RuleTestError("bundle_manifest_not_object")
    _validate_manifest(manifest)
    expected_names = {"manifest.json", "README.md", "REPORT.json", *(item["basename"] for item in manifest["files"])}
    actual_names = {entry.name for entry in root.iterdir()}
    if actual_names != expected_names:
        raise RuleTestError("bundle_directory_file_set_mismatch")
    readme_path = _regular_file(root / "README.md")
    report_path = _regular_file(root / "REPORT.json")
    readme = readme_path.read_text(encoding="utf-8")
    if "stop_to_neighbor_proxy" not in readme or "取得日時" not in readme or "一時" not in readme:
        raise RuleTestError("bundle_readme_notice_missing")
    if report_path.stat().st_size > MAX_REPORT_BYTES:
        raise RuleTestError("report_size_limit_exceeded")
    report = _loads(report_path.read_bytes())
    if not isinstance(report, dict) or report.get("dataset_id") != DATASET_ID:
        raise RuleTestError("bundle_report_invalid")

    points: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    total_rows = total_comp = total_uncomp = 0
    for descriptor in manifest["files"]:
        path = _regular_file(root / descriptor["basename"])
        rows = _read_gzip_shard(path, descriptor)
        total_rows += len(rows)
        total_comp += descriptor["compressed_bytes"]
        total_uncomp += descriptor["uncompressed_bytes"]
        if descriptor["kind"] == "points":
            points.extend(rows)
        else:
            candidates.extend(rows)
    if total_comp > MAX_BUNDLE_COMPRESSED or total_uncomp > MAX_BUNDLE_UNCOMPRESSED:
        raise RuleTestError("bundle_total_size_limit_exceeded")
    if len(points) != manifest["counts"]["points"] or len(candidates) != manifest["counts"]["candidates"]:
        raise RuleTestError("bundle_record_count_mismatch")
    if points != sorted(points, key=lambda row: row.get("entity_id", "")):
        raise RuleTestError("bundle_point_sort_invalid")
    point_by_id: dict[str, dict[str, Any]] = {}
    point_by_uid: set[str] = set()
    for point in points:
        _validate_point_row(point, manifest["inputs"]["source_csv_sha256"])
        if point["entity_id"] in point_by_id or point["raw_uid"] in point_by_uid:
            raise RuleTestError("bundle_point_duplicate_identity")
        point_by_id[point["entity_id"]] = point
        point_by_uid.add(point["raw_uid"])
    candidate_order = [(row.get("source_entity_id", ""), row.get("rank", 0)) for row in candidates]
    if candidate_order != sorted(candidate_order):
        raise RuleTestError("bundle_candidate_sort_invalid")
    candidate_groups: dict[str, list[dict[str, Any]]] = {}
    previous = ("", 0)
    for candidate in candidates:
        _validate_candidate_row(candidate, point_by_id)
        current = (candidate["source_entity_id"], candidate["rank"])
        if current == previous:
            raise RuleTestError("bundle_candidate_duplicate_rank")
        previous = current
        group = candidate_groups.setdefault(current[0], [])
        if current[1] != len(group) + 1:
            raise RuleTestError("bundle_candidate_rank_sequence_invalid")
        group.append(candidate)
    xy, _ = _project_points(points)
    ids = [point["entity_id"] for point in points]
    observed_direction = {key: 0 for key in DIRECTION_STATUS_KEYS}
    observed_method = {key: 0 for key in METHOD_KEYS}
    observed_reason = {key: 0 for key in DIRECTION_REASON_KEYS}
    observed_validity_reason = {key: 0 for key in VALIDITY_REASON_KEYS}
    observed_validity = {key: 0 for key in VALIDITY_STATUS_KEYS}
    for index, point in enumerate(points):
        group = candidate_groups.get(point["entity_id"], [])
        nearest_actual = _nearest_two(index, xy, ids)
        degree = point["direction_degrees"]
        if point["direction_status"] == "inferred":
            evidence = point["direction_evidence"]
            if not group or group[0]["rank"] != 1 or evidence["neighbor_entity_id"] != group[0]["neighbor_entity_id"]:
                raise RuleTestError("bundle_direction_neighbor_mismatch")
            if (nearest_actual[2] != group[0]["neighbor_entity_id"] or abs(nearest_actual[0] - group[0]["distance_m"]) > EPSILON_M
                    or nearest_actual[0] <= EPSILON_M or nearest_actual[0] > CANDIDATE_RADIUS_M
                    or nearest_actual[1] - nearest_actual[0] <= EPSILON_M):
                raise RuleTestError("bundle_inferred_nearest_recomputation_mismatch")
            if abs((degree - group[0]["candidate_bearing"] + 180.0) % 360.0 - 180.0) > EPSILON_M:
                raise RuleTestError("bundle_direction_bearing_mismatch")
        elif point["imputation_reason"] == "no_neighbor_within_radius":
            if nearest_actual[0] <= CANDIDATE_RADIUS_M or group:
                raise RuleTestError("bundle_no_neighbor_reason_mismatch")
        elif point["imputation_reason"] == "zero_length_geometry":
            if nearest_actual[0] > EPSILON_M or not group or abs(group[0]["distance_m"] - nearest_actual[0]) > EPSILON_M:
                raise RuleTestError("bundle_zero_length_reason_mismatch")
        elif point["imputation_reason"] == "ambiguous_nearest_neighbor":
            if nearest_actual[1] - nearest_actual[0] > EPSILON_M or not group or abs(group[0]["distance_m"] - nearest_actual[0]) > EPSILON_M:
                raise RuleTestError("bundle_tie_reason_mismatch")
        observed_direction[point["direction_status"]] += 1
        observed_method[point["imputation_method"]] += 1
        observed_reason[point["imputation_reason"]] += 1
        observed_validity[point["validity_status"]] += 1
        observed_validity_reason[point["validity_reason"]] += 1
    counts = manifest["counts"]
    for key, value in (("direction_status_counts", observed_direction), ("validity_status_counts", observed_validity),
                       ("imputation_method_counts", observed_method), ("imputation_reason_counts", observed_reason),
                       ("validity_reason_counts", observed_validity_reason)):
        if value != counts[key]:
            raise RuleTestError(f"bundle_state_count_mismatch:{key}")
    for key, expected in (("points", counts["points"]), ("candidates", counts["candidates"]),
                          ("rows_total", counts["rows_total"]), ("excluded_non_stop", counts["excluded_non_stop"]),
                          ("outside_region", counts["outside_region"]), ("rejected", counts["rejected"])):
        if report.get(key) != expected:
            raise RuleTestError(f"bundle_report_count_mismatch:{key}")
    for key, expected in (("direction_status_counts", observed_direction), ("validity_status_counts", observed_validity),
                          ("imputation_method_counts", observed_method), ("imputation_reason_counts", observed_reason),
                          ("validity_reason_counts", observed_validity_reason)):
        if report.get(key) != expected:
            raise RuleTestError(f"bundle_report_state_mismatch:{key}")
    return {"dataset_id": DATASET_ID, "content_sha256": manifest["content_sha256"], "points": len(points),
            "candidates": len(candidates), "inferred": observed_direction["inferred"], "unknown": observed_direction["unknown"],
            "compressed_bytes": total_comp, "uncompressed_bytes": total_uncomp}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="Build a deterministic rule_test bundle from a verified schema-3 generation")
    build.add_argument("--generation-manifest", required=True)
    build.add_argument("--source-zip", required=True)
    build.add_argument("--dataset-id", required=True)
    build.add_argument("--notice-config", required=True)
    build.add_argument("--output-dir", required=True)
    verify = commands.add_parser("verify", help="Verify a completed rule_test bundle without a database")
    verify.add_argument("--bundle", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "build":
            result = build_bundle(args.generation_manifest, args.source_zip, args.dataset_id,
                                  args.notice_config, args.output_dir)
            result["status"] = "built"
        else:
            result = verify_bundle(args.bundle)
            result["status"] = "valid"
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 0
    except (RuleTestError, PortableArtifactError) as exc:
        print(f"rule_test_seed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

