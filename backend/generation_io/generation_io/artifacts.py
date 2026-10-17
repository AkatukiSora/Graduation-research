from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import json
import math
import os
import platform
import stat
import sys
from pathlib import Path
from typing import Any, Iterable

from .region import RegionError, load_boundary
from .source import (
    FIXTURE_SOURCE_BYTES,
    FIXTURE_SOURCE_SHA256,
    SOURCE_MONTH,
    SourceError,
    load_selected_projection,
    normalize_selected_record,
    sha256_file,
)


TASK_ID = "t_322319de"
BASE_COMMIT = "b3776c98d8834c6df7c281b4e1a3a1ad8542af0d"
SCHEMA_VERSION = 2
MAX_CANDIDATES = 6
CANDIDATE_RADIUS_M = 80.0
MAX_OUTPUT_BYTES = 32 * 1024 * 1024
MAX_REPORT_BYTES = 8 * 1024 * 1024
FIXTURE_DIR = Path("/opt/data/workspaces/graduation-research-evidence/unified-work/G01/fixtures")
FIXTURE_SOURCE_PATH = FIXTURE_DIR / "selected-fields.jsonl"
FIXTURE_CONTRACT_PATH = FIXTURE_DIR / "fixture-manifest.json"
P05_FEATURES_PATH = Path(
    "/opt/data/workspaces/graduation-research-p05/backend/generation/generation/features.py"
)
P05_FEATURES_SHA256 = "0a5b6791c155a185b89bb53b9ba3acf53c54946103b73e3abe1cf32c3a69be1e"
P05_FEATURES_BYTES = 5038
P02_LOCK_PATH = Path(
    "/opt/data/workspaces/graduation-research-evidence/unified-work/P02/uv.lock"
)
P02_LOCK_SHA256 = "6ad0bdaa9a69478dc11652920bb889b60b7d6039546906b4d020385ab87cd1ff"
P02_LOCK_BYTES = 26583
P02_PYTHON_PATH = Path(
    "/opt/data/workspaces/graduation-research-evidence/unified-work/P02/.venv/bin/python"
)
SCHEMA_PATH = Path(__file__).with_name("schema-v2.json")
SCHEMA_SHA256 = "f84a0337925749604475c4c2c3c92bf70f0e25ef458a10eb09875b2ee1be895d"
SCHEMA_BYTES = 3846
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
ATTEMPT_ROOT = Path(
    "/opt/data/workspaces/graduation-research-evidence/unified-work/G02-revision4/validation/attempts"
)
FREEZE_PATH = Path(
    "/opt/data/workspaces/graduation-research-evidence/unified-work/G02/operator-input-freeze.json"
)
ARTIFACT_CONTRACT_PATH = Path(
    "/opt/data/workspaces/graduation-research-evidence/unified-work/G01/artifact-contract.json"
)
ARTIFACT_CONTRACT_SHA256 = "730eb7dd90bd23d4fd7c480411f581fa15435450c7f94d33e4fd87f660353df4"
BOUNDARY_FIXTURE_PATH = Path(__file__).resolve().parents[1] / "tests/fixtures/region-a.geojson"
BOUNDARY_FIXTURE_SHA256 = "068767336c2eb43021d5e5c6608a3c4dfab6d87411484869a59a2199fb3903cd"
BOUNDARY_FIXTURE_BYTES = 312
REAL_BOUNDARY_PATH = Path(
    "/opt/data/workspaces/graduation-research-evidence/unified-work/G01-boundary-prep/final-run54/nagoya-wgs84.geojson"
)
REAL_BOUNDARY_SHA256 = "75e606a1ea19ec6a33c18b3cf4ca4616209d6d1179a835ea8d57424c3adfcb2d"
REAL_BOUNDARY_BYTES = 201344
BOUNDARY_MANIFEST_PATH = REAL_BOUNDARY_PATH.with_name("boundary-manifest.json")
BOUNDARY_MANIFEST_SHA256 = "90b8207bdddc449be7e3b4e0c466eed537e4b6887d06926bb2b317b610430573"
BOUNDARY_MANIFEST_BYTES = 54937
WARD_CROSSCHECK_PATH = REAL_BOUNDARY_PATH.with_name("ward-crosscheck.json")
WARD_CROSSCHECK_SHA256 = "8332dcf004629662799f3fbc07ab8c055ba9ed6e076fd6db8b5316b76437b7ec"
WARD_CROSSCHECK_BYTES = 7745
BOUNDARY_VALIDATION_PATH = REAL_BOUNDARY_PATH.with_name("validation.json")
BOUNDARY_VALIDATION_SHA256 = "c708381eda010afd1d81afcad8395872daa1ad0c7381f674b6fbb2be275ff480"
BOUNDARY_VALIDATION_BYTES = 8054
JARTIC_TERMS_PATH = Path(
    "/opt/data/workspaces/graduation-research-evidence/unified-work/G01-boundary/metadata-run50/jartic-terms.pdf"
)
JARTIC_TERMS_SHA256 = "b1ada2fc58d068ed95b1973932f4882dfb5d49e888f17ded934f777d545cd285"
JARTIC_TERMS_BYTES = 139938
PARENT_REF_PATHS = {
    "selected_source": FIXTURE_SOURCE_PATH,
    "source_contract": FIXTURE_CONTRACT_PATH,
    "operator_input_freeze": FREEZE_PATH,
    "g01_artifact_contract": ARTIFACT_CONTRACT_PATH,
    "synthetic_boundary": BOUNDARY_FIXTURE_PATH,
    "nagoya_boundary": REAL_BOUNDARY_PATH,
    "boundary_manifest": BOUNDARY_MANIFEST_PATH,
    "ward_crosscheck": WARD_CROSSCHECK_PATH,
    "boundary_validation": BOUNDARY_VALIDATION_PATH,
    "jartic_terms": JARTIC_TERMS_PATH,
    "p05_features": P05_FEATURES_PATH,
    "p02_lock": P02_LOCK_PATH,
}
PARENT_REF_EXPECTATIONS = {
    "selected_source": (FIXTURE_SOURCE_BYTES, FIXTURE_SOURCE_SHA256),
    "source_contract": (6490, "900bab5cd47cacbc191a5edd7e9afb709aeca1163146689b08de30269f554c88"),
    "operator_input_freeze": (34262, "0ff34b71b0d4f9934d84b34813c5a7951760a905b21e8ada55dcdc1789ee73c2"),
    "g01_artifact_contract": (42888, ARTIFACT_CONTRACT_SHA256),
    "synthetic_boundary": (BOUNDARY_FIXTURE_BYTES, BOUNDARY_FIXTURE_SHA256),
    "nagoya_boundary": (REAL_BOUNDARY_BYTES, REAL_BOUNDARY_SHA256),
    "boundary_manifest": (BOUNDARY_MANIFEST_BYTES, BOUNDARY_MANIFEST_SHA256),
    "ward_crosscheck": (WARD_CROSSCHECK_BYTES, WARD_CROSSCHECK_SHA256),
    "boundary_validation": (BOUNDARY_VALIDATION_BYTES, BOUNDARY_VALIDATION_SHA256),
    "jartic_terms": (JARTIC_TERMS_BYTES, JARTIC_TERMS_SHA256),
    "p05_features": (P05_FEATURES_BYTES, P05_FEATURES_SHA256),
    "p02_lock": (P02_LOCK_BYTES, P02_LOCK_SHA256),
}
CODE_REF_PATHS = {
    "generation_io_init": PACKAGE_ROOT / "generation_io/__init__.py",
    "generation_io_main": PACKAGE_ROOT / "generation_io/__main__.py",
    "generation_io_artifacts": PACKAGE_ROOT / "generation_io/artifacts.py",
    "generation_io_region": PACKAGE_ROOT / "generation_io/region.py",
    "generation_io_source": PACKAGE_ROOT / "generation_io/source.py",
    "generation_io_supervisor": PACKAGE_ROOT / "generation_io/supervisor.py",
    "schema_v2": SCHEMA_PATH,
    "pyproject": PACKAGE_ROOT / "pyproject.toml",
    "uv_lock": PACKAGE_ROOT / "uv.lock",
    "p05_features": P05_FEATURES_PATH,
}
EXPECTED_RUNTIME_PACKAGES = {
    "numpy": "2.3.5",
    "pandas": "2.3.3",
    "scipy": "1.16.3",
}
KERNEL_COLUMNS = (
    "candidate_rank",
    "candidate_distance_m",
    "candidate_bearing",
    "p_d1",
    "p_d2",
    "p_d3",
    "p_gap12",
    "p_ratio12",
    "q_d1",
    "q_d2",
    "reciprocal_rank",
    "mutual_nearest",
    "min_axis_angle_other",
    "min_directed_angle_other",
    "third_midpoint_distance_m",
    "bearing_mod_45",
    "bearing_mod_90",
    *(
        column
        for radius in (12, 15, 18, 20, 25, 30, 40, 60, 100)
        for column in (f"p_n{radius}", f"q_n{radius}")
    ),
)
KERNEL_BASE_COLUMNS = (
    "point_index",
    "neighbor_index",
    "record_id",
    "uniqueKey",
    "lon",
    "lat",
    *KERNEL_COLUMNS,
)
CANDIDATE_PROVENANCE_COLUMNS = (
    "entity_id",
    "candidate_entity_id",
    "source_row",
    "candidate_source_row",
    "source_month",
    "parent_normalized_sha256",
    "bearing_semantics",
    "production_approved",
)
CANDIDATE_SCHEMA = (*KERNEL_BASE_COLUMNS, *CANDIDATE_PROVENANCE_COLUMNS)
OUTPUT_NAMES = (
    "normalized-points.jsonl",
    "candidate-features.jsonl",
    "rejected-records.jsonl",
    "source-manifest.json",
)


