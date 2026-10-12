from __future__ import annotations

import hashlib
import json
import re
import stat
import csv
import zlib
from decimal import Decimal, InvalidOperation
from io import TextIOWrapper
from pathlib import Path
from typing import Any, BinaryIO


FIXTURE_MANIFEST_SHA256 = "900bab5cd47cacbc191a5edd7e9afb709aeca1163146689b08de30269f554c88"
FIXTURE_SOURCE_SHA256 = "8b39a90b6b6313bf6293d649732b87b7fa970af1d925a5a2fc241b05139f33fa"
FIXTURE_SOURCE_BYTES = 19497
SOURCE_OBSERVATION_SHA256 = "1a2dfd7e4134c9c0b9d7cbfa356fc638088fa6f1e72b174257317525e978b825"
SOURCE_OBSERVATION_BYTES = 37052
ORIGINAL_CSV_SHA256 = "0d902b472980d40fa7b472998e3424c73ad4730108fac9655ac1b23e82ee573d"
ORIGINAL_CSV_BYTES = 363440778
HEADER_SHA256 = "74334997340bd11605f3cb3edbc8cea93782df73af3cdc2492b9c48cb6825ca1"
SOURCE_MONTH = "202602"
SOURCE_VERSION = "k_2.1"
SELECTED_FIELDS = (
    "version",
    "prefecture",
    "police",
    "regulation",
    "update",
    "key",
    "branch",
    "location",
    "entrance",
)
UID_PATTERN = re.compile(r"[A-Za-z0-9]{32}\Z", re.ASCII)


