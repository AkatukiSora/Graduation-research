"""Portable, candidate-only artifacts generated from an external CSV contract."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import os
import platform
import re
import stat
import uuid
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, BinaryIO, Iterator

import numpy as np
import pandas as pd

from .input_manifest import InputError, InputManifest, iter_portable_raw_records, load_input_manifest
from .kernel import build_candidate_features
from .region import RegionError, load_boundary
from .source import SourceError, portable_normalize_selected_record


SCHEMA_VERSION = 3
KERNEL_VERSION = "p05-adopted-kernel"
MAX_CANDIDATES = 6
CANDIDATE_RADIUS_M = 80.0
MAX_OUTPUT_BYTES = 32 * 1024 * 1024
MAX_REPORT_BYTES = 8 * 1024 * 1024
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_MONTH_RE = re.compile(r"[0-9]{4}(0[1-9]|1[0-2])\Z", re.ASCII)
_SOURCE_REF_FIELDS = {"path", "bytes", "sha256", "crc32"}
_BOUNDARY_REF_FIELDS = {"path", "bytes", "sha256", "crs", "region_id"}
_INPUT_REF_FIELDS = {"path", "bytes", "sha256"}
_PROVENANCE_FIELDS = {
    "dataset", "publisher", "edition", "retrieved_at", "terms_reference", "rights_status"
}
_FILE_NAMES = {
    "normalized": "normalized.jsonl",
    "rejected": "rejected.jsonl",
    "candidates": "candidates.jsonl",
    "source_manifest": "source-manifest.json",
}
_NORMALIZED_FIELDS = [
    "entity_id", "region_id", "source_row", "selected_field_sha256", "source_month",
    "prefecture", "raw_uid", "record_id", "jartic_record_id", "ユニークキー",
    "police", "regulation", "branch", "raw_update", "update_semantics",
    "source_active_status", "source_location_token", "source_entrance_tokens",
    "entrance_points", "lon", "lat", "normalization_status", "included_boundary",
    "production_approved",
]
_REJECTED_FIELDS = ["raw_uid", "source_row", "reason", "selected_field_sha256"]
_KERNEL_COLUMNS = [
    "point_index", "neighbor_index", "record_id", "uniqueKey", "lon", "lat",
    "candidate_rank", "candidate_distance_m", "candidate_bearing", "p_d1", "p_d2",
    "p_d3", "p_gap12", "p_ratio12", "q_d1", "q_d2", "reciprocal_rank",
    "mutual_nearest", "min_axis_angle_other", "min_directed_angle_other",
    "third_midpoint_distance_m", "bearing_mod_45", "bearing_mod_90",
    *[name for radius in (12, 15, 18, 20, 25, 30, 40, 60, 100) for name in (f"p_n{radius}", f"q_n{radius}")],
]
_CANDIDATE_FIELDS = [
    *_KERNEL_COLUMNS, "entity_id", "candidate_entity_id", "source_row",
    "candidate_source_row", "source_month", "parent_normalized_sha256",
    "bearing_semantics", "production_approved",
]


class PortableArtifactError(RuntimeError):
    """Actionable failure from the portable artifact API."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class _Budget:
    def __init__(self, total: int = 0):
        self.total = total

    def add(self, amount: int) -> None:
        if amount < 0 or self.total + amount > MAX_OUTPUT_BYTES:
            raise PortableArtifactError("output_budget_exceeded")
        self.total += amount


class _JsonlWriter:
    def __init__(self, stream: BinaryIO, budget: _Budget):
        self.stream = stream
        self.budget = budget
        self.rows = 0
        self.bytes = 0

    def write(self, record: dict[str, Any]) -> None:
        try:
            line = (json.dumps(
                _json_value(record), ensure_ascii=False, allow_nan=False,
                separators=(",", ":"),
            ) + "\n").encode("utf-8")
        except (TypeError, ValueError, OverflowError) as exc:
            raise PortableArtifactError("invalid_json_value") from exc
        self.budget.add(len(line))
        self.stream.write(line)
        self.rows += 1
        self.bytes += len(line)