class ArtifactError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def jsonl_bytes(records: Iterable[dict[str, Any]]) -> bytes:
    try:
        return b"".join(
            (
                json.dumps(
                    _json_value(record),
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
            for record in records
        )
    except ArtifactError:
        raise
    except (TypeError, ValueError, OverflowError) as exc:
        raise ArtifactError("invalid_json_value") from exc


def candidate_feature_rows(
    normalized_records: list[dict[str, Any]], parent_normalized_sha256: str
) -> list[dict[str, Any]]:
    if len(normalized_records) < 2:
        return []
    if not _is_sha256(parent_normalized_sha256):
        raise ArtifactError("invalid_normalized_parent_hash")
    try:
        import numpy as np
        import pandas as pd

        kernel = _load_p05_kernel()
        points = pd.DataFrame(
            {
                "lon": np.asarray([row["lon"] for row in normalized_records], dtype=np.float64),
                "lat": np.asarray([row["lat"] for row in normalized_records], dtype=np.float64),
                "jartic_record_id": pd.Series(
                    [row["jartic_record_id"] for row in normalized_records], dtype=object
                ),
                "ユニークキー": pd.Series(
                    [row["ユニークキー"] for row in normalized_records], dtype=object
                ),
            }
        )
        snapshot = points.copy(deep=True)
        features = kernel.build_candidate_features(
            points, max_candidates=MAX_CANDIDATES, candidate_radius=CANDIDATE_RADIUS_M
        )
        pd.testing.assert_frame_equal(points, snapshot, check_exact=True)
        if features.empty:
            return []
    except ArtifactError:
        raise
    except Exception as exc:
        raise ArtifactError("candidate_kernel_failed") from exc

    expected_columns = list(KERNEL_BASE_COLUMNS)
    if list(features.columns) != expected_columns:
        raise ArtifactError("candidate_kernel_schema_mismatch")
    result: list[dict[str, Any]] = []
    for feature_row in features.to_dict(orient="records"):
        point_index = _index_value(feature_row["point_index"], len(normalized_records))
        neighbor_index = _index_value(feature_row["neighbor_index"], len(normalized_records))
        point = normalized_records[point_index]
        neighbor = normalized_records[neighbor_index]
        if (
            str(feature_row["record_id"]) != point["jartic_record_id"]
            or str(feature_row["uniqueKey"]) != point["ユニークキー"]
            or float(feature_row["lon"]).hex() != float(point["lon"]).hex()
            or float(feature_row["lat"]).hex() != float(point["lat"]).hex()
        ):
            raise ArtifactError("candidate_provenance_join_mismatch")
        row = {column: feature_row[column] for column in KERNEL_BASE_COLUMNS}
        row.update(
            {
                "entity_id": point["entity_id"],
                "candidate_entity_id": neighbor["entity_id"],
                "source_row": point["source_row"],
                "candidate_source_row": neighbor["source_row"],
                "source_month": point["source_month"],
                "parent_normalized_sha256": parent_normalized_sha256,
                "bearing_semantics": "candidate_point_neighbor_only",
                "production_approved": False,
            }
        )
        result.append(row)
    return result


def generate_candidate_artifact(
    *,
    source: str | Path,
    source_contract: str | Path,
    input_kind: str,
    boundary: str | Path,
    region_id: str,
    output: str | Path,
    ownership_receipt: str | Path | None = None,
    invocation_id: str | None = None,
) -> Path:
    if input_kind != "selected_projection":
        raise ArtifactError("unsupported_input_kind")
    if region_id != "fixture:region-a":
        raise ArtifactError("fixture_region_id_required")
    source_path = _absolute_path(source)
    contract_path = _absolute_path(source_contract)
    output_path = _absolute_path(output)
    _reject_symlink_ancestors(output_path.parent)
    if not output_path.parent.is_dir():
        raise ArtifactError("output_parent_missing")
    if os.path.lexists(output_path):
        raise ArtifactError("output_exists")
    if (ownership_receipt is None) != (invocation_id is None):
        raise ArtifactError("invalid_ownership_receipt_arguments")
    receipt_path = _ownership_receipt_path(ownership_receipt) if ownership_receipt else None
    if receipt_path is not None and (not invocation_id or len(invocation_id) > 128):
        raise ArtifactError("invalid_ownership_receipt_arguments")
    lock_path = output_path.with_name(f".{output_path.name}.lock")
    lock_fd = _acquire_lock(lock_path)
    created = False
    owned_files: dict[str, tuple[int, int]] = {}
    try:
        output_path.mkdir(mode=0o700)
        created = True
        if receipt_path is not None:
            _write_ownership_receipt(output_path, receipt_path, invocation_id or "")
        try:
            if source_path.is_file():
                source_bytes, source_hash = sha256_file(source_path)
                if source_bytes != FIXTURE_SOURCE_BYTES or source_hash != FIXTURE_SOURCE_SHA256:
                    raise ArtifactError("source_hash_mismatch")
            if source_path != FIXTURE_SOURCE_PATH or contract_path != FIXTURE_CONTRACT_PATH:
                raise ArtifactError("fixed_selected_projection_required")
            manifest = _build_and_publish(
                source=_absolute_path(source),
                source_contract=_absolute_path(source_contract),
                boundary=_absolute_path(boundary),
                region_id=region_id,
                output=output_path,
                owned_files=owned_files,
            )
            if receipt_path is not None:
                _write_published_files_receipt(output_path, receipt_path, invocation_id or "")
            return output_path / "generation-manifest.json"
        except BaseException as exc:
            code = exc.code if isinstance(exc, (ArtifactError, SourceError, RegionError)) else "generation_failed"
            if created and _cleanup_output(output_path, owned_files):
                _write_exclusive(
                    output_path / "failure-report.json",
                    _json_document_bytes(
                        {
                            "schema_version": SCHEMA_VERSION,
                            "status": "failed",
                            "error_code": code,
                            "complete_manifest_published": False,
                        }
                    ),
                )
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise ArtifactError(code) from exc
            raise ArtifactError(code) from exc
    finally:
        os.close(lock_fd)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def resume_candidate_artifact(*, manifest: str | Path, output: str | Path) -> Path:
    previous_manifest_path = _absolute_path(manifest)
    previous = verify_generation_manifest(previous_manifest_path)
    if previous.get("input_kind") != "selected_projection_fixture_not_full_raw_csv":
        raise ArtifactError("resume_input_kind_mismatch")
    if previous.get("boundary", {}).get("synthetic_fixture") is not True:
        raise ArtifactError("resume_boundary_contract_mismatch")
    if previous.get("parameters", {}).get("max_candidates") != MAX_CANDIDATES or previous.get(
        "parameters", {}
    ).get("candidate_radius_m") != CANDIDATE_RADIUS_M:
        raise ArtifactError("resume_parameter_mismatch")
    _validate_fixed_runtime_refs()

    previous_root = previous_manifest_path.parent
    normalized_path = previous_root / "normalized-points.jsonl"
    rejected_path = previous_root / "rejected-records.jsonl"
    source_manifest_path = previous_root / "source-manifest.json"
    normalized_bytes = normalized_path.read_bytes()
    rejected_bytes = rejected_path.read_bytes()
    source_manifest_bytes = source_manifest_path.read_bytes()
    normalized_records = _parse_jsonl(normalized_bytes)
    rejected_records = _parse_jsonl(rejected_bytes)
    if len(normalized_records) != previous["counts"]["normalized_points"]:
        raise ArtifactError("resume_normalized_count_mismatch")
    if len(rejected_records) != previous["counts"]["rejected_stop"]:
        raise ArtifactError("resume_rejected_count_mismatch")
    expected_schema = previous.get("schemas", {}).get("normalized_points")
    if not isinstance(expected_schema, list) or any(
        not isinstance(record, dict) or list(record) != expected_schema
        for record in normalized_records
    ):
        raise ArtifactError("resume_normalized_schema_mismatch")
    for record in normalized_records:
        if record.get("production_approved") is not False or record.get(
            "normalization_status"
        ) != "valid_candidate_only":
            raise ArtifactError("resume_normalized_status_mismatch")
    normalized_hash = hashlib.sha256(normalized_bytes).hexdigest()
    if normalized_hash != previous["outputs"]["normalized-points.jsonl"]["sha256"]:
        raise ArtifactError("output_hash_mismatch")
    candidates = candidate_feature_rows(normalized_records, normalized_hash)
    candidate_bytes = jsonl_bytes(candidates)
    output_path = _absolute_path(output)
    _reject_symlink_ancestors(output_path.parent)
    if not output_path.parent.is_dir():
        raise ArtifactError("output_parent_missing")
    if os.path.lexists(output_path):
        raise ArtifactError("output_exists")
    lock_path = output_path.with_name(f".{output_path.name}.lock")
    lock_fd = _acquire_lock(lock_path)
    created = False
    owned_files: dict[str, tuple[int, int]] = {}
    try:
        output_path.mkdir(mode=0o700)
        created = True
        data = {
            "normalized-points.jsonl": normalized_bytes,
            "candidate-features.jsonl": candidate_bytes,
            "rejected-records.jsonl": rejected_bytes,
            "source-manifest.json": source_manifest_bytes,
        }
        if sum(map(len, data.values())) > MAX_OUTPUT_BYTES:
            raise ArtifactError("output_budget_exceeded")
        for name, contents in data.items():
            _atomic_publish(output_path / name, contents)
            _record_owned_file(output_path, name, owned_files)
        resumed = dict(previous)
        counts = dict(previous["counts"])
        counts["candidate_rows"] = len(candidates)
        resumed["counts"] = counts
        resumed["candidate_rows"] = len(candidates)
        resumed["outputs"] = {
            name: {
                "bytes": len(contents),
                "sha256": hashlib.sha256(contents).hexdigest(),
                "rows": (
                    len(normalized_records)
                    if name == "normalized-points.jsonl"
                    else len(candidates)
                    if name == "candidate-features.jsonl"
                    else len(rejected_records)
                    if name == "rejected-records.jsonl"
                    else 1
                ),
            }
            for name, contents in data.items()
        }
        resumed["resume"] = {
            "stage": "whole_region_kernel_only",
            "source_generation_manifest": str(previous_manifest_path),
            "source_generation_manifest_ref": _ref(previous_manifest_path),
            "source_generation_manifest_sha256": hashlib.sha256(
                previous_manifest_path.read_bytes()
            ).hexdigest(),
            "normalized_points_sha256": normalized_hash,
            "raw_scan_resumed": False,
            "region_clip_recomputed": False,
            "fresh_output_path": str(output_path),
        }
        resumed["parent_refs"] = previous["parent_refs"]
        draft_path = output_path / ".generation-manifest.draft.json"
        _atomic_publish(draft_path, _json_document_bytes(resumed))
        _record_owned_file(output_path, draft_path.name, owned_files)
        verify_generation_manifest(draft_path)
        final_path = output_path / "generation-manifest.json"
        if os.path.lexists(final_path):
            raise ArtifactError("complete_manifest_collision")
        os.replace(draft_path, final_path)
        owned_files.pop(draft_path.name, None)
        _record_owned_file(output_path, final_path.name, owned_files)
        return output_path / "generation-manifest.json"
    except BaseException as exc:
        code = exc.code if isinstance(exc, ArtifactError) else "resume_failed"
        if created:
            cleaned = _cleanup_output(output_path, owned_files)
            if cleaned:
                _atomic_publish(
                    output_path / "failure-report.json",
                    _json_document_bytes(
                        {
                            "schema_version": 1,
                            "status": "failed",
                            "error_code": code,
                            "complete_manifest_published": False,
                        }
                    ),
                )
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise ArtifactError(code) from exc
        raise ArtifactError(code) from exc
    finally:
        os.close(lock_fd)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def verify_generation_manifest(manifest_path: str | Path) -> dict[str, Any]:
    path = Path(manifest_path)
    _require_regular(path, "manifest_not_regular")
    manifest = _load_json_object(path, "invalid_generation_manifest")
    return _verify_manifest_document(manifest, path.parent)


def _verify_manifest_document(manifest: dict[str, Any], root: Path) -> dict[str, Any]:
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ArtifactError("unsupported_manifest_schema")
    _fixed_ref(SCHEMA_PATH, SCHEMA_SHA256, SCHEMA_BYTES)
    schema = _load_json_object(SCHEMA_PATH, "invalid_manifest_schema")
    required = set(schema.get("manifest_required_fields", []))
    optional_fields = schema.get("manifest_optional_fields")
    if (
        not required
        or not isinstance(optional_fields, list)
        or any(not isinstance(name, str) for name in optional_fields)
        or len(optional_fields) != len(set(optional_fields))
        or required.intersection(optional_fields)
        or not required.issubset(manifest)
    ):
        raise ArtifactError("manifest_required_field_missing")
    if set(manifest) - required - set(optional_fields):
        raise ArtifactError("unknown_manifest_field")
    if manifest.get("task_id") != TASK_ID or manifest.get("base_commit") != BASE_COMMIT:
        raise ArtifactError("manifest_identity_mismatch")
    if manifest.get("status") != schema["status"] or manifest.get("input_kind") != schema[
        "input_kind"
    ]:
        raise ArtifactError("manifest_status_or_input_kind_mismatch")
    if manifest.get("production_approved") is not False:
        raise ArtifactError("production_approval_must_remain_false")
    if manifest.get("source_active_status") != "unknown":
        raise ArtifactError("source_active_status_must_remain_unknown")
    if manifest.get("identity_scope") != schema["guards"]["identity_scope"]:
        raise ArtifactError("identity_scope_mismatch")

    schema_ref = {
        "version": SCHEMA_VERSION,
        "path": str(SCHEMA_PATH),
        "bytes": SCHEMA_BYTES,
        "sha256": SCHEMA_SHA256,
    }
    if manifest.get("schema") != schema_ref:
        raise ArtifactError("manifest_schema_ref_mismatch")
    if schema.get("schema_version") != SCHEMA_VERSION:
        raise ArtifactError("invalid_manifest_schema")
    if schema.get("parent_ref_roles") != list(PARENT_REF_PATHS) or schema.get(
        "code_ref_roles"
    ) != list(CODE_REF_PATHS):
        raise ArtifactError("invalid_manifest_schema_roles")
    expected_schemas = schema.get("schemas")
    if not isinstance(expected_schemas, dict) or manifest.get("schemas") != expected_schemas:
        raise ArtifactError("manifest_schema_mismatch")
    _verify_ref_map(
        manifest.get("parent_refs"),
        PARENT_REF_PATHS,
        PARENT_REF_EXPECTATIONS,
        "parent",
    )
    _verify_code_refs(manifest.get("code_refs"))
    if manifest.get("runtime") != _runtime_info():
        raise ArtifactError("runtime_mismatch")

    counts = manifest.get("counts")
    if not isinstance(counts, dict) or set(counts) != set(schema["count_fields"]):
        raise ArtifactError("manifest_counts_missing")
    if any(not _is_nonnegative_int(counts.get(name)) for name in schema["count_fields"]):
        raise ArtifactError("invalid_manifest_count")
    source_rows = counts["source_rows"]
    if source_rows != 64 or source_rows != sum(
        counts[name]
        for name in ("excluded_non_stop", "rejected_stop", "outside_region", "normalized_points")
    ):
        raise ArtifactError("row_count_reconciliation_failed")
    if manifest.get("rows_total") != source_rows or manifest.get("candidate_rows") != counts[
        "candidate_rows"
    ]:
        raise ArtifactError("manifest_count_alias_mismatch")
    expected_parameters = {
        **schema["parameters"],
        "kernel_invocations": 1 if counts["normalized_points"] >= 2 else 0,
    }
    if manifest.get("parameters") != expected_parameters:
        raise ArtifactError("manifest_parameter_mismatch")

    expected_boundary = {
        **_fixed_ref(BOUNDARY_FIXTURE_PATH, BOUNDARY_FIXTURE_SHA256, BOUNDARY_FIXTURE_BYTES),
        "region_id": "fixture:region-a",
        "synthetic_fixture": True,
        "used_for_candidate_clip": True,
        "represents_nagoya_administrative_boundary": False,
    }
    if manifest.get("boundary") != expected_boundary:
        raise ArtifactError("boundary_contract_mismatch")
    provenance = manifest.get("provenance")
    if not isinstance(provenance, dict) or (
        provenance.get("source_month") != SOURCE_MONTH
        or provenance.get("version") != "k_2.1"
        or provenance.get("candidate_production_approved") is not False
        or provenance.get("jartic_distribution_approved") is not False
    ):
        raise ArtifactError("manifest_provenance_mismatch")

    outputs = manifest.get("outputs")
    if not isinstance(outputs, dict) or set(outputs) != set(OUTPUT_NAMES):
        raise ArtifactError("manifest_outputs_missing")
    parsed_jsonl: dict[str, list[Any]] = {}
    for name, entry in outputs.items():
        if Path(name).name != name or name not in OUTPUT_NAMES or not isinstance(entry, dict):
            raise ArtifactError("invalid_output_reference")
        if set(entry) != {"bytes", "sha256", "rows"}:
            raise ArtifactError("invalid_output_reference")
        if not _is_nonnegative_int(entry.get("rows")):
            raise ArtifactError("invalid_output_row_count")
        file_path = root / name
        _require_regular(file_path, "output_not_regular")
        size, digest = sha256_file(file_path)
        if size != entry.get("bytes") or digest != entry.get("sha256"):
            raise ArtifactError("output_hash_mismatch")
        body = file_path.read_bytes()
        if name.endswith(".jsonl"):
            records = _parse_jsonl(body)
            if not _is_nonnegative_int(entry.get("rows")) or len(records) != entry.get("rows"):
                raise ArtifactError("output_row_count_mismatch")
            schema_key = {
                "normalized-points.jsonl": "normalized_points",
                "candidate-features.jsonl": "candidate_features",
                "rejected-records.jsonl": "rejected_records",
            }[name]
            if any(
                not isinstance(record, dict)
                or list(record) != expected_schemas[schema_key]
                for record in records
            ):
                raise ArtifactError("output_schema_mismatch")
            parsed_jsonl[name] = records
        elif name == "source-manifest.json":
            source_manifest = _load_json_object(file_path, "invalid_source_manifest")
            if (
                source_manifest.get("schema_version") != 1
                or source_manifest.get("input_kind") != schema["input_kind"]
                or source_manifest.get("source_month") != SOURCE_MONTH
                or source_manifest.get("format_version") != "k_2.1"
                or source_manifest.get("selected_rows") != 64
                or source_manifest.get("selected_file")
                != _fixed_ref(
                    FIXTURE_SOURCE_PATH,
                    FIXTURE_SOURCE_SHA256,
                    FIXTURE_SOURCE_BYTES,
                )
                or source_manifest.get("selected_fixture_contract")
                != _fixed_ref(
                    FIXTURE_CONTRACT_PATH,
                    PARENT_REF_EXPECTATIONS["source_contract"][1],
                    PARENT_REF_EXPECTATIONS["source_contract"][0],
                )
                or source_manifest.get("source_identity", {}).get("source_active_status")
                != "unknown"
                or source_manifest.get("production_approved") is not False
                or source_manifest.get("original_csv", {}).get(
                    "complete_raw_csv_in_this_run"
                ) is not False
                or outputs[name]["rows"] != 1
            ):
                raise ArtifactError("invalid_source_manifest")

    expected_rows = {
        "normalized-points.jsonl": counts["normalized_points"],
        "candidate-features.jsonl": counts["candidate_rows"],
        "rejected-records.jsonl": counts["rejected_stop"],
    }
    if outputs["normalized-points.jsonl"]["rows"] != expected_rows[
        "normalized-points.jsonl"
    ]:
        raise ArtifactError("normalized_count_reconciliation_failed")
    if outputs["candidate-features.jsonl"]["rows"] != expected_rows[
        "candidate-features.jsonl"
    ]:
        raise ArtifactError("candidate_count_reconciliation_failed")
    if outputs["rejected-records.jsonl"]["rows"] != expected_rows[
        "rejected-records.jsonl"
    ]:
        raise ArtifactError("rejected_count_reconciliation_failed")
    for record in parsed_jsonl["normalized-points.jsonl"]:
        if (
            record.get("production_approved") is not False
            or record.get("normalization_status") != "valid_candidate_only"
            or record.get("source_active_status") != "unknown"
        ):
            raise ArtifactError("normalized_candidate_status_mismatch")
    for record in parsed_jsonl["candidate-features.jsonl"]:
        if (
            record.get("production_approved") is not False
            or record.get("bearing_semantics") != "candidate_point_neighbor_only"
        ):
            raise ArtifactError("candidate_approval_must_remain_false")
    if "resume" in manifest:
        _verify_resume_ref(manifest["resume"], schema, root, outputs)
    return manifest


def _load_json_object(path: Path, error_code: str) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("nonfinite")),
            object_pairs_hook=_unique_object,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ArtifactError(error_code) from exc
    if not isinstance(value, dict):
        raise ArtifactError(error_code)
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _runtime_info() -> dict[str, Any]:
    package_versions = {
        name: importlib.metadata.version(name) for name in EXPECTED_RUNTIME_PACKAGES
    }
    if platform.python_version() != "3.13.5" or package_versions != EXPECTED_RUNTIME_PACKAGES:
        raise ArtifactError("runtime_mismatch")
    if Path(sys.executable).resolve() != P02_PYTHON_PATH.resolve():
        raise ArtifactError("runtime_interpreter_mismatch")
    return {
        "python": {
            "version": platform.python_version(),
            "executable": sys.executable,
            "p02_interpreter": str(P02_PYTHON_PATH),
            "byteorder": sys.byteorder,
            "platform": platform.platform(),
        },
        "packages": package_versions,
        "lock_ref": _fixed_ref(P02_LOCK_PATH, P02_LOCK_SHA256, P02_LOCK_BYTES),
    }


def _verify_ref_map(
    refs: Any,
    paths: dict[str, Path],
    expectations: dict[str, tuple[int, str]],
    label: str,
) -> None:
    if not isinstance(refs, dict) or set(refs) != set(paths):
        raise ArtifactError(f"{label}_reference_roles_mismatch")
    for role, path in paths.items():
        ref = refs[role]
        if not isinstance(ref, dict) or set(ref) != {"path", "bytes", "sha256"}:
            raise ArtifactError(f"invalid_{label}_reference")
        if ref.get("path") != str(path):
            raise ArtifactError(f"{label}_reference_path_mismatch")
        expected_bytes, expected_hash = expectations[role]
        actual = _fixed_ref(path, expected_hash, expected_bytes)
        if ref != actual:
            raise ArtifactError(f"{label}_reference_hash_mismatch")


def _code_refs() -> dict[str, dict[str, Any]]:
    _fixed_ref(SCHEMA_PATH, SCHEMA_SHA256, SCHEMA_BYTES)
    _fixed_ref(P05_FEATURES_PATH, P05_FEATURES_SHA256, P05_FEATURES_BYTES)
    _fixed_ref(P02_LOCK_PATH, P02_LOCK_SHA256, P02_LOCK_BYTES)
    return {role: _ref(path) for role, path in CODE_REF_PATHS.items()}


def _verify_code_refs(refs: Any) -> None:
    if not isinstance(refs, dict) or set(refs) != set(CODE_REF_PATHS):
        raise ArtifactError("code_reference_roles_mismatch")
    if refs != _code_refs():
        raise ArtifactError("code_reference_mismatch")


def _verify_resume_ref(
    resume: Any,
    schema: dict[str, Any],
    root: Path,
    outputs: dict[str, Any],
) -> None:
    required_fields = schema.get("resume_required_fields")
    contract = schema.get("resume_contract")
    if (
        not isinstance(required_fields, list)
        or any(not isinstance(name, str) for name in required_fields)
        or len(required_fields) != len(set(required_fields))
        or not isinstance(contract, dict)
        or set(contract) != {"stage", "raw_scan_resumed", "region_clip_recomputed"}
        or not isinstance(resume, dict)
        or set(resume) != set(required_fields)
    ):
        raise ArtifactError("invalid_resume_metadata")
    if (
        resume.get("stage") != contract["stage"]
        or resume.get("raw_scan_resumed") is not contract["raw_scan_resumed"]
        or resume.get("region_clip_recomputed")
        is not contract["region_clip_recomputed"]
        or not _is_sha256(resume.get("normalized_points_sha256", ""))
    ):
        raise ArtifactError("invalid_resume_metadata")
    source_path = Path(resume["source_generation_manifest"])
    source_ref = _ref(source_path)
    if (
        resume["source_generation_manifest_ref"] != source_ref
        or resume["source_generation_manifest_sha256"] != source_ref["sha256"]
    ):
        raise ArtifactError("resume_source_manifest_ref_mismatch")
    source_manifest = verify_generation_manifest(source_path)
    if (
        source_manifest["outputs"]["normalized-points.jsonl"]["sha256"]
        != resume["normalized_points_sha256"]
        or outputs["normalized-points.jsonl"]["sha256"]
        != resume["normalized_points_sha256"]
    ):
        raise ArtifactError("resume_normalized_hash_mismatch")
    try:
        if Path(resume["fresh_output_path"]).resolve(strict=True) != root.resolve(strict=True):
            raise ArtifactError("resume_output_path_mismatch")
    except (OSError, RuntimeError) as exc:
        raise ArtifactError("resume_output_path_mismatch") from exc


def _build_and_publish(
    *,
    source: Path,
    source_contract: Path,
    boundary: Path,
    region_id: str,
    output: Path,
    owned_files: dict[str, tuple[int, int]],
) -> dict[str, Any]:
    source_manifest, source_rows = load_selected_projection(source, source_contract)
    boundary_ref = _validate_fixture_boundary(boundary, region_id)
    boundary_shape = load_boundary(boundary)
    _validate_fixed_runtime_refs()
    schema_document = _load_json_object(SCHEMA_PATH, "invalid_manifest_schema")

    normalized: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    excluded_non_stop = 0
    outside_region = 0
    for item in source_rows:
        result = normalize_selected_record(
            item["selected"],
            source_row=item["source_row"],
            source_month=source_manifest["source_month"],
            region_id=region_id,
            boundary=boundary_shape,
            selected_hash=item["selected_utf8_sha256"],
        )
        if result["classification"] == "excluded_non_stop":
            excluded_non_stop += 1
        elif result["classification"] == "rejected":
            rejected.append(
                {
                    "raw_uid": result["raw_uid"],
                    "source_row": result["source_row"],
                    "reason": result["reason"],
                    "selected_field_sha256": result["selected_field_sha256"],
                }
            )
        elif result["classification"] == "outside_region":
            outside_region += 1
        elif result["classification"] == "normalized":
            normalized.append(result["record"])
        else:
            raise ArtifactError("unknown_source_classification")
    if len(source_rows) != excluded_non_stop + len(rejected) + outside_region + len(normalized):
        raise ArtifactError("row_count_reconciliation_failed")

    normalized_bytes = jsonl_bytes(normalized)
    normalized_hash = hashlib.sha256(normalized_bytes).hexdigest()
    candidates = candidate_feature_rows(normalized, normalized_hash)
    rejected_bytes = jsonl_bytes(rejected)
    candidate_bytes = jsonl_bytes(candidates)
    source_manifest_bytes = _json_document_bytes(
        _source_manifest(source_manifest, source, source_contract, boundary_ref, region_id)
    )
    data = {
        "normalized-points.jsonl": normalized_bytes,
        "candidate-features.jsonl": candidate_bytes,
        "rejected-records.jsonl": rejected_bytes,
        "source-manifest.json": source_manifest_bytes,
    }
    total_bytes = sum(map(len, data.values()))
    if total_bytes > MAX_OUTPUT_BYTES:
        raise ArtifactError("output_budget_exceeded")
    if len(source_manifest_bytes) > MAX_REPORT_BYTES:
        raise ArtifactError("report_budget_exceeded")
    for name, contents in data.items():
        _atomic_publish(output / name, contents)
        _record_owned_file(output, name, owned_files)

    counts = {
        "source_rows": len(source_rows),
        "excluded_non_stop": excluded_non_stop,
        "rejected_stop": len(rejected),
        "outside_region": outside_region,
        "normalized_points": len(normalized),
        "candidate_rows": len(candidates),
    }
    outputs = {
        name: {
            "bytes": len(contents),
            "sha256": hashlib.sha256(contents).hexdigest(),
            "rows": (len(normalized) if name == "normalized-points.jsonl" else
                     len(candidates) if name == "candidate-features.jsonl" else
                     len(rejected) if name == "rejected-records.jsonl" else 1),
        }
        for name, contents in data.items()
    }
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "task_id": TASK_ID,
        "base_commit": BASE_COMMIT,
        "status": "complete_candidate_artifact_not_quality_approved",
        "input_kind": "selected_projection_fixture_not_full_raw_csv",
        "counts": counts,
        "rows_total": len(source_rows),
        "candidate_rows": len(candidates),
        "production_approved": False,
        "source_active_status": "unknown",
        "identity_scope": "(prefecture, source_month, raw_uid); month-local; no cross-month upsert/delete",
        "parameters": {
            "max_candidates": MAX_CANDIDATES,
            "candidate_radius_m": CANDIDATE_RADIUS_M,
            "kernel_invocations": 1 if len(normalized) >= 2 else 0,
            "chunked_kernel_invocations": False,
        },
        "boundary": {
            **boundary_ref,
            "region_id": region_id,
            "synthetic_fixture": True,
            "used_for_candidate_clip": True,
            "represents_nagoya_administrative_boundary": False,
        },
        "provenance": {
            "source_month": SOURCE_MONTH,
            "version": "k_2.1",
            "original_csv_sha256": source_manifest["original_csv_sha256"],
            "original_csv_bytes": source_manifest["original_csv_bytes"],
            "raw_update_semantics": "unknown; empty observed in selected fixture",
            "candidate_production_approved": False,
            "jartic_distribution_approved": False,
            "boundary_crs_transform_claim": "not performed by this synthetic fixture run",
        },
        "schemas": schema_document["schemas"],
        "schema": {
            "version": SCHEMA_VERSION,
            "path": str(SCHEMA_PATH),
            "bytes": SCHEMA_BYTES,
            "sha256": SCHEMA_SHA256,
        },
        "parent_refs": _parent_refs(source, source_contract, boundary),
        "code_refs": _code_refs(),
        "runtime": _runtime_info(),
        "outputs": outputs,
        "excluded_claims": [
            "full original TypeD CSV parsing or full-month generation",
            "Nagoya administrative clipping or completeness",
            "source activity/current validity",
            "continuous cross-month identity or database compatibility",
            "candidate bearing as legal traffic direction or sign orientation",
            "quality allowlist, safety notification, database import, publication or production approval",
            "JARTIC or boundary redistribution permission",
        ],
    }
    draft_path = output / ".generation-manifest.draft.json"
    _atomic_publish(draft_path, _json_document_bytes(manifest))
    _record_owned_file(output, draft_path.name, owned_files)
    verify_generation_manifest(draft_path)
    complete_path = output / "generation-manifest.json"
    if os.path.lexists(complete_path):
        raise ArtifactError("complete_manifest_collision")
    os.replace(draft_path, complete_path)
    owned_files.pop(draft_path.name, None)
    _record_owned_file(output, complete_path.name, owned_files)
    return manifest


