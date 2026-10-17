#!/usr/bin/env python3
"""Rebuild or verify the embedded synthetic seed dataset.

The dataset is produced in three deterministic steps:

1. write a small, fully synthetic regulation CSV (170-column ``k_2.1`` layout,
   CP932) and a rectangular boundary,
2. run the portable ``generation_io`` CLI (``prepare-input`` and ``generate``)
   on that input,
3. convert the CLI artifacts into the versioned JSON that the Go seed binary
   embeds (``dataset.json`` and ``manifest.json``).

Usage (from ``backend/generation_io`` so the locked environment is active)::

    uv run --locked python ../app/db/seed/synthetic/build_dataset.py --write
    uv run --locked python ../app/db/seed/synthetic/build_dataset.py --check

``--write`` replaces the checked-in input and embedded data. ``--check``
rebuilds everything in a temporary directory and fails unless every file is
byte-identical to the checked-in copy. No real regulation data, real boundary
or randomness is involved: all coordinates are fixed values near lon/lat 1.0.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[3]
INPUT_DIR = Path(__file__).resolve().parent / "input"
DATA_DIR = APP_DIR / "internal" / "seeddata" / "data" / "synthetic-v1"
DEFAULT_GENERATION_IO = APP_DIR.parent / "generation_io"
TEMPLATE_CSV = Path("tests") / "fixtures" / "synthetic" / "source.csv"

DATASET_ID = "synthetic-v1"
REGION_ID = "synthetic:rectangle"
SOURCE_MONTH = "202701"
RIGHTS_STATUS = "synthetic-no-third-party-data"
SCHEMA_VERSION = 1
BEARING_SEMANTICS = "candidate_point_neighbor_only"
UID_PREFIX = "SYNTHETIC"
COL_UNIQUE_KEY = 20
COL_LONLAT = 23
HEADER_COLUMNS = 170

# Arbitrary synthetic positions (lon, lat). Ten points form a cluster with
# distinct pairwise distances (tens of metres) so that neighbour candidates
# exist; the last two are isolated and therefore have no candidates.
POINTS = [
    ("1.00000", "1.00000"),
    ("1.00031", "1.00007"),
    ("1.00058", "1.00019"),
    ("1.00012", "1.00040"),
    ("1.00043", "1.00052"),
    ("1.00071", "1.00041"),
    ("1.00004", "1.00071"),
    ("1.00029", "1.00083"),
    ("1.00066", "1.00088"),
    ("1.00089", "1.00064"),
    ("1.50000", "1.50000"),
    ("0.50000", "1.20000"),
]
BOUNDARY = (
    '{"type":"Feature","properties":{"synthetic":true,"production_approved":false},'
    '"geometry":{"type":"Polygon","coordinates":[[[0,0],[2,0],[2,2],[0,2],[0,0]]]}}\n'
)
NON_FEATURE_COLUMNS = {
    "point_index", "neighbor_index", "record_id", "uniqueKey", "lon", "lat",
    "candidate_rank", "candidate_distance_m", "candidate_bearing",
}


class BuildError(RuntimeError):
    pass


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_source_csv(generation_io: Path) -> bytes:
    template = (generation_io / TEMPLATE_CSV).read_bytes().decode("cp932")
    lines = template.split("\n")
    if len(lines) < 3 or lines[-1] != "" or "\r" in template or '"' in template:
        raise BuildError("template CSV has an unexpected shape")
    header = lines[0]
    row = lines[1].split(",")
    if len(header.split(",")) != HEADER_COLUMNS or len(row) != HEADER_COLUMNS:
        raise BuildError("template CSV must have 170 columns")
    out = [header]
    for number, (lon, lat) in enumerate(POINTS, start=1):
        fields = list(row)
        uid = f"{UID_PREFIX}{number:023d}"
        if len(uid) != 32:
            raise BuildError("synthetic uid length")
        fields[COL_UNIQUE_KEY] = uid
        fields[COL_LONLAT] = f"{lon} {lat}"
        out.append(",".join(fields))
    return ("\n".join(out) + "\n").encode("cp932")


def run_cli(python: str, generation_io: Path, *args: str) -> str:
    result = subprocess.run(
        [python, "-B", "-m", "generation_io", *args],
        cwd=generation_io, capture_output=True, text=True, timeout=300,
    )
    if result.returncode != 0:
        raise BuildError(f"generation_io {args[0]} failed: {result.stderr.strip()}")
    return result.stdout


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def build_dataset(points: list[dict], candidates: list[dict]) -> bytes:
    point_rows = []
    for record in points:
        point_rows.append({
            "entity_id": record["entity_id"],
            "raw_uid": record["raw_uid"],
            "lon": record["lon"],
            "lat": record["lat"],
            "direction_degrees": None,
            "direction_status": "unknown",
            "validity_status": "unknown",
            "normalization_status": record["normalization_status"],
        })
    candidate_rows = []
    for record in candidates:
        if record["bearing_semantics"] != BEARING_SEMANTICS or record["production_approved"] is not False:
            raise BuildError("unexpected candidate semantics")
        bearing = record["candidate_bearing"]
        if not (isinstance(bearing, float) and math.isfinite(bearing) and 0 <= bearing < 360):
            raise BuildError("candidate bearing out of range")
        candidate_rows.append({
            "source_entity_id": record["entity_id"],
            "neighbor_entity_id": record["candidate_entity_id"],
            "rank": int(record["candidate_rank"]),
            "distance_m": record["candidate_distance_m"],
            "candidate_bearing": bearing,
            "features": {k: v for k, v in record.items() if k in _feature_keys(record)},
            "bearing_semantics": BEARING_SEMANTICS,
        })
    lines = [f'{{"schema_version":{SCHEMA_VERSION},"dataset_id":{dump(DATASET_ID)},"points":[']
    lines.append(",\n".join(dump(row) for row in point_rows))
    lines.append('],"candidates":[')
    lines.append(",\n".join(dump(row) for row in candidate_rows))
    lines.append("]}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _feature_keys(record: dict) -> list[str]:
    keys = []
    for key in record:
        if key in NON_FEATURE_COLUMNS:
            continue
        if key in {"entity_id", "candidate_entity_id", "source_row", "candidate_source_row",
                   "source_month", "parent_normalized_sha256", "bearing_semantics",
                   "production_approved"}:
            continue
        keys.append(key)
    return keys


def build(python: str, generation_io: Path, staging: Path) -> None:
    source = build_source_csv(generation_io)
    input_dir = staging / "input"
    input_dir.mkdir(parents=True)
    (input_dir / "source.csv").write_bytes(source)
    (input_dir / "boundary.geojson").write_bytes(BOUNDARY.encode("utf-8"))
    run_cli(
        python, generation_io, "prepare-input",
        "--data-dir", str(input_dir), "--source", "source.csv", "--boundary", "boundary.geojson",
        "--source-month", SOURCE_MONTH, "--region-id", REGION_ID,
        "--manifest-out", str(input_dir / "input-manifest.json"),
        "--dataset", "synthetic-regulation-63", "--publisher", "graduation-research-synthetic",
        "--edition", DATASET_ID, "--retrieved-at", "not-applicable",
        "--terms-reference", "not-applicable", "--rights-status", RIGHTS_STATUS, "--synthetic",
    )
    run_dir = staging / "run"
    run_cli(
        python, generation_io, "generate",
        "--data-dir", str(input_dir), "--input-manifest", "input-manifest.json",
        "--region-id", REGION_ID, "--output-dir", str(run_dir),
    )
    run_cli(python, generation_io, "verify", "--manifest", str(run_dir / "generation-manifest.json"))

    generation = json.loads((run_dir / "generation-manifest.json").read_text(encoding="utf-8"))
    points = read_jsonl(run_dir / "normalized.jsonl")
    candidates = read_jsonl(run_dir / "candidates.jsonl")
    counts = generation["counts"]
    if counts["rejected"] != 0 or counts["outside_region"] != 0 or counts["excluded_non_stop"] != 0:
        raise BuildError("synthetic generation must not reject or exclude rows")
    if counts["normalized"] != len(points) or counts["candidate_rows"] != len(candidates):
        raise BuildError("generation counts disagree with artifacts")
    if len(points) != len(POINTS) or not candidates:
        raise BuildError("synthetic input must yield every point and some candidates")
    entity_ids = {p["entity_id"] for p in points}
    if any(c["candidate_entity_id"] not in entity_ids or c["entity_id"] not in entity_ids for c in candidates):
        raise BuildError("candidate refers to a point outside the dataset")
    if not any(p["entity_id"] not in {c["entity_id"] for c in candidates} for p in points):
        raise BuildError("expected at least one isolated point without candidates")

    dataset_bytes = build_dataset(points, candidates)
    input_manifest = json.loads((input_dir / "input-manifest.json").read_text(encoding="utf-8"))
    files = generation["files"]
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "dataset_id": DATASET_ID,
        "dataset_file": "dataset.json",
        "source_month": SOURCE_MONTH,
        "region_id": REGION_ID,
        "synthetic": True,
        "production_approved": False,
        "content_sha256": sha256_bytes(dataset_bytes),
        "counts": {
            "points": len(points),
            "candidates": len(candidates),
            "rejected": counts["rejected"],
            "outside_region": counts["outside_region"],
        },
        "provenance": {
            **input_manifest["provenance"],
            "input_manifest_sha256": sha256_bytes((input_dir / "input-manifest.json").read_bytes()),
        },
        "generator": {
            "tool": "generation_io",
            "artifact_schema_version": generation["schema_version"],
            "kernel_version": generation["kernel_version"],
            "parameters": generation["parameters"],
            "runtime_versions": generation["runtime_versions"],
            "generation_manifest_sha256": sha256_bytes((run_dir / "generation-manifest.json").read_bytes()),
            "artifact_sha256": {name: files[name]["sha256"] for name in sorted(files)},
            "build_script": "db/seed/synthetic/build_dataset.py",
        },
    }
    data_dir = staging / "data"
    data_dir.mkdir()
    (data_dir / "dataset.json").write_bytes(dataset_bytes)
    (data_dir / "manifest.json").write_bytes(
        (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    )


def targets(staging: Path) -> list[tuple[Path, Path]]:
    return [
        (staging / "input" / "source.csv", INPUT_DIR / "source.csv"),
        (staging / "input" / "boundary.geojson", INPUT_DIR / "boundary.geojson"),
        (staging / "input" / "input-manifest.json", INPUT_DIR / "input-manifest.json"),
        (staging / "data" / "dataset.json", DATA_DIR / "dataset.json"),
        (staging / "data" / "manifest.json", DATA_DIR / "manifest.json"),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="regenerate the checked-in files")
    mode.add_argument("--check", action="store_true", help="fail unless a rebuild is byte-identical")
    parser.add_argument("--generation-io-dir", type=Path, default=DEFAULT_GENERATION_IO)
    parser.add_argument("--python", default=sys.executable, help="interpreter with generation_io dependencies")
    args = parser.parse_args()

    staging = Path(tempfile.mkdtemp(prefix="seed-build-"))
    try:
        build(args.python, args.generation_io_dir.resolve(), staging)
        mismatches = []
        for built, checked_in in targets(staging):
            if args.write:
                checked_in.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(built, checked_in)
            elif not checked_in.is_file() or checked_in.read_bytes() != built.read_bytes():
                mismatches.append(str(checked_in.relative_to(APP_DIR)))
        if mismatches:
            print("not reproducible: " + ", ".join(mismatches), file=sys.stderr)
            return 1
        manifest = json.loads((staging / "data" / "manifest.json").read_text(encoding="utf-8"))
        print(json.dumps({
            "mode": "write" if args.write else "check",
            "dataset_id": manifest["dataset_id"],
            "content_sha256": manifest["content_sha256"],
            "counts": manifest["counts"],
            "identical": True if args.check else None,
        }))
        return 0
    except BuildError as exc:
        print(f"build failed: {exc}", file=sys.stderr)
        return 1
    finally:
        shutil.rmtree(staging, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
