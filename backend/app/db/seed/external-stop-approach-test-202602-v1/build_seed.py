#!/usr/bin/env python3
"""Build the immutable, test-only psql seed from a read-only JSONL artifact."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import Counter
from pathlib import Path
from typing import Any

DATASET_ID = "external-stop-approach-test-202602-v1"
ARTIFACT_SHA256 = "02eeb6998b5e926844448b4260d29c95f5b9a8a1b4216397789cbf1973493ebe"
METHOD_VERSION = "local-stop-approach-offline-v1"
SEMANTICS = "vehicle_approach_to_stop_estimate"
EXPECTED_ROWS = 2333
EXPECTED_STATUS = {"teacher": {"rows": 394, "inferred": 388, "unknown": 6},
                   "target": {"rows": 1939, "inferred": 1937, "unknown": 2}}
RECORD_KEYS = ("entity_id", "raw_uid", "longitude", "latitude", "direction_degrees",
               "direction_status", "validity_status", "normalization_status",
               "imputation_method", "imputation_reason", "direction_source",
               "direction_evidence", "validity_reason", "validity_evidence")


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                         allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def load_inputs(input_path: Path, manifest_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    artifact_bytes = input_path.read_bytes()
    artifact_sha = hashlib.sha256(artifact_bytes).hexdigest()
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    if artifact_sha != ARTIFACT_SHA256 or manifest.get("data_sha256") != ARTIFACT_SHA256:
        raise ValueError("input artifact SHA-256 does not match the approved manifest digest")
    if manifest.get("dataset_id") != DATASET_ID or manifest.get("rows") != EXPECTED_ROWS:
        raise ValueError("manifest dataset identity or row count mismatch")
    if manifest.get("source_semantics") != SEMANTICS or manifest.get("confidence_semantics") != "not_provided":
        raise ValueError("manifest direction or confidence semantics mismatch")
    if manifest.get("production_approved") is not False:
        raise ValueError("manifest must keep production_approved=false")

    rows: list[dict[str, Any]] = []
    counts: dict[str, Counter[str]] = {"teacher": Counter(), "target": Counter()}
    seen_entity: set[str] = set()
    seen_uid: set[str] = set()
    expected_input_keys = {"entity_id", "raw_uid", "subset", "lat", "lon", "direction_degrees",
                           "source", "confidence", "error"}
    for line_number, line in enumerate(artifact_bytes.decode("utf-8").splitlines(), 1):
        try:
            source = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid JSON on line {line_number}: {error}") from error
        if not isinstance(source, dict) or set(source) != expected_input_keys:
            raise ValueError(f"unexpected input fields on line {line_number}")
        entity_id, raw_uid, subset = source["entity_id"], source["raw_uid"], source["subset"]
        if not isinstance(entity_id, str) or not entity_id or not isinstance(raw_uid, str) or not raw_uid or len(raw_uid) > 32:
            raise ValueError(f"invalid entity_id/raw_uid on line {line_number}")
        if subset not in counts or entity_id in seen_entity or raw_uid in seen_uid:
            raise ValueError(f"invalid subset or duplicate ID on line {line_number}")
        if source["source"] != "external_local_stop_approach" or source["confidence"] is not None:
            raise ValueError(f"source/confidence mismatch on line {line_number}")
        if source["error"] is not None and not isinstance(source["error"], str):
            raise ValueError(f"invalid error field on line {line_number}")
        lat, lon = source["lat"], source["lon"]
        if not finite_number(lat) or not -90 <= lat <= 90 or not finite_number(lon) or not -180 <= lon <= 180:
            raise ValueError(f"invalid coordinates on line {line_number}")
        degree = source["direction_degrees"]
        if degree is None:
            status = "unknown"
            if source["error"] is None:
                raise ValueError(f"unknown direction must retain a source abstention on line {line_number}")
            counts[subset]["unknown"] += 1
            method, reason, direction_source, evidence = "none", "topology_only_unverified", "unknown", {}
        else:
            if not finite_number(degree) or not 0 <= degree < 360 or source["error"] is not None:
                raise ValueError(f"invalid inferred direction on line {line_number}")
            status = "inferred"
            counts[subset]["inferred"] += 1
            method = direction_source = "external_local_stop_approach"
            reason = "model_direction_inference"
            evidence = {
                "schema_version": 1,
                "kind": "external_local_stop_approach",
                "source_csv_sha256": manifest["provenance"]["candidate_reference"]["files"][subset]["source_csv_sha256"],
                "method_version": METHOD_VERSION,
                "direction_semantics": SEMANTICS,
                "artifact_sha256": ARTIFACT_SHA256,
                "record_ref": entity_id,
                "subset": subset,
                "confidence": None,
            }
        seen_entity.add(entity_id)
        seen_uid.add(raw_uid)
        counts[subset]["rows"] += 1
        rows.append({
            "entity_id": entity_id,
            "raw_uid": raw_uid,
            "longitude": lon,
            "latitude": lat,
            "direction_degrees": degree,
            "direction_status": status,
            "validity_status": "unknown",
            "normalization_status": "valid_candidate_only",
            "imputation_method": method,
            "imputation_reason": reason,
            "direction_source": direction_source,
            "direction_evidence": evidence,
            "validity_reason": "source_validity_unverified",
            "validity_evidence": {},
        })

    if len(rows) != EXPECTED_ROWS:
        raise ValueError(f"input rows={len(rows)}, want {EXPECTED_ROWS}")
    for subset, want in EXPECTED_STATUS.items():
        got = dict(counts[subset])
        if got != want:
            raise ValueError(f"{subset} counts={got}, want {want}")
    expected_counts = manifest["counts"]
    if expected_counts != EXPECTED_STATUS:
        raise ValueError("manifest subset counts do not match the approved counts")

    p = manifest["provenance"]
    csv_files = p["candidate_reference"]["files"]
    pbf_inputs = p["osm_inputs"]
    implementation_hashes = p["external_source"]["python_source_sha256"]
    if p["external_source"].get("redistributed") is not False:
        raise ValueError("external implementation source must not be redistributed")
    provenance = {
        "data_purpose": "direction_test",
        "source": "external_local_stop_approach",
        "direction_semantics": SEMANTICS,
        "method_version": METHOD_VERSION,
        "confidence": None,
        "confidence_semantics": "not_provided",
        "input_artifact_sha256": ARTIFACT_SHA256,
        "input_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "input_rows": EXPECTED_ROWS,
        "subsets": {
            subset: {
                "rows": EXPECTED_STATUS[subset]["rows"],
                "inferred": EXPECTED_STATUS[subset]["inferred"],
                "unknown": EXPECTED_STATUS[subset]["unknown"],
                "source_csv_sha256": csv_files[subset]["source_csv_sha256"],
                "source_pbf_sha256": pbf_inputs[subset]["sha256"],
            }
            for subset in ("teacher", "target")
        },
        "external_source": {
            "archive_sha1": p["external_source"]["archive_sha1"],
            "implementation_source_sha256": implementation_hashes,
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
    if any(isinstance(value, str) and "/opt/data/" in value for value in provenance.values()):
        raise ValueError("private environment paths must not enter public provenance")
    return provenance, rows, hashlib.sha256(manifest_bytes).hexdigest()


def sql_dollar(value: Any, tag: str) -> str:
    marker = f"${tag}$"
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
    if marker in text:
        raise ValueError(f"SQL dollar quote marker collision: {tag}")
    return marker + text + marker


def build_sql(provenance: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    content = {
        "dataset_id": DATASET_ID,
        "source_month": "202602",
        "region_id": "jp:shiga-aichi:external-test",
        "synthetic": False,
        "production_approved": False,
        "provenance": provenance,
        "points": rows,
    }
    content_sha = canonical_sha256(content)
    dataset = {key: content[key] for key in
               ("dataset_id", "source_month", "region_id", "synthetic", "production_approved", "provenance")}
    dataset["content_sha256"] = content_sha
    prefix = f"""-- Test-only immutable seed. Requires the append-only migration first.