def _source_manifest(
    contract: dict[str, Any], source: Path, source_contract: Path, boundary_ref: dict[str, Any], region_id: str
) -> dict[str, Any]:
    source_size, source_hash = sha256_file(source)
    contract_size, contract_hash = sha256_file(source_contract)
    return {
        "schema_version": 1,
        "input_kind": "selected_projection_fixture_not_full_raw_csv",
        "source_month": SOURCE_MONTH,
        "format_version": "k_2.1",
        "projection_columns": contract["projection_columns"],
        "selected_rows": contract["rows"],
        "selected_file": {"path": str(source), "bytes": source_size, "sha256": source_hash},
        "selected_fixture_contract": {"path": str(source_contract), "bytes": contract_size, "sha256": contract_hash},
        "source_observation": contract["source_observation"],
        "original_csv": {
            "sha256": contract["original_csv_sha256"],
            "bytes": contract["original_csv_bytes"],
            "crc32": "6f7ba6eb",
            "encoding_observed": "CP932 strict",
            "record_count": 551863,
            "complete_raw_csv_in_this_run": False,
        },
        "source_identity": {
            "raw_uid_is_literal": True,
            "identity_scope": "(prefecture, source_month, raw_uid)",
            "source_active_status": "unknown",
            "update_semantics": "unknown",
        },
        "boundary_used": {**boundary_ref, "region_id": region_id},
        "synthetic_carrier_note": "selected-field projection is not a complete 170-column raw CSV sample; omitted fields were not reconstructed here",
        "production_approved": False,
    }