def generate_portable_artifact(
    data_dir: str | os.PathLike[str],
    input_manifest: str | os.PathLike[str] = "input-manifest.json",
    *,
    region_id: str,
    output: str | os.PathLike[str],
    ownership_receipt: str | os.PathLike[str] | None = None,
    published_receipt: str | os.PathLike[str] | None = None,
    invocation_id: str | None = None,
) -> Path:
    """Stream an external CSV into a fresh schema-3 portable artifact directory."""
    data_path = _absolute_path(data_dir)
    output_path = _absolute_path(output)
    receipt_args = _validate_receipt_arguments(
        ownership_receipt, published_receipt, invocation_id
    )
    _reject_symlink_components(data_path, "input_path_symlink")
    _reject_symlink_components(output_path.parent, "output_path_symlink")
    if os.path.lexists(output_path):
        raise PortableArtifactError("output_exists")
    if not data_path.is_dir():
        raise PortableArtifactError("INPUT_DATA_DIR_INVALID")
    if not output_path.parent.is_dir():
        raise PortableArtifactError("output_parent_missing")
    data_root = data_path.resolve(strict=True)
    parent_root = output_path.parent.resolve(strict=True)
    output_path = parent_root / output_path.name
    if _paths_overlap(data_root, output_path):
        raise PortableArtifactError("input_output_overlap")
    _reject_receipt_root_overlap(receipt_args, (data_root, output_path))
    if not isinstance(region_id, str) or not region_id.strip():
        raise PortableArtifactError("region_id_invalid")

    try:
        loaded = load_input_manifest(data_root, os.fspath(input_manifest))
        if loaded.manifest["boundary"]["region_id"] != region_id:
            raise PortableArtifactError("region_id_mismatch")
        boundary = load_boundary(loaded.boundary_path)
    except PortableArtifactError:
        raise
    except InputError as exc:
        raise PortableArtifactError(exc.code) from exc
    except RegionError as exc:
        raise PortableArtifactError(exc.code) from exc

    owned: dict[str, tuple[int, int]] = {}
    root_identity: tuple[int, int] | None = None
    try:
        try:
            output_path.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise PortableArtifactError("output_exists") from exc
        except OSError as exc:
            raise PortableArtifactError("output_create_failed") from exc
        root_stat = output_path.lstat()
        root_identity = (root_stat.st_dev, root_stat.st_ino)
        if receipt_args is not None:
            _write_ownership_receipt(output_path, *receipt_args[:3])
        counts = {
            "rows_total": 0, "excluded_non_stop": 0, "outside_region": 0,
            "rejected": 0, "normalized": 0, "candidate_rows": 0,
        }
        budget = _Budget()
        points: list[dict[str, Any]] = []
        normalized_stream, normalized_tmp = _open_temp(output_path, "normalized.jsonl", owned)
        rejected_stream, rejected_tmp = _open_temp(output_path, "rejected.jsonl", owned)
        try:
            with normalized_stream, rejected_stream:
                normalized_writer = _JsonlWriter(normalized_stream, budget)
                rejected_writer = _JsonlWriter(rejected_stream, budget)
                try:
                    for item in iter_portable_raw_records(loaded.source_path, loaded):
                        counts["rows_total"] += 1
                        selected = item["selected"]
                        selected_hash = hashlib.sha256(
                            json.dumps(selected, ensure_ascii=False, sort_keys=True).encode("utf-8")
                        ).hexdigest()
                        result = portable_normalize_selected_record(
                            selected,
                            source_row=item["source_row"],
                            source_month=item["source_month"],
                            region_id=region_id,
                            boundary=boundary,
                            selected_hash=selected_hash,
                        )
                        classification = result["classification"]
                        if classification == "excluded_non_stop":
                            counts["excluded_non_stop"] += 1
                        elif classification == "outside_region":
                            counts["outside_region"] += 1
                        elif classification == "rejected":
                            counts["rejected"] += 1
                            rejected_writer.write({
                                "raw_uid": result["raw_uid"],
                                "source_row": result["source_row"],
                                "reason": result["reason"],
                                "selected_field_sha256": result["selected_field_sha256"],
                            })
                        elif classification == "normalized":
                            record = result["record"]
                            if list(record) != _NORMALIZED_FIELDS:
                                raise PortableArtifactError("normalized_schema_mismatch")
                            counts["normalized"] += 1
                            normalized_writer.write(record)
                            points.append(record)
                        else:
                            raise PortableArtifactError("unknown_source_classification")
                except InputError as exc:
                    raise PortableArtifactError(exc.code) from exc
                except SourceError as exc:
                    raise PortableArtifactError(exc.code) from exc
                _finish_temp(normalized_stream, normalized_writer.bytes)
                _finish_temp(rejected_stream, rejected_writer.bytes)
        except BaseException:
            raise
        _publish_temp(output_path, "normalized.jsonl", normalized_writer.bytes, owned, normalized_tmp.name)
        _publish_temp(output_path, "rejected.jsonl", rejected_writer.bytes, owned, rejected_tmp.name)

        normalized_hash = _sha256_path(output_path / _FILE_NAMES["normalized"])[1]
        candidate_stream, candidate_tmp = _open_temp(output_path, "candidates.jsonl", owned)
        candidate_writer = _JsonlWriter(candidate_stream, budget)
        try:
            if len(points) >= 2:
                for candidate in _candidate_records(points, normalized_hash):
                    candidate_writer.write(candidate)
            _finish_temp(candidate_stream, candidate_writer.bytes)
        except BaseException:
            candidate_stream.close()
            raise
        _publish_temp(output_path, _FILE_NAMES["candidates"], candidate_writer.bytes, owned, candidate_tmp.name)
        counts["candidate_rows"] = candidate_writer.rows
        _reconcile_counts(counts)

        source_manifest = _build_source_manifest(data_root, loaded, os.fspath(input_manifest), region_id)
        source_manifest_bytes = _document_bytes(source_manifest)
        if len(source_manifest_bytes) > MAX_REPORT_BYTES:
            raise PortableArtifactError("report_budget_exceeded")
        budget.add(len(source_manifest_bytes))
        _write_atomic(output_path, _FILE_NAMES["source_manifest"], source_manifest_bytes, owned)

        files = {
            "normalized": _file_entry(output_path / _FILE_NAMES["normalized"], normalized_writer.rows),
            "rejected": _file_entry(output_path / _FILE_NAMES["rejected"], rejected_writer.rows),
            "candidates": _file_entry(output_path / _FILE_NAMES["candidates"], candidate_writer.rows),
            "source_manifest": _file_entry(output_path / _FILE_NAMES["source_manifest"], 1),
        }
        generation = {
            "schema_version": SCHEMA_VERSION,
            "region_id": region_id,
            "source_month": loaded.manifest["source"]["source_month"],
            "format_version": loaded.manifest["source"]["format_version"],
            "files": files,
            "counts": counts,
            "parameters": {
                "max_candidates": MAX_CANDIDATES,
                "candidate_radius_m": CANDIDATE_RADIUS_M,
            },
            "kernel_version": KERNEL_VERSION,
            "runtime_versions": _runtime_versions(),
            "production_approved": False,
        }
        manifest_bytes = _document_bytes(generation)
        _check_report_and_total(source_manifest_bytes, manifest_bytes, budget.total)
        draft_name = ".generation-manifest.draft.json"
        _write_atomic(output_path, draft_name, manifest_bytes, owned, charge=False)
        verify_portable_manifest(output_path / draft_name)
        _link_complete_manifest(output_path, draft_name, owned)
        if receipt_args is not None:
            verify_portable_manifest(output_path / "generation-manifest.json")
            _write_published_files_receipt(output_path, receipt_args[1], receipt_args[2])
        return output_path / "generation-manifest.json"
    except BaseException as exc:
        _remove_owned(output_path, owned, root_identity)
        if isinstance(exc, PortableArtifactError):
            raise
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise PortableArtifactError("generation_interrupted") from exc
        raise PortableArtifactError("generation_failed") from exc


