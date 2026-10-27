#!/usr/bin/env python3
"""Independently reconcile every SQL seed row with its read-only JSONL source."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

DATASET_ID = "external-stop-approach-test-202602-v1"
ARTIFACT_SHA256 = "02eeb6998b5e926844448b4260d29c95f5b9a8a1b4216397789cbf1973493ebe"
EXPECTED_COUNTS = {"teacher": {"rows": 394, "inferred": 388, "unknown": 6},
                   "target": {"rows": 1939, "inferred": 1937, "unknown": 2}}
SEMANTICS = "vehicle_approach_to_stop_estimate"
METHOD_VERSION = "local-stop-approach-offline-v1"
POINTS_MARKER = "$external_seed_points$"
DATASET_MARKER = "$external_seed_dataset_check$"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def extract_dollar_json(sql: str, marker: str) -> Any:
    start = sql.find(marker)
    require(start >= 0, f"SQL is missing {marker}")
    start += len(marker)
    end = sql.find(marker, start)
    require(end >= 0, f"SQL is missing closing {marker}")
    return json.loads(sql[start:end])


def expected_provenance(manifest: dict[str, Any], manifest_bytes: bytes) -> dict[str, Any]:
    p = manifest["provenance"]
    return {
        "data_purpose": "direction_test",
        "source": "external_local_stop_approach",
        "direction_semantics": SEMANTICS,
        "method_version": METHOD_VERSION,
        "confidence": None,
        "confidence_semantics": "not_provided",
        "input_artifact_sha256": ARTIFACT_SHA256,
        "input_manifest_sha256": sha256(manifest_bytes),
        "input_rows": 2333,
        "subsets": {
            subset: {
                "rows": EXPECTED_COUNTS[subset]["rows"],
                "inferred": EXPECTED_COUNTS[subset]["inferred"],
                "unknown": EXPECTED_COUNTS[subset]["unknown"],
                "source_csv_sha256": p["candidate_reference"]["files"][subset]["source_csv_sha256"],
                "source_pbf_sha256": p["osm_inputs"][subset]["sha256"],
            }
            for subset in ("teacher", "target")
        },
        "external_source": {
            "archive_sha1": p["external_source"]["archive_sha1"],
            "implementation_source_sha256": p["external_source"]["python_source_sha256"],
            "license_status": "unconfirmed; original external source is private and excluded from generated output",
            "redistributed": False,
        },
        "attribution": {
            "jartic": "JARTIC-derived source CSV; attribution and applicable rights require owner review",
            "osm": "© OpenStreetMap contributors; OSM data is subject to ODbL 1.0",
            "rights_caveat": "The external implementation license is unconfirmed; its archive and source code are not included.",
        },
        "generated_by": "team-internal experimental run; no production integration",
        "production_approved": False,
    }


def expected_points(source_rows: list[dict[str, Any]], manifest: dict[str, Any]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    source_hashes = manifest["provenance"]["candidate_reference"]["files"]
    for row in source_rows:
        inferred = row["direction_degrees"] is not None
        evidence = ({
            "schema_version": 1,
            "kind": "external_local_stop_approach",
            "source_csv_sha256": source_hashes[row["subset"]]["source_csv_sha256"],
            "method_version": METHOD_VERSION,
            "direction_semantics": SEMANTICS,
            "artifact_sha256": ARTIFACT_SHA256,
            "record_ref": row["entity_id"],
            "subset": row["subset"],
            "confidence": None,
        } if inferred else {})
        output.append({
            "entity_id": row["entity_id"],
            "raw_uid": row["raw_uid"],
            "longitude": row["lon"],
            "latitude": row["lat"],
            "direction_degrees": row["direction_degrees"],
            "direction_status": "inferred" if inferred else "unknown",
            "validity_status": "unknown",
            "normalization_status": "valid_candidate_only",
            "imputation_method": "external_local_stop_approach" if inferred else "none",
            "imputation_reason": "model_direction_inference" if inferred else "topology_only_unverified",
            "direction_source": "external_local_stop_approach" if inferred else "unknown",
            "direction_evidence": evidence,
            "validity_reason": "source_validity_unverified",
            "validity_evidence": {},
        })
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-jsonl", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--sql", required=True, type=Path)
    args = parser.parse_args()
    try:
        artifact_bytes = args.input_jsonl.read_bytes()
        manifest_bytes = args.manifest.read_bytes()
        manifest = json.loads(manifest_bytes)
        require(sha256(artifact_bytes) == ARTIFACT_SHA256, "source artifact digest mismatch")
        require(manifest["data_sha256"] == ARTIFACT_SHA256, "manifest artifact digest mismatch")
        require(manifest["dataset_id"] == DATASET_ID and manifest["rows"] == 2333,
                "manifest identity/row count mismatch")
        source_rows = [json.loads(line) for line in artifact_bytes.decode("utf-8").splitlines()]
        require(len(source_rows) == 2333, f"source rows={len(source_rows)}")

        source_counts: dict[str, Counter[str]] = {"teacher": Counter(), "target": Counter()}
        source_ids: set[str] = set()
        source_uids: set[str] = set()
        for index, row in enumerate(source_rows, 1):
            require(isinstance(row, dict), f"source row {index} is not an object")
            require(row.get("source") == "external_local_stop_approach" and row.get("confidence") is None,
                    f"source/confidence mismatch at row {index}")
            subset = row.get("subset")
            require(subset in source_counts, f"unknown subset at row {index}")
            require(row.get("entity_id") not in source_ids and row.get("raw_uid") not in source_uids,
                    f"duplicate ID at source row {index}")
            source_ids.add(row["entity_id"])
            source_uids.add(row["raw_uid"])
            degree = row.get("direction_degrees")
            require(degree is None or (finite(degree) and 0 <= degree < 360),
                    f"invalid direction degree at source row {index}")
            require(finite(row.get("lat")) and -90 <= row["lat"] <= 90, f"invalid latitude at row {index}")
            require(finite(row.get("lon")) and -180 <= row["lon"] <= 180, f"invalid longitude at row {index}")
            source_counts[subset]["rows"] += 1
            source_counts[subset]["inferred" if degree is not None else "unknown"] += 1
        require({key: dict(value) for key, value in source_counts.items()} == EXPECTED_COUNTS,
                f"source counts differ: {source_counts}")
        require(manifest["counts"] == EXPECTED_COUNTS, "manifest counts differ from fixed expected counts")

        sql = args.sql.read_text(encoding="utf-8")
        actual_rows = extract_dollar_json(sql, POINTS_MARKER)
        actual_dataset = extract_dollar_json(sql, DATASET_MARKER)
        wanted_rows = expected_points(source_rows, manifest)
        require(actual_rows == wanted_rows, "generated SQL point JSON does not exactly match all source rows")
        require(len(actual_rows) == 2333, f"SQL point rows={len(actual_rows)}")
        require(all(set(point) == {
            "entity_id", "raw_uid", "longitude", "latitude", "direction_degrees", "direction_status",
            "validity_status", "normalization_status", "imputation_method", "imputation_reason",
            "direction_source", "direction_evidence", "validity_reason", "validity_evidence",
        } for point in actual_rows), "SQL contains a point with unexpected fields")

        provenance = expected_provenance(manifest, manifest_bytes)
        payload = {
            "dataset_id": DATASET_ID,
            "source_month": "202602",
            "region_id": "jp:shiga-aichi:external-test",
            "synthetic": False,
            "production_approved": False,
            "provenance": provenance,
            "points": wanted_rows,
        }
        content_sha = sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                        separators=(",", ":"), allow_nan=False).encode("utf-8"))
        wanted_dataset = {
            "dataset_id": DATASET_ID,
            "content_sha256": content_sha,
            "source_month": "202602",
            "region_id": "jp:shiga-aichi:external-test",
            "synthetic": False,
            "production_approved": False,
            "provenance": provenance,
        }
        require(actual_dataset == wanted_dataset, "dataset payload/provenance/content digest mismatch")
        require("BEGIN;" in sql and sql.rstrip().endswith("COMMIT;"), "seed is not one explicit transaction")
        require(f"'regulation_datasets:{DATASET_ID}'" in sql, "existing seed advisory-lock namespace mismatch")
        require("regulation_candidates" not in sql, "external seed must not create candidate rows")
        require("CREATE TEMP TABLE" in sql and "CREATE TABLE regulation_" not in sql,
                "seed must stage temporarily and must not create persistent regulation tables")
        statements = re.findall(r"(?im)^\s*INSERT\s+INTO\s+([a-z_][a-z0-9_]*)", sql)
        require(Counter(statements) == Counter({
            "external_seed_expected_points": 1, "regulation_datasets": 1, "regulation_points": 1,
        }), f"unexpected INSERT targets: {statements}")
        require(not re.search(r"(?im)^\s*(UPDATE|DELETE)\s+", sql), "seed must not update or delete rows")
        require("EXCEPT ALL" in sql and "read-back differs" in sql and "count mismatch" in sql,
                "SQL does not verify full count and row read-back")
        require("INSERT INTO regulation_points" in sql and "ST_SetSRID(ST_MakePoint(longitude, latitude), 4326)::geography" in sql,
                "seed does not insert geodetic points into the existing table")
        require("/opt/data/" not in sql, "private workspace path leaked into generated SQL")

        for path, label in ((args.manifest, "manifest"), (args.input_jsonl, "input JSONL")):
            require(path.exists(), f"missing {label}")
        inferred = sum(1 for point in actual_rows if point["direction_status"] == "inferred")
        unknown = len(actual_rows) - inferred
        print(json.dumps({
            "passed": True,
            "rows_reconciled": len(actual_rows),
            "teacher": EXPECTED_COUNTS["teacher"],
            "target": EXPECTED_COUNTS["target"],
            "inferred": inferred,
            "unknown": unknown,
            "candidates": 0,
            "confidence": None,
            "content_sha256": content_sha,
            "input_artifact_sha256": ARTIFACT_SHA256,
            "private_path_leak": False,
        }, sort_keys=True))
    except (OSError, ValueError, KeyError, TypeError, AssertionError, json.JSONDecodeError) as error:
        print(f"verify_generated_seed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