def _parent_refs(source: Path, source_contract: Path, boundary: Path) -> dict[str, dict[str, Any]]:
    if source != FIXTURE_SOURCE_PATH or source_contract != FIXTURE_CONTRACT_PATH:
        raise ArtifactError("fixed_selected_projection_required")
    if boundary.resolve(strict=True) != BOUNDARY_FIXTURE_PATH.resolve(strict=True):
        raise ArtifactError("synthetic_fixture_boundary_required")
    return {
        role: _fixed_ref(
            path,
            PARENT_REF_EXPECTATIONS[role][1],
            PARENT_REF_EXPECTATIONS[role][0],
        )
        for role, path in PARENT_REF_PATHS.items()
    }


def _validate_fixture_boundary(path: Path, region_id: str) -> dict[str, Any]:
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ArtifactError("boundary_fixture_missing") from exc
    if resolved != BOUNDARY_FIXTURE_PATH.resolve(strict=True):
        raise ArtifactError("synthetic_fixture_boundary_required")
    size, digest = sha256_file(path)
    if size != BOUNDARY_FIXTURE_BYTES or digest != BOUNDARY_FIXTURE_SHA256:
        raise ArtifactError("boundary_hash_mismatch")
    return {
        "path": str(path),
        "bytes": size,
        "sha256": digest,
        "region_id": region_id,
        "synthetic_fixture": True,
    }


