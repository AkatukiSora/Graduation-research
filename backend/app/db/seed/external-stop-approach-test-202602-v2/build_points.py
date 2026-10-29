#!/usr/bin/env python3
"""Convert result_local.json into points.csv.gz (uid,lat,lon,az) for seed.sql."""
import argparse
import csv
import gzip
import hashlib
import io
import json
import math
import sys
from pathlib import Path

SOURCE_SHA256 = "0c19ade31372def466d7faa18568a367814be3434379916f4cb1a5cf62a6dbe7"
EXPECTED_ROWS = 168652


def render(source: bytes) -> bytes:
    digest = hashlib.sha256(source).hexdigest()
    if digest != SOURCE_SHA256:
        raise SystemExit(f"result_local.json SHA-256 is {digest}, want {SOURCE_SHA256}")
    records = json.loads(source)
    if len(records) != EXPECTED_ROWS:
        raise SystemExit(f"{len(records)} records, want {EXPECTED_ROWS}")
    seen = set()
    out = io.StringIO(newline="")
    writer = csv.writer(out, lineterminator="\n")
    for r in sorted(records, key=lambda r: r["uniqueKey"]):
        uid, lat, lon, az = r["uniqueKey"], r["lat"], r["lon"], r["az"]
        if not uid or len(uid) > 32 or uid in seen:
            raise SystemExit(f"invalid or duplicate uniqueKey {uid!r}")
        if not (math.isfinite(lat) and -90 <= lat <= 90 and math.isfinite(lon) and -180 <= lon <= 180):
            raise SystemExit(f"invalid coordinates for {uid}")
        if az is not None and not 0 <= az < 360:
            raise SystemExit(f"invalid az for {uid}")
        seen.add(uid)
        writer.writerow([uid, lat, lon, "" if az is None else az])
    return out.getvalue().encode()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="result_local.json")
    parser.add_argument("--output", required=True, type=Path, help="points.csv.gz")
    parser.add_argument("--check", action="store_true", help="compare with the existing output instead of writing")
    args = parser.parse_args()
    csv_bytes = render(args.input.read_bytes())
    if args.check:
        if gzip.decompress(args.output.read_bytes()) != csv_bytes:
            sys.exit("points.csv.gz differs from result_local.json")
        print(f"points.csv.gz matches: {csv_bytes.count(10)} rows")
        return
    with args.output.open("wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0, filename="") as gz:
        gz.write(csv_bytes)
    print(f"wrote {args.output}: {csv_bytes.count(10)} rows")


if __name__ == "__main__":
    main()