class SourceError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def sha256_file(path: str | Path) -> tuple[int, str]:
    file_path = Path(path)
    _require_regular_file(file_path)
    digest = hashlib.sha256()
    size = 0
    with file_path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def load_selected_projection(
    source_path: str | Path, contract_path: str | Path
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    source_file = Path(source_path)
    contract_file = Path(contract_path)
    contract_size, contract_digest = sha256_file(contract_file)
    if contract_size <= 0 or contract_digest != FIXTURE_MANIFEST_SHA256:
        raise SourceError("source_contract_hash_mismatch")
    try:
        contract = json.loads(contract_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SourceError("invalid_source_contract") from exc
    if contract.get("input_kind") != "selected_projection":
        raise SourceError("unknown_input_kind")
    if contract.get("source_month") != SOURCE_MONTH:
        raise SourceError("unknown_month")
    if contract.get("rows") != 64 or contract.get("production_approved") is not False:
        raise SourceError("invalid_fixture_contract")
    if contract.get("not_original_full_csv_rows") is not True:
        raise SourceError("fixture_provenance_missing")
    if contract.get("original_csv_sha256") != ORIGINAL_CSV_SHA256 or contract.get(
        "original_csv_bytes"
    ) != ORIGINAL_CSV_BYTES:
        raise SourceError("original_source_ref_mismatch")
    if contract.get("projection_columns") != list(SELECTED_FIELDS):
        raise SourceError("projection_schema_mismatch")
    if contract.get("header_utf8_sha256") != HEADER_SHA256 or not _header_matches_hash(
        contract.get("header")
    ):
        raise SourceError("header_hash_mismatch")

    source_ref = contract.get("file")
    observation_ref = contract.get("source_observation")
    if not isinstance(source_ref, dict) or not isinstance(observation_ref, dict):
        raise SourceError("fixture_parent_ref_missing")
    if source_ref.get("bytes") != FIXTURE_SOURCE_BYTES or source_ref.get(
        "sha256"
    ) != FIXTURE_SOURCE_SHA256:
        raise SourceError("source_hash_mismatch")
    if observation_ref.get("bytes") != SOURCE_OBSERVATION_BYTES or observation_ref.get(
        "sha256"
    ) != SOURCE_OBSERVATION_SHA256:
        raise SourceError("source_observation_hash_mismatch")

    source_size, source_digest = sha256_file(source_file)
    if source_size != FIXTURE_SOURCE_BYTES or source_digest != FIXTURE_SOURCE_SHA256:
        raise SourceError("source_hash_mismatch")
    rows: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    previous_source_row = 0
    try:
        with source_file.open("r", encoding="utf-8", newline="") as stream:
            for line_number, line in enumerate(stream, start=1):
                if not line.endswith("\n"):
                    raise SourceError("unterminated_jsonl")
                try:
                    item = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise SourceError("invalid_projection_json") from exc
                if not isinstance(item, dict) or set(item) != {
                    "source_row",
                    "selected_utf8_sha256",
                    "selected",
                }:
                    raise SourceError("invalid_projection_record")
                source_row = item["source_row"]
                selected = item["selected"]
                if isinstance(source_row, bool) or not isinstance(source_row, int) or source_row <= previous_source_row:
                    raise SourceError("invalid_source_order")
                if not isinstance(selected, dict) or tuple(selected) != SELECTED_FIELDS:
                    raise SourceError("projection_schema_mismatch")
                if any(not isinstance(value, str) for value in selected.values()):
                    raise SourceError("invalid_projection_value")
                selected_digest = hashlib.sha256(
                    json.dumps(selected, ensure_ascii=False, sort_keys=True).encode("utf-8")
                ).hexdigest()
                if item["selected_utf8_sha256"] != selected_digest:
                    raise SourceError("selected_field_hash_mismatch")
                if selected["version"] != SOURCE_VERSION:
                    raise SourceError("unknown_version")
                if selected["prefecture"] != "23":
                    raise SourceError("unknown_prefecture")
                if selected["update"] != "" or selected["branch"] != "":
                    raise SourceError("fixture_projection_changed")
                uid = selected["key"]
                if not UID_PATTERN.fullmatch(uid):
                    raise SourceError("invalid_raw_uid")
                if uid in seen_keys:
                    raise SourceError("duplicate_raw_uid")
                seen_keys.add(uid)
                previous_source_row = source_row
                rows.append(item)
    except SourceError:
        raise
    except (OSError, UnicodeError) as exc:
        raise SourceError("source_read_error") from exc
    if len(rows) != 64:
        raise SourceError("fixture_row_count_mismatch")
    return contract, rows


def iter_raw_records(stream: BinaryIO, contract: dict[str, Any]):
    if contract.get("source_month") != SOURCE_MONTH:
        raise SourceError("unknown_month")
    if contract.get("version") != SOURCE_VERSION:
        raise SourceError("unknown_version")
    header = contract.get("header")
    if not _header_matches_hash(header):
        raise SourceError("header_hash_mismatch")
    expected_sha = contract.get("csv_sha256")
    expected_crc = contract.get("csv_crc32")
    expected_bytes = contract.get("csv_bytes")
    if (
        not isinstance(expected_sha, str)
        or not re.fullmatch(r"[0-9a-f]{64}", expected_sha, re.ASCII)
        or not isinstance(expected_crc, str)
        or not re.fullmatch(r"[0-9a-f]{8}", expected_crc, re.ASCII)
        or isinstance(expected_bytes, bool)
        or not isinstance(expected_bytes, int)
        or expected_bytes < 0
    ):
        raise SourceError("invalid_source_manifest")
    try:
        stream.seek(0)
        digest = hashlib.sha256()
        crc = 0
        size = 0
        while chunk := stream.read(1024 * 1024):
            if not isinstance(chunk, bytes):
                raise SourceError("invalid_source_stream")
            size += len(chunk)
            digest.update(chunk)
            crc = zlib.crc32(chunk, crc)
        if size != expected_bytes or digest.hexdigest() != expected_sha:
            raise SourceError("source_hash_mismatch")
        if f"{crc & 0xffffffff:08x}" != expected_crc:
            raise SourceError("source_crc_mismatch")
        stream.seek(0)
    except (OSError, AttributeError) as exc:
        raise SourceError("source_stream_not_seekable") from exc

    text_stream = TextIOWrapper(stream, encoding="cp932", errors="strict", newline="")
    try:
        reader = csv.reader(text_stream, strict=True)
        try:
            actual_header = next(reader)
        except StopIteration as exc:
            raise SourceError("empty_source") from exc
        if actual_header != header:
            raise SourceError("header_mismatch")
        indexes = {name: index for index, name in enumerate(header)}
        column_names = {
            "version": "拡張版標準フォーマット種別・バージョン",
            "prefecture": "都道府県コード",
            "police": "警察署コード",
            "regulation": "共通規制種別コード",
            "update": "データ更新区分コード",
            "key": "ユニークキー",
            "branch": "枝番号",
            "location": "規制場所の経度緯度",
            "entrance": "進入方向(座標)",
        }
        seen: set[str] = set()
        for source_row, raw_row in enumerate(reader, start=2):
            if len(raw_row) != len(header):
                raise SourceError("invalid_row_width")
            selected = {
                key: raw_row[indexes[column]] for key, column in column_names.items()
            }
            if selected["version"] != SOURCE_VERSION:
                raise SourceError("unknown_version")
            uid = selected["key"]
            if not UID_PATTERN.fullmatch(uid):
                raise SourceError("invalid_raw_uid")
            if uid in seen:
                raise SourceError("duplicate_raw_uid")
            seen.add(uid)
            yield {"source_row": source_row, "selected": selected}
    except UnicodeDecodeError as exc:
        raise SourceError("invalid_encoding") from exc
    except csv.Error as exc:
        raise SourceError("invalid_csv") from exc
    finally:
        text_stream.close()


def _header_matches_hash(header: Any) -> bool:
    if not isinstance(header, list) or len(header) != 170:
        return False
    if any(not isinstance(column, str) for column in header) or len(set(header)) != 170:
        return False
    digest = hashlib.sha256(json.dumps(header, ensure_ascii=False).encode("utf-8")).hexdigest()
    return digest == HEADER_SHA256


def normalize_selected_record(
    selected: dict[str, str],
    *,
    source_row: int,
    source_month: str,
    region_id: str,
    boundary: Any,
    selected_hash: str,
) -> dict[str, Any]:
    if source_month != SOURCE_MONTH:
        raise SourceError("unknown_month")
    if selected.get("version") != SOURCE_VERSION:
        raise SourceError("unknown_version")
    uid = selected.get("key")
    if not isinstance(uid, str) or not UID_PATTERN.fullmatch(uid):
        raise SourceError("invalid_raw_uid")
    if selected.get("regulation") != "63":
        return {"classification": "excluded_non_stop"}
    if not re.fullmatch(r"[0-9a-f]{64}", selected_hash, re.ASCII):
        raise SourceError("invalid_selected_hash")
    update = selected.get("update")
    if update != "":
        return _rejected(selected, source_row, selected_hash, "unsupported_update_code")

    location = selected.get("location")
    if not isinstance(location, str):
        raise SourceError("invalid_location_geometry")
    if location == "":
        return _rejected(selected, source_row, selected_hash, "missing_location")
    if "/" in location:
        return _rejected(selected, source_row, selected_hash, "multiple_location_regions")
    if ";" in location:
        return _rejected(selected, source_row, selected_hash, "multiple_location_points")
    lon_token, lat_token = _parse_pair(location, "invalid_location_geometry")

    entrance_raw = selected.get("entrance", "")
    if not isinstance(entrance_raw, str):
        return _rejected(selected, source_row, selected_hash, "invalid_entrance_geometry")
    entrance_tokens: list[str] = []
    entrance_points: list[dict[str, Any]] = []
    if entrance_raw:
        if "/" in entrance_raw:
            return _rejected(selected, source_row, selected_hash, "invalid_entrance_geometry")
        for pair_token in entrance_raw.split(";"):
            try:
                entrance_lon, entrance_lat = _parse_pair(
                    pair_token, "invalid_entrance_geometry"
                )
            except SourceError:
                return _rejected(selected, source_row, selected_hash, "invalid_entrance_geometry")
            entrance_tokens.append(pair_token)
            entrance_points.append(
                {
                    "lon_token": entrance_lon,
                    "lat_token": entrance_lat,
                    "lon": float(Decimal(entrance_lon)),
                    "lat": float(Decimal(entrance_lat)),
                }
            )

    lon_decimal, lat_decimal = Decimal(lon_token), Decimal(lat_token)
    included, included_boundary = boundary.covers(lon_decimal, lat_decimal)
    if not included:
        return {
            "classification": "outside_region",
            "raw_uid": uid,
            "source_row": source_row,
        }
    return {
        "classification": "normalized",
        "record": {
            "entity_id": f"jp:aichi:nagoya/{source_month}/{uid}",
            "region_id": region_id,
            "source_row": source_row,
            "selected_field_sha256": selected_hash,
            "source_month": source_month,
            "prefecture": selected["prefecture"],
            "raw_uid": uid,
            "record_id": uid,
            "jartic_record_id": uid,
            "ユニークキー": uid,
            "police": selected["police"],
            "regulation": selected["regulation"],
            "branch": selected.get("branch", ""),
            "raw_update": update,
            "update_semantics": "unknown",
            "source_active_status": "unknown",
            "source_location_token": location,
            "source_entrance_tokens": entrance_tokens,
            "entrance_points": entrance_points,
            "lon": float(lon_decimal),
            "lat": float(lat_decimal),
            "normalization_status": "valid_candidate_only",
            "included_boundary": included_boundary,
            "production_approved": False,
        },
    }


def _rejected(
    selected: dict[str, str], source_row: int, selected_hash: str, reason: str
) -> dict[str, Any]:
    return {
        "classification": "rejected",
        "reason": reason,
        "raw_uid": selected.get("key", ""),
        "source_row": source_row,
        "selected_field_sha256": selected_hash,
    }


def _parse_pair(value: str, reason: str) -> tuple[str, str]:
    tokens = value.split()
    if len(tokens) != 2:
        raise SourceError(reason)
    try:
        lon = Decimal(tokens[0])
        lat = Decimal(tokens[1])
    except InvalidOperation as exc:
        raise SourceError(reason) from exc
    if (
        not lon.is_finite()
        or not lat.is_finite()
        or lon < -180
        or lon > 180
        or lat < -90
        or lat > 90
    ):
        raise SourceError(reason)
    return tokens[0], tokens[1]


def _require_regular_file(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise SourceError("missing_source_file") from exc
    if not stat.S_ISREG(mode):
        raise SourceError("source_not_regular_file")