def _validate_fixed_runtime_refs() -> None:
    _fixed_ref(P05_FEATURES_PATH, P05_FEATURES_SHA256, P05_FEATURES_BYTES)
    _fixed_ref(P02_LOCK_PATH, P02_LOCK_SHA256, P02_LOCK_BYTES)


def _load_p05_kernel():
    _fixed_ref(P05_FEATURES_PATH, P05_FEATURES_SHA256, P05_FEATURES_BYTES)
    spec = importlib.util.spec_from_file_location("_g02_frozen_p05_features", P05_FEATURES_PATH)
    if spec is None or spec.loader is None:
        raise ArtifactError("kernel_import_failed")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise ArtifactError("kernel_import_failed") from exc
    return module


def _ref(path: Path) -> dict[str, Any]:
    size, digest = sha256_file(path)
    return {"path": str(path), "bytes": size, "sha256": digest}


def _record_owned_file(
    output: Path, name: str, owned_files: dict[str, tuple[int, int]]
) -> None:
    path = output / name
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ArtifactError("owned_output_missing") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise ArtifactError("owned_output_not_regular")
    owned_files[name] = (metadata.st_dev, metadata.st_ino)


def _fixed_ref(path: Path, expected_sha256: str, expected_bytes: int) -> dict[str, Any]:
    ref = _ref(path)
    if ref["bytes"] != expected_bytes or ref["sha256"] != expected_sha256:
        raise ArtifactError("frozen_parent_hash_mismatch")
    return ref