-- Run with: psql -v ON_ERROR_STOP=1 -f seed.sql against a disposable local database.
-- Rebuild with build_seed.py; the source JSONL and manifest are not copied here.
BEGIN;

SELECT pg_advisory_xact_lock(hashtextextended('regulation_datasets:{DATASET_ID}', 0));

CREATE TEMP TABLE external_seed_expected_points (
    entity_id text PRIMARY KEY,
    raw_uid varchar(32) NOT NULL,
    longitude double precision NOT NULL,
    latitude double precision NOT NULL,
    direction_degrees double precision,
    direction_status text NOT NULL,
    validity_status text NOT NULL,
    normalization_status text NOT NULL,
    imputation_method text NOT NULL,
    imputation_reason text NOT NULL,
    direction_source text NOT NULL,
    direction_evidence jsonb NOT NULL,
    validity_reason text NOT NULL,
    validity_evidence jsonb NOT NULL
) ON COMMIT DROP;

INSERT INTO external_seed_expected_points ({', '.join(RECORD_KEYS)})
SELECT entity_id, raw_uid, longitude, latitude, direction_degrees,
       direction_status, validity_status, normalization_status,
       imputation_method, imputation_reason, direction_source, direction_evidence,
       validity_reason, validity_evidence
FROM jsonb_to_recordset(
{sql_dollar(rows, 'external_seed_points')}::jsonb
) AS p(
    entity_id text, raw_uid varchar(32), longitude double precision, latitude double precision,
    direction_degrees double precision, direction_status text, validity_status text,
    normalization_status text, imputation_method text, imputation_reason text,
    direction_source text, direction_evidence jsonb, validity_reason text, validity_evidence jsonb
);