def verify_portable_manifest(manifest: str | os.PathLike[str]) -> dict[str, Any]:
    """Read-only strict verifier; it never follows artifact symlinks or needs source inputs."""
    path = _absolute_path(manifest)
    _reject_symlink_components(path.parent, "manifest_not_regular")
    _require_regular(path, "manifest_not_regular")
    root = path.parent
    try:
        document = _read_json_object(path, "invalid_generation_manifest")
        return _verify_document(document, root, path.name)
    except PortableArtifactError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
        raise PortableArtifactError("invalid_generation_manifest") from exc


def resume_portable_artifact(
    manifest: str | os.PathLike[str],
    output: str | os.PathLike[str],
    *,
    ownership_receipt: str | os.PathLike[str] | None = None,
    published_receipt: str | os.PathLike[str] | None = None,
    invocation_id: str | None = None,
) -> Path:
    """Copy verified normalized state to a fresh directory and recompute only the kernel."""
    previous_path = _absolute_path(manifest)
    receipt_args = _validate_receipt_arguments(
        ownership_receipt, published_receipt, invocation_id
    )
    output_path: Path | None = None
    if receipt_args is not None:
        output_path = _absolute_path(output)
        _reject_symlink_components(previous_path.parent, "manifest_not_regular")
        _reject_symlink_components(output_path.parent, "output_path_symlink")
        protected_roots: tuple[Path, ...] = ()
        if previous_path.parent.is_dir():
            protected_roots += (previous_path.parent.resolve(strict=True),)
        if output_path.parent.is_dir():
            protected_roots += (
                output_path.parent.resolve(strict=True) / output_path.name,
            )
        _reject_receipt_root_overlap(receipt_args, protected_roots)
    previous = verify_portable_manifest(previous_path)
    if output_path is None:
        output_path = _absolute_path(output)
    _reject_symlink_components(output_path.parent, "output_path_symlink")
    if os.path.lexists(output_path):
        raise PortableArtifactError("output_exists")
    if not output_path.parent.is_dir():
        raise PortableArtifactError("output_parent_missing")
    output_path = output_path.parent.resolve(strict=True) / output_path.name
    previous_root = previous_path.parent.resolve(strict=True)
    if _paths_overlap(previous_root, output_path):
        raise PortableArtifactError("input_output_overlap")
    _reject_receipt_root_overlap(receipt_args, (previous_root, output_path))

    owned: dict[str, tuple[int, int]] = {}
    root_identity: tuple[int, int] | None = None
    try:
        try:
            output_path.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise PortableArtifactError("output_exists") from exc
        except OSError as exc:
            raise PortableArtifactError("output_create_failed") from exc
        root_stat = output_path.lstat()
        root_identity = (root_stat.st_dev, root_stat.st_ino)
        if receipt_args is not None:
            _write_ownership_receipt(output_path, *receipt_args[:3])
        budget = _Budget()
        old_files = previous["files"]
        for logical in ("normalized", "rejected", "source_manifest"):
            entry = old_files[logical]
            source_file = previous_path.parent / entry["path"]
            _copy_atomic(source_file, output_path, entry["path"], entry["bytes"], budget, owned)

        normalized_records = _read_jsonl_records(
            output_path / _FILE_NAMES["normalized"], _NORMALIZED_FIELDS
        )
        normalized_hash = _sha256_path(output_path / _FILE_NAMES["normalized"])[1]
        candidate_stream, candidate_tmp = _open_temp(output_path, "candidates.jsonl", owned)
        candidate_writer = _JsonlWriter(candidate_stream, budget)
        try:
            if len(normalized_records) >= 2:
                for candidate in _candidate_records(normalized_records, normalized_hash):
                    candidate_writer.write(candidate)
            _finish_temp(candidate_stream, candidate_writer.bytes)
        except BaseException:
            candidate_stream.close()
            raise
        _publish_temp(output_path, _FILE_NAMES["candidates"], candidate_writer.bytes, owned, candidate_tmp.name)

        counts = dict(previous["counts"])
        counts["candidate_rows"] = candidate_writer.rows
        generation = {
            "schema_version": SCHEMA_VERSION,
            "region_id": previous["region_id"],
            "source_month": previous["source_month"],
            "format_version": previous["format_version"],
            "files": {
                "normalized": _file_entry(output_path / _FILE_NAMES["normalized"], old_files["normalized"]["rows"]),
                "rejected": _file_entry(output_path / _FILE_NAMES["rejected"], old_files["rejected"]["rows"]),
                "candidates": _file_entry(output_path / _FILE_NAMES["candidates"], candidate_writer.rows),
                "source_manifest": _file_entry(output_path / _FILE_NAMES["source_manifest"], 1),
            },
            "counts": counts,
            "parameters": dict(previous["parameters"]),
            "kernel_version": previous["kernel_version"],
            "runtime_versions": _runtime_versions(),
            "production_approved": False,
            "resume": {
                "parent_generation_manifest_sha256": _sha256_path(previous_path)[1],
                "raw_scan_resumed": False,
                "region_clip_recomputed": False,
            },
        }
        source_manifest_bytes = (output_path / _FILE_NAMES["source_manifest"]).read_bytes()
        manifest_bytes = _document_bytes(generation)
        _check_report_and_total(source_manifest_bytes, manifest_bytes, budget.total)
        draft_name = ".generation-manifest.draft.json"
        _write_atomic(output_path, draft_name, manifest_bytes, owned, charge=False)
        verify_portable_manifest(output_path / draft_name)
        _link_complete_manifest(output_path, draft_name, owned)
        if receipt_args is not None:
            verify_portable_manifest(output_path / "generation-manifest.json")
            _write_published_files_receipt(output_path, receipt_args[1], receipt_args[2])
        return output_path / "generation-manifest.json"
    except BaseException as exc:
        _remove_owned(output_path, owned, root_identity)
        if isinstance(exc, PortableArtifactError):
            raise
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise PortableArtifactError("resume_failed") from exc
        raise PortableArtifactError("resume_failed") from exc


