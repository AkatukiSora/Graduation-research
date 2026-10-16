#!/usr/bin/env python3
"""Build/check the immutable synthetic-v2 streaming seed fixture.

This fixture is synthetic-only and never represents acquired or licensed real
regulation data. Its manifest canonical digest is shared with the Go validator.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import platform
import sys
import zlib
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[3]
SEEDDATA_DIR = APP_DIR / "internal" / "seeddata" / "data"
V1_DIR = SEEDDATA_DIR / "synthetic-v1"
V2_DIR = SEEDDATA_DIR / "synthetic-v2"
INPUT_DIR = Path(__file__).resolve().parent / "input"
GENERATION_IO_DIR = APP_DIR.parent / "generation_io"
DATASET_ID = "synthetic-v2"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def jsonl(rows: list[dict]) -> bytes:
    return b"".join(canonical(row) + b"\n" for row in rows)


def gzip_bytes(data: bytes) -> bytes:
    target = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", compresslevel=9, fileobj=target, mtime=0) as stream:
        stream.write(data)
    return target.getvalue()


def make_counts(points: list[dict], candidates: list[dict]) -> dict:
    def tally(rows: list[dict], field: str, keys: list[str]) -> dict:
        result = {key: 0 for key in keys}
        for row in rows:
            result[row[field]] += 1
        return result

    return {
        "points": len(points),
        "candidates": len(candidates),
        "rejected": 0,
        "outside_region": 0,
        "rows_total": len(points),
        "excluded_non_stop": 0,
        "direction_status_counts": tally(points, "direction_status", ["unknown", "inferred", "confirmed"]),
        "validity_status_counts": tally(points, "validity_status", ["unknown", "valid", "invalid"]),
        "imputation_method_counts": tally(points, "imputation_method", ["none", "source_entrance_geometry", "nearest_stop_reference", "osm_fusion_cautious", "field_observation"]),
        "imputation_reason_counts": tally(points, "imputation_reason", ["none", "missing_entrance_coordinates", "unsupported_entrance_geometry", "topology_only_unverified", "source_identity_unverified", "legacy_unrecorded", "source_geometry_derived", "model_direction_inference", "topology_proxy_unverified", "no_neighbor_within_radius", "zero_length_geometry", "ambiguous_nearest_neighbor", "multiple_entrance_directions"]),
        "validity_reason_counts": tally(points, "validity_reason", ["source_validity_unverified", "active_regulation_evidence", "withdrawn_regulation_evidence", "synthetic_fixture", "legacy_unrecorded"]),
    }


def build() -> dict[str, bytes]:
    # This is a tiny immutable fixture, never a real or licensed dataset.
    old_dataset = json.loads((V1_DIR / "dataset.json").read_text(encoding="utf-8"))
    source_csv = (INPUT_DIR / "source.csv").read_bytes()
    boundary = (INPUT_DIR / "boundary.geojson").read_bytes()
    input_manifest_bytes = (INPUT_DIR / "input-manifest.json").read_bytes()
    input_manifest = json.loads(input_manifest_bytes)
    lock = (GENERATION_IO_DIR / "uv.lock").read_bytes()
    script_bytes = Path(__file__).read_bytes()

    points = []
    for row in old_dataset["points"]:
        points.append({
            "entity_id": row["entity_id"], "raw_uid": row["raw_uid"], "lon": row["lon"], "lat": row["lat"],
            "direction_degrees": None, "direction_status": "unknown", "imputation_method": "none",
            "imputation_reason": "missing_entrance_coordinates", "direction_source": "unknown", "direction_evidence": {},
            "validity_status": "unknown", "validity_reason": "synthetic_fixture", "validity_evidence": {},
            "normalization_status": row["normalization_status"],
        })
    points.sort(key=lambda row: row["entity_id"])

    candidates = []
    for row in old_dataset["candidates"]:
        candidates.append({
            "source_entity_id": row["source_entity_id"], "neighbor_entity_id": row["neighbor_entity_id"],
            "rank": row["rank"], "distance_m": row["distance_m"], "candidate_bearing": row["candidate_bearing"],
            "features": row["features"], "bearing_semantics": "candidate_point_neighbor_only",
        })
    candidates.sort(key=lambda row: (row["source_entity_id"], row["rank"]))

    files: list[dict] = []
    payloads: dict[str, bytes] = {}
    for kind, rows in (("candidates", candidates), ("points", points)):
        if not rows:
            continue
        raw = jsonl(rows)
        if len(raw) > 4 << 20:
            raise ValueError(f"{kind} fixture exceeds the 4 MiB shard limit")
        basename = f"{kind}-00001.jsonl.gz"
        compressed = gzip_bytes(raw)
        payloads[basename] = compressed
        files.append({
            "kind": kind, "basename": basename, "rows": len(rows), "compressed_bytes": len(compressed),
            "compressed_sha256": sha256(compressed), "uncompressed_bytes": len(raw),
            "uncompressed_sha256": sha256(raw),
        })

    boundary_sha = sha256(boundary)
    if sha256(source_csv) != input_manifest["source"]["sha256"] or boundary_sha != input_manifest["boundary"]["sha256"]:
        raise ValueError("synthetic source bytes differ from their checked-in input manifest")
    manifest = {
        "schema_version": 2, "dataset_id": DATASET_ID, "source_month": input_manifest["source"]["source_month"],
        "region_id": input_manifest["boundary"]["region_id"], "synthetic": True, "counts": make_counts(points, candidates),
        "geometry": {
            "region_id": input_manifest["boundary"]["region_id"], "center_lat": "1.0000000", "center_lon": "1.0000000",
            "center_source": "synthetic fixture only", "radius_m": 0, "sphere_radius_m": "6371008.8",
            "vertices": 4, "coordinate_decimal_places": 0, "algorithm": "synthetic-rectangle-fixture-v1",
            "boundary_inclusion": "covers", "boundary_sha256": boundary_sha,
        },
        "inputs": {
            "source_zip_sha256": None, "source_csv_sha256": sha256(source_csv), "source_csv_bytes": len(source_csv),
            "source_header_sha256": input_manifest["source"]["header_sha256"], "boundary_sha256": boundary_sha,
            "input_manifest_sha256": sha256(input_manifest_bytes), "enrichment_sha256": None,
        },
        "generator": {
            "code_ref": "db/seed/synthetic/build_dataset_v2.py", "code_sha256": sha256(script_bytes),
            "method_implementation_sha256": sha256(script_bytes), "method_version": "synthetic-v1-preserving-v2-v1",
            "source_selection_order": ["source_entrance_geometry", "nearest_stop_reference", "unknown"],
            "commit": "uncommitted deterministic fixture generator", "uv_lock_sha256": sha256(lock),
            "python_version": platform.python_version(),
            "dependency_versions": {"implementation": platform.python_implementation(), "json": "python-stdlib", "zlib": zlib.ZLIB_VERSION},
            "zlib_version": zlib.ZLIB_VERSION, "kernel": "synthetic-v1 fixture conversion; no real-data inference",
            "max_candidates": 6, "candidate_radius_m": 80,
            "thread_policy": {"PYTHONHASHSEED": "0", "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1", "NUMEXPR_NUM_THREADS": "1"},
        },
        "provenance": {
            "dataset": "synthetic regulation fixture", "publisher": "graduation-research-synthetic", "edition": DATASET_ID,
            "source_url": "", "terms_reference": "not-applicable-synthetic-only", "terms_sha256": "",
            "rights_status": "synthetic-no-third-party-data", "attribution": "Synthetic-only fixture; no third-party data.",
            "modification_notice": "Deterministic conversion of the checked-in synthetic-v1 fixture.",
            "identity_scope": "source_month", "source_identity_note": "Test fixture only; not evidence of real source acquisition, inference, or permission.",
        },
        "files": files, "content_sha256": "",
    }
    manifest["content_sha256"] = sha256(canonical({key: value for key, value in manifest.items() if key != "content_sha256"}))
    payloads["manifest.json"] = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")
    return payloads


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="create/update the synthetic-v2 fixture")
    mode.add_argument("--check", action="store_true", help="verify the checked-in fixture is byte-identical")
    args = parser.parse_args()
    expected = build()
    mismatches: list[str] = []
    if args.write:
        V2_DIR.mkdir(parents=True, exist_ok=True)
        for name, data in expected.items():
            (V2_DIR / name).write_bytes(data)
        for current in V2_DIR.iterdir():
            if current.is_file() and current.name not in expected:
                current.unlink()
    else:
        current_files = {entry.name for entry in V2_DIR.iterdir() if entry.is_file()} if V2_DIR.is_dir() else set()
        if current_files != set(expected):
            mismatches.append("file set")
        for name, data in expected.items():
            if not (V2_DIR / name).is_file() or (V2_DIR / name).read_bytes() != data:
                mismatches.append(name)
    if mismatches:
        print("not reproducible: " + ", ".join(mismatches), file=sys.stderr)
        return 1
    manifest = json.loads(expected["manifest.json"])
    print(json.dumps({"mode": "write" if args.write else "check", "dataset_id": DATASET_ID,
                      "content_sha256": manifest["content_sha256"], "counts": manifest["counts"],
                      "identical": True if args.check else None}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
