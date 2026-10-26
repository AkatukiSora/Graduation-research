"""Validate the frozen review-only output; no database or inference dependencies."""

import hashlib
import json
import math
from pathlib import Path

BUNDLE = Path(__file__).resolve().parent / "c1r1-kansai-chubu-v1"
CONVENTION = "clockwise_from_north_vehicle_travel_toward_junction"
EXPECTED_COUNTS = {"teacher-directions.jsonl": (394, 346),
                   "target-directions.jsonl": (1939, 1859)}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def number(value, minimum, maximum, *, exclusive_max=False):
    return (type(value) in (int, float) and math.isfinite(value)
            and minimum <= value
            and (value < maximum if exclusive_max else value <= maximum))


def validate_row(row):
    require(isinstance(row, dict), "row must be an object")
    require(isinstance(row.get("entity_id"), str) and row["entity_id"], "entity_id")
    require(number(row.get("lon"), -180, 180), "longitude")
    require(number(row.get("lat"), -90, 90), "latitude")
    require(number(row.get("confidence"), 0, 1), "confidence")
    require(row.get("direction_convention") == CONVENTION, "direction convention")
    require(type(row.get("source_row")) is int and row["source_row"] > 0, "source row")
    require("direction_deg" in row, "missing direction")
    if row.get("status") == "inferred":
        require(number(row["direction_deg"], 0, 360, exclusive_max=True), "direction")
        require(row.get("confidence_calibrated") is False, "uncalibrated confidence")
        require(isinstance(row.get("confidence_method"), str)
                and row["confidence_method"], "confidence method")
    else:
        require(row.get("status") == "no_usable_road_candidate", "status")
        require(row["direction_deg"] is None and row["confidence"] == 0,
                "unavailable direction must be null with zero confidence")


def validate_bundle(bundle=BUNDLE):
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    require(manifest.get("dataset_id") == BUNDLE.name, "dataset id")
    require(manifest.get("purpose") == "review_only", "review-only purpose")
    require(manifest.get("production_approved") is False, "production must be unapproved")
    require(manifest.get("confidence_calibrated") is False, "uncalibrated manifest")
    require(manifest.get("direction_convention") == CONVENTION, "manifest convention")
    require(set(manifest.get("files", {})) == set(EXPECTED_COUNTS), "file inventory")
    seen = set()
    result = {}
    for filename, (count, inferred_count) in EXPECTED_COUNTS.items():
        spec = manifest["files"][filename]
        require((spec["rows"], spec["inferred"]) == (count, inferred_count), "expected counts")
        data = (bundle / filename).read_bytes()
        require(hashlib.sha256(data).hexdigest() == spec["sha256"], f"{filename}: SHA-256")
        rows = [json.loads(line) for line in data.decode("utf-8").splitlines()]
        require(len(rows) == count, f"{filename}: row count")
        inferred = 0
        for index, row in enumerate(rows, 1):
            try:
                validate_row(row)
                require(row["source_csv_sha256"] == spec["source_csv_sha256"], "source digest")
                require(row["entity_id"] not in seen, "duplicate entity_id")
            except (ValueError, KeyError) as error:
                raise ValueError(f"{filename}:{index}: {error}") from error
            seen.add(row["entity_id"])
            inferred += row["status"] == "inferred"
        require(inferred == inferred_count, f"{filename}: inferred count")
        result[filename] = {"rows": count, "inferred": inferred, "unavailable": count - inferred}
    return result


if __name__ == "__main__":
    print(json.dumps(validate_bundle(), sort_keys=True))