DO $external_seed_apply$
DECLARE
    expected_count integer := {len(rows)};
    expected_dataset jsonb := {sql_dollar(dataset, 'external_seed_dataset_check')}::jsonb;
BEGIN
    IF (SELECT count(*) FROM external_seed_expected_points) <> expected_count THEN
        RAISE EXCEPTION 'external seed staging row count mismatch';
    END IF;
    IF EXISTS (
        SELECT 1 FROM regulation_datasets
        WHERE dataset_id = expected_dataset->>'dataset_id'
          AND content_sha256 IS DISTINCT FROM expected_dataset->>'content_sha256'
    ) THEN
        RAISE EXCEPTION 'external seed dataset ID already has a different digest';
    END IF;
    IF EXISTS (
        SELECT 1 FROM regulation_datasets
        WHERE content_sha256 = expected_dataset->>'content_sha256'
          AND dataset_id <> expected_dataset->>'dataset_id'
    ) THEN
        RAISE EXCEPTION 'external seed digest already belongs to another dataset ID';
    END IF;
    IF EXISTS (
        SELECT 1 FROM regulation_datasets
        WHERE dataset_id = expected_dataset->>'dataset_id'
          AND (content_sha256 IS DISTINCT FROM expected_dataset->>'content_sha256'
            OR source_month IS DISTINCT FROM expected_dataset->>'source_month'
            OR region_id IS DISTINCT FROM expected_dataset->>'region_id'
            OR synthetic IS DISTINCT FROM (expected_dataset->>'synthetic')::boolean
            OR production_approved IS DISTINCT FROM false
            OR provenance IS DISTINCT FROM expected_dataset->'provenance')
    ) THEN
        RAISE EXCEPTION 'external seed dataset metadata differs; refusing to overwrite';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM regulation_datasets WHERE dataset_id = expected_dataset->>'dataset_id'
    ) THEN
        INSERT INTO regulation_datasets
            (dataset_id, content_sha256, source_month, region_id, synthetic, production_approved, provenance)
        SELECT dataset_id, content_sha256, source_month, region_id, synthetic, production_approved, provenance
        FROM jsonb_to_record(expected_dataset) AS d(
            dataset_id text, content_sha256 text, source_month text, region_id text,
            synthetic boolean, production_approved boolean, provenance jsonb
        );
        INSERT INTO regulation_points (
            dataset_id, entity_id, raw_uid, geom, direction_degrees, direction_status,
            validity_status, normalization_status, imputation_method, imputation_reason,
            direction_source, direction_evidence, validity_reason, validity_evidence
        )
        SELECT expected_dataset->>'dataset_id', entity_id, raw_uid,
               ST_SetSRID(ST_MakePoint(longitude, latitude), 4326)::geography,
               direction_degrees, direction_status, validity_status, normalization_status,
               imputation_method, imputation_reason, direction_source, direction_evidence,
               validity_reason, validity_evidence
        FROM external_seed_expected_points
        ORDER BY entity_id;
    END IF;

    IF (SELECT count(*) FROM regulation_datasets
        WHERE dataset_id = expected_dataset->>'dataset_id') <> 1 THEN
        RAISE EXCEPTION 'external seed dataset read-back count mismatch';
    END IF;
    IF (SELECT count(*) FROM regulation_points
        WHERE dataset_id = expected_dataset->>'dataset_id') <> expected_count THEN
        RAISE EXCEPTION 'external seed point read-back count mismatch';
    END IF;
    IF EXISTS (
        (SELECT entity_id, raw_uid, ST_X(geom::geometry)::double precision AS longitude,
                ST_Y(geom::geometry)::double precision AS latitude, direction_degrees,
                direction_status, validity_status, normalization_status, imputation_method,
                imputation_reason, direction_source, direction_evidence, validity_reason, validity_evidence
         FROM regulation_points WHERE dataset_id = expected_dataset->>'dataset_id'
         EXCEPT ALL
         SELECT entity_id, raw_uid, longitude, latitude, direction_degrees,
                direction_status, validity_status, normalization_status, imputation_method,
                imputation_reason, direction_source, direction_evidence, validity_reason, validity_evidence
         FROM external_seed_expected_points)
        UNION ALL
        (SELECT entity_id, raw_uid, longitude, latitude, direction_degrees,
                direction_status, validity_status, normalization_status, imputation_method,
                imputation_reason, direction_source, direction_evidence, validity_reason, validity_evidence
         FROM external_seed_expected_points
         EXCEPT ALL
         SELECT entity_id, raw_uid, ST_X(geom::geometry)::double precision AS longitude,
                ST_Y(geom::geometry)::double precision AS latitude, direction_degrees,
                direction_status, validity_status, normalization_status, imputation_method,
                imputation_reason, direction_source, direction_evidence, validity_reason, validity_evidence
         FROM regulation_points WHERE dataset_id = expected_dataset->>'dataset_id')
    ) THEN
        RAISE EXCEPTION 'external seed point read-back differs; refusing to overwrite';
    END IF;
END
$external_seed_apply$;

COMMIT;
"""
    return prefix


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-jsonl", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--check", action="store_true", help="compare generated SQL with the existing file")
    args = parser.parse_args()
    try:
        provenance, rows, _ = load_inputs(args.input_jsonl, args.manifest)
        generated = build_sql(provenance, rows)
        if args.check:
            if not args.output.is_file() or args.output.read_text(encoding="utf-8") != generated:
                raise ValueError("generated SQL differs from the checked-in seed.sql")
            print(f"seed SQL reproducible: {len(rows)} points, SHA-256 {hashlib.sha256(generated.encode()).hexdigest()}")
        else:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(generated, encoding="utf-8", newline="\n")
            print(f"wrote {args.output}: {len(rows)} points, SHA-256 {hashlib.sha256(generated.encode()).hexdigest()}")
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        print(f"build_seed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