def _validate_receipt_arguments(
    ownership_receipt: str | os.PathLike[str] | None,
    published_receipt: str | os.PathLike[str] | None,
    invocation_id: str | None,
) -> tuple[Path, Path, str, Path, Path] | None:
    supplied = (ownership_receipt, published_receipt, invocation_id)
    if all(value is None for value in supplied):
        return None
    if (
        any(value is None for value in supplied)
        or not isinstance(invocation_id, str)
        or not invocation_id
        or len(invocation_id) > 128
    ):
        raise PortableArtifactError("invalid_ownership_receipt_arguments")
    assert ownership_receipt is not None and published_receipt is not None
    ownership_path = _absolute_path(ownership_receipt)
    published_path = _absolute_path(published_receipt)
    _reject_symlink_components(ownership_path.parent, "invalid_ownership_receipt_path")
    attempt_name = ownership_path.parent.name
    attempt_id = attempt_name.removeprefix("attempt-")
    attempts_root = ownership_path.parent.parent
    state_root = attempts_root.parent
    state_path = state_root / "budget-state.json"
    if (
        ownership_path.name != "ownership.json"
        or published_path != ownership_path.parent / "published-files.json"
        or attempts_root.name != "attempts"
        or not re.fullmatch(r"[0-9a-f]{32}", attempt_id, re.ASCII)
        or not ownership_path.parent.is_dir()
        or ownership_path.parent.is_symlink()
        or not state_root.is_dir()
        or state_root.is_symlink()
        or not state_path.is_file()
        or state_path.is_symlink()
        or os.path.lexists(ownership_path)
        or os.path.lexists(published_path)
    ):
        raise PortableArtifactError("invalid_ownership_receipt_path")
    active_state = _read_json_object(state_path, "invalid_ownership_receipt_path")
    in_flight = active_state.get("in_flight")
    stage = in_flight.get("stage") if isinstance(in_flight, dict) else None
    if (
        not isinstance(in_flight, dict)
        or stage not in {"generate", "resume"}
        or in_flight.get("invocation_id") != invocation_id
        or in_flight.get("owned_evidence_dir") != str(ownership_path.parent.resolve(strict=True))
    ):
        raise PortableArtifactError("invalid_ownership_receipt_path")
    _require_regular(ownership_path.parent / f"{stage}.log", "invalid_ownership_receipt_path")
    return (
        ownership_path,
        published_path,
        invocation_id,
        state_root.resolve(strict=True),
        ownership_path.parent.resolve(strict=True),
    )


def _reject_receipt_root_overlap(
    receipt_args: tuple[Path, Path, str, Path, Path] | None,
    protected_roots: tuple[Path, ...],
) -> None:
    if receipt_args is None:
        return
    state_root, attempt_root = receipt_args[3:]
    for receipt_root in (state_root, attempt_root):
        if any(_paths_overlap(receipt_root, protected) for protected in protected_roots):
            raise PortableArtifactError("invalid_ownership_receipt_path")


def _write_ownership_receipt(
    output: Path, ownership_path: Path, _published_path: Path, invocation_id: str
) -> None:
    metadata = output.stat(follow_symlinks=False)
    receipt = {
        "receipt_version": 1,
        "invocation_id": invocation_id,
        "output_path": str(output.resolve(strict=True)),
        "root_st_dev": metadata.st_dev,
        "root_st_ino": metadata.st_ino,
    }
    _write_receipt_atomic(ownership_path, receipt)