def _json_document_bytes(document: dict[str, Any]) -> bytes:
    try:
        return (json.dumps(document, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise ArtifactError("invalid_manifest_value") from exc


def _json_value(value: Any) -> Any:
    try:
        import numpy as np
    except ImportError:
        np = None
    if np is not None and isinstance(value, np.generic):
        return _json_value(value.item())
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise ArtifactError("non_finite_json_value")
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise ArtifactError("invalid_json_value")


def _index_value(value: Any, count: int) -> int:
    try:
        index = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ArtifactError("invalid_candidate_index") from exc
    if isinstance(value, bool) or float(value) != index or not 0 <= index < count:
        raise ArtifactError("invalid_candidate_index")
    return index


def _parse_jsonl(contents: bytes) -> list[Any]:
    if contents and not contents.endswith(b"\n"):
        raise ArtifactError("invalid_jsonl_ending")
    rows: list[Any] = []
    for line in contents.splitlines():
        try:
            record = json.loads(
                line,
                parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("nonfinite")),
            )
        except (json.JSONDecodeError, ValueError) as exc:
            raise ArtifactError("invalid_jsonl_record") from exc
        _assert_finite_tree(record)
        rows.append(record)
    return rows


def _assert_finite_tree(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ArtifactError("non_finite_json_value")
    if isinstance(value, dict):
        for item in value.values():
            _assert_finite_tree(item)
    elif isinstance(value, list):
        for item in value:
            _assert_finite_tree(item)


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _is_nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _absolute_path(value: str | Path) -> Path:
    return Path(os.path.abspath(os.fspath(value)))


def _reject_symlink_ancestors(path: Path) -> None:
    current = path
    while current != current.parent:
        if current.exists() and current.is_symlink():
            raise ArtifactError("output_parent_symlink")
        current = current.parent


def _acquire_lock(lock_path: Path) -> int:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        return os.open(lock_path, flags, 0o600)
    except FileExistsError as exc:
        raise ArtifactError("output_lock_exists") from exc
    except OSError as exc:
        raise ArtifactError("output_lock_failed") from exc


def _atomic_publish(path: Path, contents: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(temporary, flags, 0o600)
    except OSError as exc:
        raise ArtifactError("temporary_output_collision") from exc
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _write_exclusive(path: Path, contents: bytes) -> None:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise ArtifactError("failure_report_collision") from exc
    with os.fdopen(fd, "wb") as stream:
        stream.write(contents)
        stream.flush()
        os.fsync(stream.fileno())


def _cleanup_output(output: Path, owned_files: dict[str, tuple[int, int]]) -> bool:
    if output.is_symlink() or not output.is_dir():
        return False
    safe = True
    for name, identity in tuple(owned_files.items()):
        if Path(name).name != name:
            safe = False
            continue
        path = output / name
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            safe = False
            continue
        if not stat.S_ISREG(metadata.st_mode) or (metadata.st_dev, metadata.st_ino) != identity:
            safe = False
            continue
        path.unlink()
    return safe


def _require_regular(path: Path, error_code: str) -> None:
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise ArtifactError(error_code) from exc
    if not stat.S_ISREG(mode):
        raise ArtifactError(error_code)


def _ownership_receipt_path(value: str | Path) -> Path:
    path = _absolute_path(value)
    _reject_symlink_ancestors(path.parent)
    if (
        path.name != "ownership.json"
        or path.parent.parent != ATTEMPT_ROOT
        or not path.parent.name.startswith("attempt-")
        or len(path.parent.name) != len("attempt-") + 32
        or any(character not in "0123456789abcdef" for character in path.parent.name.removeprefix("attempt-"))
        or not path.parent.is_dir()
        or path.parent.is_symlink()
        or os.path.lexists(path)
        or os.path.lexists(path.parent / "published-files.json")
    ):
        raise ArtifactError("invalid_ownership_receipt_path")
    return path


def _write_ownership_receipt(output: Path, receipt_path: Path, invocation_id: str) -> None:
    metadata = output.stat(follow_symlinks=False)
    receipt = {
        "receipt_version": 1,
        "invocation_id": invocation_id,
        "output_path": str(output.resolve(strict=True)),
        "root_st_dev": metadata.st_dev,
        "root_st_ino": metadata.st_ino,
    }
    _write_receipt_exclusive(receipt_path, receipt)


def _write_published_files_receipt(
    output: Path, receipt_path: Path, invocation_id: str
) -> None:
    complete = output / "generation-manifest.json"
    _require_regular(complete, "complete_manifest_not_regular")
    root_stat = output.stat(follow_symlinks=False)
    complete_stat = complete.stat(follow_symlinks=False)
    contents = complete.read_bytes()
    receipt = {
        "receipt_version": 1,
        "invocation_id": invocation_id,
        "output_path": str(output.resolve(strict=True)),
        "root_st_dev": root_stat.st_dev,
        "root_st_ino": root_stat.st_ino,
        "complete_path": str(complete.resolve(strict=True)),
        "complete_st_dev": complete_stat.st_dev,
        "complete_st_ino": complete_stat.st_ino,
        "complete_bytes": len(contents),
        "complete_sha256": hashlib.sha256(contents).hexdigest(),
    }
    _write_receipt_exclusive(receipt_path.parent / "published-files.json", receipt)


def _write_receipt_exclusive(path: Path, receipt: dict[str, Any]) -> None:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise ArtifactError("ownership_receipt_collision") from exc
    payload = (json.dumps(receipt, allow_nan=False, sort_keys=True, indent=2) + "\n").encode(
        "utf-8"
    )
    with os.fdopen(fd, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