def _write_published_files_receipt(
    output: Path, published_path: Path, invocation_id: str
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
    _write_receipt_atomic(published_path, receipt)


def _write_receipt_atomic(path: Path, receipt: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(temporary, flags, 0o600)
        with os.fdopen(fd, "wb") as stream:
            payload = (json.dumps(receipt, allow_nan=False, sort_keys=True, indent=2) + "\n").encode(
                "utf-8"
            )
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path, follow_symlinks=False)
        temporary.unlink()
        _fsync_dir(path.parent)
    except FileExistsError as exc:
        raise PortableArtifactError("ownership_receipt_collision") from exc
    except OSError as exc:
        raise PortableArtifactError("ownership_receipt_write_failed") from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _verify_document(document: dict[str, Any], root: Path, manifest_name: str) -> dict[str, Any]:
    required = {
        "schema_version", "region_id", "source_month", "format_version", "files",
        "counts", "parameters", "kernel_version", "runtime_versions", "production_approved",
    }
    optional = {"resume"}
    if set(document) - required - optional or not required.issubset(document):
        raise PortableArtifactError("generation_manifest_schema_mismatch")
    if isinstance(document["schema_version"], bool) or document["schema_version"] != SCHEMA_VERSION:
        raise PortableArtifactError("unsupported_manifest_schema")
    if document["production_approved"] is not False:
        raise PortableArtifactError("production_approval_must_remain_false")
    if not isinstance(document["region_id"], str) or not document["region_id"].strip():
        raise PortableArtifactError("region_id_invalid")
    if not isinstance(document["source_month"], str) or not _MONTH_RE.fullmatch(document["source_month"]) or document["source_month"][:4] == "0000":
        raise PortableArtifactError("invalid_source_month")
    if document["format_version"] != "k_2.1" or document["kernel_version"] != KERNEL_VERSION:
        raise PortableArtifactError("manifest_version_mismatch")
    if document["parameters"] != {"max_candidates": MAX_CANDIDATES, "candidate_radius_m": CANDIDATE_RADIUS_M}:
        raise PortableArtifactError("manifest_parameter_mismatch")
    _validate_runtime_versions(document["runtime_versions"])

    files = document["files"]
    if not isinstance(files, dict) or set(files) != set(_FILE_NAMES):
        raise PortableArtifactError("manifest_files_mismatch")
    entries: dict[str, dict[str, Any]] = {}
    total = 0
    for logical, expected_name in _FILE_NAMES.items():
        entry = files[logical]
        if not isinstance(entry, dict) or set(entry) != {"path", "bytes", "sha256", "rows"}:
            raise PortableArtifactError("invalid_artifact_reference")
        relative = _validate_artifact_path(entry["path"])
        if relative.as_posix() != expected_name:
            raise PortableArtifactError("invalid_artifact_path")
        if not _nonnegative_int(entry["bytes"]) or not _nonnegative_int(entry["rows"]) or not _is_sha256(entry["sha256"]):
            raise PortableArtifactError("invalid_artifact_reference")
        artifact_path = root / relative
        _require_regular(artifact_path, "artifact_not_regular")
        size, digest = _sha256_path(artifact_path)
        if size != entry["bytes"] or digest != entry["sha256"]:
            raise PortableArtifactError("artifact_hash_mismatch")
        total += size
        entries[logical] = entry
    if total > MAX_OUTPUT_BYTES:
        raise PortableArtifactError("output_budget_exceeded")

    counts = document["counts"]
    count_keys = {"rows_total", "excluded_non_stop", "outside_region", "rejected", "normalized", "candidate_rows"}
    if not isinstance(counts, dict) or set(counts) != count_keys or any(not _nonnegative_int(counts[name]) for name in count_keys):
        raise PortableArtifactError("invalid_manifest_counts")
    _reconcile_counts(counts)
    expected_rows = {
        "normalized": counts["normalized"], "rejected": counts["rejected"],
        "candidates": counts["candidate_rows"], "source_manifest": 1,
    }
    for logical, row_count in expected_rows.items():
        if entries[logical]["rows"] != row_count:
            raise PortableArtifactError(f"{logical}_count_mismatch")

    source_manifest_path = root / _FILE_NAMES["source_manifest"]
    if entries["source_manifest"]["bytes"] > MAX_REPORT_BYTES:
        raise PortableArtifactError("report_budget_exceeded")
    source_manifest = _read_json_object(source_manifest_path, "invalid_source_manifest")
    _verify_source_manifest(source_manifest, document)
    source_report_size = entries["source_manifest"]["bytes"]
    generation_size = _sha256_path(root / manifest_name)[0]
    if source_report_size + generation_size > MAX_REPORT_BYTES:
        raise PortableArtifactError("report_budget_exceeded")
    if total + generation_size > MAX_OUTPUT_BYTES:
        raise PortableArtifactError("output_budget_exceeded")

    normalized_hash = entries["normalized"]["sha256"]
    normalized = _read_jsonl_records(root / _FILE_NAMES["normalized"], _NORMALIZED_FIELDS)
    rejected = _read_jsonl_records(root / _FILE_NAMES["rejected"], _REJECTED_FIELDS)
    candidates = _read_jsonl_records(root / _FILE_NAMES["candidates"], _CANDIDATE_FIELDS)
    if len(normalized) != counts["normalized"] or len(rejected) != counts["rejected"] or len(candidates) != counts["candidate_rows"]:
        raise PortableArtifactError("artifact_count_mismatch")
    for record in normalized:
        if (
            record.get("region_id") != document["region_id"]
            or record.get("source_month") != document["source_month"]
            or record.get("entity_id") != f"{document['region_id']}/{document['source_month']}/{record.get('raw_uid')}"
            or record.get("production_approved") is not False
            or record.get("source_active_status") != "unknown"
            or record.get("update_semantics") != "unknown"
            or record.get("normalization_status") != "valid_candidate_only"
            or not _is_sha256(record.get("selected_field_sha256"))
            or not _valid_uid(record.get("raw_uid"))
        ):
            raise PortableArtifactError("normalized_record_contract_mismatch")
    for record in rejected:
        if not _valid_uid(record.get("raw_uid")) or not _nonnegative_int(record.get("source_row")) or not isinstance(record.get("reason"), str) or not _is_sha256(record.get("selected_field_sha256")):
            raise PortableArtifactError("rejected_record_contract_mismatch")
    for record in candidates:
        if (
            record.get("production_approved") is not False
            or record.get("bearing_semantics") != "candidate_point_neighbor_only"
            or record.get("parent_normalized_sha256") != normalized_hash
            or record.get("source_month") != document["source_month"]
            or not _nonnegative_int(record.get("point_index"))
            or not _nonnegative_int(record.get("neighbor_index"))
            or record["point_index"] >= len(normalized)
            or record["neighbor_index"] >= len(normalized)
        ):
            raise PortableArtifactError("candidate_record_contract_mismatch")
        point = normalized[record["point_index"]]
        neighbor = normalized[record["neighbor_index"]]
        if record.get("entity_id") != point["entity_id"] or record.get("candidate_entity_id") != neighbor["entity_id"]:
            raise PortableArtifactError("candidate_provenance_mismatch")

    if "resume" in document:
        resume = document["resume"]
        if (
            not isinstance(resume, dict)
            or set(resume) != {"parent_generation_manifest_sha256", "raw_scan_resumed", "region_clip_recomputed"}
            or not _is_sha256(resume.get("parent_generation_manifest_sha256"))
            or resume.get("raw_scan_resumed") is not False
            or resume.get("region_clip_recomputed") is not False
        ):
            raise PortableArtifactError("invalid_resume_metadata")
    _verify_output_directory(root, manifest_name)
    return document


def _verify_source_manifest(source: dict[str, Any], generation: dict[str, Any]) -> None:
    required = {
        "schema_version", "source_month", "format_version", "region_id", "synthetic",
        "provenance", "source", "boundary", "input_manifest", "production_approved",
    }
    if (
        set(source) != required
        or isinstance(source.get("schema_version"), bool)
        or source.get("schema_version") != 1
    ):
        raise PortableArtifactError("invalid_source_manifest")
    if source.get("production_approved") is not False:
        raise PortableArtifactError("production_approval_must_remain_false")
    if (
        source.get("region_id") != generation["region_id"]
        or source.get("source_month") != generation["source_month"]
        or source.get("format_version") != generation["format_version"]
        or not isinstance(source.get("synthetic"), bool)
    ):
        raise PortableArtifactError("source_manifest_provenance_mismatch")
    provenance = source.get("provenance")
    if not isinstance(provenance, dict) or set(provenance) != _PROVENANCE_FIELDS or any(not isinstance(value, str) for value in provenance.values()):
        raise PortableArtifactError("invalid_source_provenance")
    _verify_source_ref(source.get("source"), _SOURCE_REF_FIELDS, allow_crc=True)
    _verify_source_ref(source.get("boundary"), _BOUNDARY_REF_FIELDS, allow_crc=False)
    _verify_source_ref(source.get("input_manifest"), _INPUT_REF_FIELDS, allow_crc=False)
    if source["boundary"].get("region_id") != generation["region_id"]:
        raise PortableArtifactError("source_boundary_region_mismatch")


def _verify_source_ref(value: Any, fields: set[str], *, allow_crc: bool) -> None:
    if not isinstance(value, dict) or set(value) != fields:
        raise PortableArtifactError("invalid_source_reference")
    _validate_provenance_path(value.get("path"))
    if not _nonnegative_int(value.get("bytes")) or not _is_sha256(value.get("sha256")):
        raise PortableArtifactError("invalid_source_reference")
    if allow_crc and (not isinstance(value.get("crc32"), str) or not re.fullmatch(r"[0-9a-f]{8}", value["crc32"], re.ASCII)):
        raise PortableArtifactError("invalid_source_reference")
    if not allow_crc:
        if "crs" in fields and (
            not isinstance(value.get("crs"), str)
            or value["crs"].upper() not in {
                "EPSG:4326", "URN:OGC:DEF:CRS:EPSG::4326",
                "URN:OGC:DEF:CRS:OGC::CRS84", "URN:OGC:DEF:CRS:OGC:1.3:CRS84",
                "OGC:CRS84",
            }
        ):
            raise PortableArtifactError("invalid_source_reference")
        if "region_id" in fields and (not isinstance(value.get("region_id"), str) or not value["region_id"].strip()):
            raise PortableArtifactError("invalid_source_reference")


def _build_source_manifest(data_root: Path, loaded: InputManifest, manifest_name: str, region_id: str) -> dict[str, Any]:
    source_meta = loaded.manifest["source"]
    boundary_meta = loaded.manifest["boundary"]
    input_path = data_root / Path(manifest_name)
    source_size, source_hash = _sha256_path(loaded.source_path)
    boundary_size, boundary_hash = _sha256_path(loaded.boundary_path)
    input_size, input_hash = _sha256_path(input_path)
    try:
        current_manifest = json.loads(
            input_path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise PortableArtifactError("input_manifest_changed_during_generation") from exc
    if current_manifest != loaded.manifest:
        raise PortableArtifactError("input_manifest_changed_during_generation")
    if (
        source_size != source_meta["bytes"]
        or source_hash != source_meta["sha256"]
        or boundary_size != loaded.manifest["boundary"]["bytes"]
        or boundary_hash != loaded.manifest["boundary"]["sha256"]
    ):
        raise PortableArtifactError("input_changed_during_generation")
    return {
        "schema_version": 1,
        "source_month": source_meta["source_month"],
        "format_version": source_meta["format_version"],
        "region_id": region_id,
        "synthetic": loaded.manifest["synthetic"],
        "provenance": dict(loaded.manifest["provenance"]),
        "source": {
            "path": loaded.source_path.relative_to(data_root).as_posix(),
            "bytes": source_size,
            "sha256": source_hash,
            "crc32": source_meta["crc32"],
        },
        "boundary": {
            "path": loaded.boundary_path.relative_to(data_root).as_posix(),
            "bytes": boundary_size,
            "sha256": boundary_hash,
            "crs": boundary_meta["crs"],
            "region_id": boundary_meta["region_id"],
        },
        "input_manifest": {
            "path": Path(manifest_name).as_posix(),
            "bytes": input_size,
            "sha256": input_hash,
        },
        "production_approved": False,
    }


def _candidate_records(points: list[dict[str, Any]], normalized_sha256: str) -> Iterator[dict[str, Any]]:
    kernel_points = pd.DataFrame({
        "lon": np.asarray([point["lon"] for point in points], dtype=np.float64),
        "lat": np.asarray([point["lat"] for point in points], dtype=np.float64),
        "jartic_record_id": pd.Series([point["jartic_record_id"] for point in points], dtype=object),
        "ユニークキー": pd.Series([point["ユニークキー"] for point in points], dtype=object),
    })
    snapshot = kernel_points.copy(deep=True)
    try:
        features = build_candidate_features(
            kernel_points, max_candidates=MAX_CANDIDATES, candidate_radius=CANDIDATE_RADIUS_M
        )
        pd.testing.assert_frame_equal(kernel_points, snapshot, check_exact=True)
    except Exception as exc:
        raise PortableArtifactError("candidate_kernel_failed") from exc
    if features.empty:
        return
    if list(features.columns) != _KERNEL_COLUMNS:
        raise PortableArtifactError("candidate_kernel_schema_mismatch")
    for values in features.itertuples(index=False, name=None):
        feature = dict(zip(features.columns, values, strict=True))
        index = _index_value(feature["point_index"], len(points))
        neighbor_index = _index_value(feature["neighbor_index"], len(points))
        point, neighbor = points[index], points[neighbor_index]
        if (
            str(feature["record_id"]) != point["jartic_record_id"]
            or str(feature["uniqueKey"]) != point["ユニークキー"]
            or float(feature["lon"]).hex() != float(point["lon"]).hex()
            or float(feature["lat"]).hex() != float(point["lat"]).hex()
        ):
            raise PortableArtifactError("candidate_provenance_mismatch")
        record = {key: feature[key] for key in _KERNEL_COLUMNS}
        record.update({
            "entity_id": point["entity_id"],
            "candidate_entity_id": neighbor["entity_id"],
            "source_row": point["source_row"],
            "candidate_source_row": neighbor["source_row"],
            "source_month": point["source_month"],
            "parent_normalized_sha256": normalized_sha256,
            "bearing_semantics": "candidate_point_neighbor_only",
            "production_approved": False,
        })
        yield record


def _open_temp(root: Path, final_name: str, owned: dict[str, tuple[int, int]]) -> tuple[BinaryIO, Path]:
    name = f".{final_name}.{uuid.uuid4().hex}.tmp"
    path = root / name
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
        info = os.fstat(fd)
        owned[name] = (info.st_dev, info.st_ino)
        return os.fdopen(fd, "wb"), path
    except OSError as exc:
        raise PortableArtifactError("temporary_output_create_failed") from exc


def _finish_temp(stream: BinaryIO, expected_bytes: int) -> None:
    stream.flush()
    os.fsync(stream.fileno())
    if os.fstat(stream.fileno()).st_size != expected_bytes:
        raise PortableArtifactError("temporary_output_size_mismatch")


def _publish_temp(root: Path, final_name: str, expected_bytes: int, owned: dict[str, tuple[int, int]], temp_name: str) -> None:
    temporary = root / temp_name
    final = root / final_name
    identity = owned.get(temp_name)
    if identity is None:
        raise PortableArtifactError("temporary_output_unowned")
    try:
        os.link(temporary, final, follow_symlinks=False)
        owned[final_name] = identity
        info = final.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size != expected_bytes:
            raise PortableArtifactError("published_output_invalid")
        temporary.unlink()
        owned.pop(temp_name, None)
        _fsync_dir(root)
    except FileExistsError as exc:
        raise PortableArtifactError("artifact_name_collision") from exc
    except OSError as exc:
        raise PortableArtifactError("artifact_publish_failed") from exc


def _write_atomic(root: Path, name: str, contents: bytes, owned: dict[str, tuple[int, int]], *, charge: bool = True, budget: _Budget | None = None) -> None:
    if charge and budget is not None:
        budget.add(len(contents))
    stream, temp_path = _open_temp(root, name, owned)
    try:
        with stream:
            stream.write(contents)
            _finish_temp(stream, len(contents))
        _publish_temp(root, name, len(contents), owned, temp_path.name)
    except BaseException:
        stream.close()
        raise


def _link_complete_manifest(root: Path, draft_name: str, owned: dict[str, tuple[int, int]]) -> None:
    draft = root / draft_name
    complete = root / "generation-manifest.json"
    identity = owned.get(draft_name)
    if identity is None:
        raise PortableArtifactError("draft_manifest_unowned")
    try:
        os.link(draft, complete, follow_symlinks=False)
        owned[complete.name] = identity
        info = complete.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise PortableArtifactError("complete_manifest_not_regular")
        draft.unlink()
        owned.pop(draft_name, None)
        _fsync_dir(root)
    except FileExistsError as exc:
        raise PortableArtifactError("complete_manifest_collision") from exc
    except OSError as exc:
        raise PortableArtifactError("complete_manifest_publish_failed") from exc


def _copy_atomic(source: Path, output: Path, name: str, expected_size: int, budget: _Budget, owned: dict[str, tuple[int, int]]) -> None:
    stream, temp_path = _open_temp(output, name, owned)
    size = 0
    try:
        with source.open("rb") as incoming, stream:
            while chunk := incoming.read(1024 * 1024):
                size += len(chunk)
                budget.add(len(chunk))
                stream.write(chunk)
            _finish_temp(stream, size)
        if size != expected_size:
            raise PortableArtifactError("artifact_copy_size_mismatch")
        _publish_temp(output, name, size, owned, temp_path.name)
    except BaseException:
        stream.close()
        raise


def _read_json_object(path: Path, error_code: str) -> dict[str, Any]:
    _require_regular(path, error_code)
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_REPORT_BYTES + 1)
        if len(raw) > MAX_REPORT_BYTES:
            raise PortableArtifactError("report_budget_exceeded")
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_unique_object,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
    except PortableArtifactError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise PortableArtifactError(error_code) from exc
    if not isinstance(value, dict):
        raise PortableArtifactError(error_code)
    return value


def _read_jsonl_records(path: Path, expected_fields: list[str]) -> list[dict[str, Any]]:
    _require_regular(path, "artifact_not_regular")
    result: list[dict[str, Any]] = []
    try:
        with path.open("rb") as stream:
            for raw_line in stream:
                if not raw_line.endswith(b"\n"):
                    raise PortableArtifactError("invalid_jsonl_ending")
                try:
                    record = json.loads(
                        raw_line[:-1].decode("utf-8"), object_pairs_hook=_unique_object,
                        parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
                    )
                except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
                    raise PortableArtifactError("invalid_jsonl_record") from exc
                if not isinstance(record, dict) or list(record) != expected_fields:
                    raise PortableArtifactError("artifact_schema_mismatch")
                _assert_finite(record)
                result.append(record)
    except PortableArtifactError:
        raise
    except OSError as exc:
        raise PortableArtifactError("artifact_read_error") from exc
    return result


def _file_entry(path: Path, rows: int) -> dict[str, Any]:
    size, digest = _sha256_path(path)
    return {"path": path.name, "bytes": size, "sha256": digest, "rows": rows}


def _document_bytes(value: dict[str, Any]) -> bytes:
    try:
        return (json.dumps(_json_value(value), ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise PortableArtifactError("invalid_manifest_value") from exc


def _json_value(value: Any) -> Any:
    if isinstance(value, np.generic):
        return _json_value(value.item())
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise PortableArtifactError("non_finite_json_value")
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise PortableArtifactError("invalid_json_value")


def _runtime_versions() -> dict[str, str]:
    try:
        versions = {
            "python": platform.python_version(),
            "numpy": importlib.metadata.version("numpy"),
            "pandas": importlib.metadata.version("pandas"),
            "scipy": importlib.metadata.version("scipy"),
        }
    except importlib.metadata.PackageNotFoundError as exc:
        raise PortableArtifactError("runtime_version_unavailable") from exc
    return versions


def _validate_runtime_versions(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != {"python", "numpy", "pandas", "scipy"} or any(not isinstance(item, str) or not item for item in value.values()):
        raise PortableArtifactError("invalid_runtime_versions")


def _check_report_and_total(source_report: bytes, generation_report: bytes, output_total: int) -> None:
    if len(source_report) + len(generation_report) > MAX_REPORT_BYTES:
        raise PortableArtifactError("report_budget_exceeded")
    if output_total + len(generation_report) > MAX_OUTPUT_BYTES:
        raise PortableArtifactError("output_budget_exceeded")


def _reconcile_counts(counts: dict[str, Any]) -> None:
    if counts["rows_total"] != sum(counts[name] for name in ("excluded_non_stop", "outside_region", "rejected", "normalized")):
        raise PortableArtifactError("row_count_reconciliation_failed")


def _validate_artifact_path(value: Any) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise PortableArtifactError("invalid_artifact_path")
    if PureWindowsPath(value).drive:
        raise PortableArtifactError("invalid_artifact_path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise PortableArtifactError("invalid_artifact_path")
    return path


def _validate_provenance_path(value: Any) -> None:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise PortableArtifactError("invalid_source_reference")
    if PureWindowsPath(value).drive:
        raise PortableArtifactError("invalid_source_reference")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise PortableArtifactError("invalid_source_reference")


def _verify_output_directory(root: Path, manifest_name: str) -> None:
    allowed = set(_FILE_NAMES.values()) | {"generation-manifest.json", ".generation-manifest.draft.json"}
    try:
        for item in root.iterdir():
            info = item.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise PortableArtifactError("artifact_symlink_rejected")
            if not stat.S_ISREG(info.st_mode):
                raise PortableArtifactError("artifact_not_regular")
            if item.name not in allowed or (item.name == "generation-manifest.json" and manifest_name != "generation-manifest.json"):
                if item.name != manifest_name:
                    raise PortableArtifactError("unexpected_artifact_file")
    except OSError as exc:
        raise PortableArtifactError("artifact_directory_unreadable") from exc


def _reject_symlink_components(path: Path, code: str) -> None:
    absolute = _absolute_path(path)
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise PortableArtifactError(code) from exc
        if stat.S_ISLNK(mode):
            raise PortableArtifactError(code)


def _require_regular(path: Path, code: str) -> None:
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            raise PortableArtifactError(code)
    except OSError as exc:
        raise PortableArtifactError(code) from exc


def _remove_owned(root: Path, owned: dict[str, tuple[int, int]], root_identity: tuple[int, int] | None) -> None:
    if root_identity is None:
        return
    try:
        root_stat = root.lstat()
        if not stat.S_ISDIR(root_stat.st_mode) or (root_stat.st_dev, root_stat.st_ino) != root_identity:
            return
    except OSError:
        return
    for name, identity in tuple(owned.items()):
        if Path(name).name != name:
            continue
        path = root / name
        try:
            info = path.lstat()
            if stat.S_ISREG(info.st_mode) and (info.st_dev, info.st_ino) == identity:
                path.unlink()
        except OSError:
            pass
    try:
        root.rmdir()
    except OSError:
        # Unknown/non-owned files or directories are deliberately preserved.
        pass


def _publish_temp_alias(source: Path, destination: Path) -> None:
    os.link(source, destination, follow_symlinks=False)


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sha256_path(path: Path) -> tuple[int, str]:
    _require_regular(path, "artifact_not_regular")
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                digest.update(chunk)
    except OSError as exc:
        raise PortableArtifactError("artifact_read_error") from exc
    return size, digest.hexdigest()


def _absolute_path(value: str | os.PathLike[str]) -> Path:
    try:
        raw = os.fspath(value)
        if (
            not isinstance(raw, str)
            or chr(0) in raw
            or chr(92) in raw
            or PureWindowsPath(raw).drive
            or ".." in Path(raw).parts
        ):
            raise PortableArtifactError("path_invalid")
        path = Path(raw)
        return path if path.is_absolute() else Path.cwd() / path
    except PortableArtifactError:
        raise
    except (TypeError, OSError) as exc:
        raise PortableArtifactError("path_invalid") from exc


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _assert_finite(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise PortableArtifactError("non_finite_json_value")
    if isinstance(value, dict):
        for item in value.values():
            _assert_finite(item)
    elif isinstance(value, list):
        for item in value:
            _assert_finite(item)


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and bool(_SHA256_RE.fullmatch(value))


def _nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _valid_uid(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 32 and value.isascii() and value.isalnum()


def _index_value(value: Any, size: int) -> int:
    try:
        index = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise PortableArtifactError("invalid_candidate_index") from exc
    if isinstance(value, bool) or float(value) != index or not 0 <= index < size:
        raise PortableArtifactError("invalid_candidate_index")
    return index
