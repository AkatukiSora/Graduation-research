#!/usr/bin/env python3
"""Validate the regulation dataset migration against a disposable local PostGIS.

The script starts a fresh PostgreSQL cluster in a temporary directory, applies the
repository migrations in order, checks the resulting schema and constraint
behaviour, then stops and removes the cluster. It never reads application
configuration and never connects to a remote database.
"""
import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[3]
MIGRATIONS = sorted((APP_DIR / "db" / "migrations").glob("*.sql"))
DIGEST = "a" * 64


class Cluster:
    def __init__(self, report_path: Path):
        base = report_path.resolve().parent
        root = base / "postgres" / "root"
        self.bin = root / "usr" / "lib" / "postgresql" / "17" / "bin"
        self.share = root / "usr" / "share" / "postgresql" / "17"
        self.env = dict(
            os.environ,
            LD_LIBRARY_PATH=str(root / "usr" / "lib" / "x86_64-linux-gnu"),
            PROJ_DATA=str(root / "usr" / "share" / "proj"),
        )
        self.tmp = Path(tempfile.mkdtemp(prefix="regulation-schema-", dir=base))
        self.data = self.tmp / "data"
        self.sock = self.tmp / "socket"
        self.sock.mkdir(mode=0o700)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.started = False

    def run(self, program, *args, check=True, stdin=None):
        result = subprocess.run(
            [str(self.bin / program), *map(str, args)],
            env=self.env, input=stdin, capture_output=True, text=True, timeout=120,
        )
        if check and result.returncode:
            raise RuntimeError(f"{program} failed: {result.stdout}{result.stderr}")
        return result

    def start(self):
        self.run("initdb", "-D", self.data, "-L", self.share, "--username=validator",
                 "--auth-local=trust", "--auth-host=reject", "--no-locale", "--encoding=UTF8")
        with (self.data / "postgresql.conf").open("a") as conf:
            conf.write(f"\nlisten_addresses = '127.0.0.1'\nport = {self.port}\n"
                       f"unix_socket_directories = '{self.sock}'\nmax_connections = 10\n")
        self.run("pg_ctl", "-D", self.data, "-l", self.tmp / "postgres.log", "-w", "-t", 30, "start")
        self.started = True

    def stop(self):
        if self.started:
            self.run("pg_ctl", "-D", self.data, "-m", "fast", "-w", "-t", 30, "stop", check=False)
            self.started = False
        pid_file = self.data / "postmaster.pid"
        stopped = not pid_file.exists()
        shutil.rmtree(self.tmp, ignore_errors=True)
        return stopped

    def sql(self, statement, database="gr9_dev", file=None):
        args = ["-h", self.sock, "-p", self.port, "-U", "validator", "-d", database,
                "-v", "ON_ERROR_STOP=1", "-At"]
        args += ["-f", file] if file else ["-c", statement]
        return self.run("psql", *args, check=False)


def expect_ok(db, statement):
    result = db.sql(statement)
    assert result.returncode == 0, f"expected success: {statement}\n{result.stderr}"
    return result.stdout.strip()


def expect_error(db, statement, needle):
    result = db.sql(statement)
    assert result.returncode != 0, f"expected rejection: {statement}"
    assert needle in result.stderr, f"expected {needle!r} in:\n{result.stderr}"


def insert_dataset(dataset_id="ds1", digest=DIGEST, month="202602", synthetic="true", approved="false"):
    return ("INSERT INTO regulation_datasets (dataset_id, content_sha256, source_month, region_id,"
            " synthetic, production_approved, provenance) VALUES "
            f"('{dataset_id}', '{digest}', '{month}', 'nagoya', {synthetic}, {approved}, '{{}}'::jsonb);")


def insert_point(entity, uid, direction="NULL", direction_status="unknown", validity="unknown",
                 lon=136.9, lat=35.1, dataset="ds1"):
    return ("INSERT INTO regulation_points (dataset_id, entity_id, raw_uid, geom, direction_degrees,"
            " direction_status, validity_status, normalization_status) VALUES "
            f"('{dataset}', '{entity}', '{uid}', ST_SetSRID(ST_MakePoint({lon},{lat}),4326)::geography,"
            f" {direction}, '{direction_status}', '{validity}', 'normalized');")


def insert_candidate(source, neighbor, rank: int = 1, distance: "float | str" = 10.0, bearing: "float | str" = 45.0, dataset="ds1",
                     semantics="candidate_point_neighbor_only", approved="false"):
    return ("INSERT INTO regulation_candidates (dataset_id, source_entity_id, neighbor_entity_id, rank,"
            " distance_m, candidate_bearing, features, bearing_semantics, production_approved) VALUES "
            f"('{dataset}', '{source}', '{neighbor}', {rank}, {distance}, {bearing}, '{{}}'::jsonb,"
            f" '{semantics}', {approved});")


def validate(db):
    # Baseline objects are untouched and still usable.
    assert expect_ok(db, "SELECT count(*) FROM stop_signs") == "0"
    tables = expect_ok(db, "SELECT string_agg(table_name, ',' ORDER BY table_name) FROM information_schema.tables"
                           " WHERE table_schema='public' AND table_name LIKE 'regulation_%'")
    assert tables == "regulation_candidates,regulation_datasets,regulation_points", tables
    index = expect_ok(db, "SELECT indexdef FROM pg_indexes WHERE indexname='idx_regulation_points_geom'")
    assert "USING gist" in index, index
    privilege = expect_ok(db, "SELECT bool_and(has_table_privilege('gr9app', 'public.'||t, 'SELECT')) FROM"
                              " unnest(ARRAY['regulation_datasets','regulation_points','regulation_candidates']) t")
    assert privilege == "t", privilege

    expect_ok(db, insert_dataset())
    expect_error(db, insert_dataset("ds2", digest=DIGEST), "regulation_datasets_content_sha256_key")
    expect_error(db, insert_dataset("ds3", digest="A" * 64), "regulation_datasets_digest_format")
    expect_error(db, insert_dataset("ds3", digest="b" * 63), "regulation_datasets_digest_format")
    expect_error(db, insert_dataset("ds3", digest="c" * 64, month="202613"), "regulation_datasets_source_month_format")
    expect_error(db, insert_dataset("ds3", digest="c" * 64, month="2026-2"), "regulation_datasets_source_month_format")
    expect_error(db, insert_dataset("ds3", digest="c" * 64, approved="true"), "regulation_datasets_synthetic_not_approved")
    expect_error(db, insert_dataset("ds1", digest="d" * 64), "regulation_datasets_pkey")

    expect_ok(db, insert_point("p1", "uid1"))
    expect_ok(db, insert_point("p2", "uid2", lon=136.901))
    expect_ok(db, insert_point("p3", "uid3", direction="90", direction_status="inferred", lon=136.902))
    expect_error(db, insert_point("p1", "uid9"), "regulation_points_pkey")
    expect_error(db, insert_point("p9", "uid1"), "regulation_points_dataset_id_raw_uid_key")
    expect_error(db, insert_point("p9", "uid9", dataset="missing"), "regulation_points_dataset_id_fkey")
    expect_error(db, insert_point("p9", "uid9", direction="10"), "regulation_points_direction_consistency")
    expect_error(db, insert_point("p9", "uid9", direction_status="inferred"), "regulation_points_direction_consistency")
    expect_error(db, insert_point("p9", "uid9", direction="360", direction_status="confirmed"), "regulation_points_direction_range")
    expect_error(db, insert_point("p9", "uid9", direction="-1", direction_status="confirmed"), "regulation_points_direction_range")
    expect_error(db, insert_point("p9", "uid9", direction_status="bogus"), "regulation_points_direction_status_values")
    expect_error(db, insert_point("p9", "uid9", validity="bogus"), "regulation_points_validity_status_values")
    expect_error(db, "INSERT INTO regulation_points (dataset_id, entity_id, raw_uid, direction_status,"
                     " validity_status, normalization_status) VALUES ('ds1','p9','uid9','unknown','unknown','normalized')",
                 "geom")
    plan = expect_ok(db, "SET enable_seqscan=off; EXPLAIN SELECT entity_id FROM regulation_points WHERE"
                         " ST_DWithin(geom, ST_SetSRID(ST_MakePoint(136.9,35.1),4326)::geography, 500)")
    assert "idx_regulation_points_geom" in plan, plan
    near = expect_ok(db, "SELECT count(*) FROM regulation_points WHERE ST_DWithin(geom,"
                         " ST_SetSRID(ST_MakePoint(136.9,35.1),4326)::geography, 500)")
    assert near == "3", near

    expect_ok(db, insert_candidate("p1", "p2"))
    expect_ok(db, insert_candidate("p1", "p3", rank=2, bearing=0))
    expect_error(db, insert_candidate("p1", "p3", rank=1), "regulation_candidates_pkey")
    expect_error(db, insert_candidate("p1", "p2", rank=3), "regulation_candidates_neighbor_unique")
    expect_error(db, insert_candidate("p9", "p2", rank=3), "regulation_candidates_source_fkey")
    expect_error(db, insert_candidate("p1", "p9", rank=3), "regulation_candidates_neighbor_fkey")
    expect_error(db, insert_candidate("p1", "p1", rank=3), "regulation_candidates_not_self")
    expect_error(db, insert_candidate("p2", "p1", rank=0), "regulation_candidates_rank_positive")
    expect_error(db, insert_candidate("p2", "p1", distance=-1), "regulation_candidates_distance_nonnegative")
    expect_error(db, insert_candidate("p2", "p1", distance="'NaN'"), "regulation_candidates_distance_nonnegative")
    expect_error(db, insert_candidate("p2", "p1", bearing=360), "regulation_candidates_bearing_range")
    expect_error(db, insert_candidate("p2", "p1", bearing=-0.5), "regulation_candidates_bearing_range")
    expect_error(db, insert_candidate("p2", "p1", bearing="'NaN'"), "regulation_candidates_bearing_range")
    expect_error(db, insert_candidate("p2", "p1", semantics="official_direction"), "regulation_candidates_bearing_semantics")
    expect_error(db, insert_candidate("p2", "p1", approved="true"), "regulation_candidates_not_approved")
    # Referenced points cannot be removed while candidates use them.
    expect_error(db, "DELETE FROM regulation_points WHERE entity_id='p2'", "regulation_candidates_neighbor_fkey")
    # Unknown direction stays unknown; the candidate bearing never writes official direction.
    assert expect_ok(db, "SELECT count(*) FROM regulation_points WHERE direction_status='unknown'"
                         " AND direction_degrees IS NULL") == "2"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--toolchain-report", required=True, type=Path)
    args = parser.parse_args()
    report = json.loads(args.toolchain_report.read_text())
    if report.get("status") != "validated_stopped":
        sys.exit("toolchain report is not a validated local toolchain")
    db = Cluster(args.toolchain_report)
    ok = False
    try:
        db.start()
        for setup in ("CREATE ROLE rds_iam NOLOGIN", "CREATE DATABASE gr9_dev"):
            created = db.sql(setup, database="postgres")
            assert created.returncode == 0, created.stderr
        for migration in MIGRATIONS:
            applied = db.sql("", file=str(migration))
            assert applied.returncode == 0, f"{migration.name}\n{applied.stderr}"
        names = [m.name for m in MIGRATIONS]
        assert names[0] == "202606160001_init_stop_signs.sql", names
        assert len(names) >= 2, "regulation dataset migration is missing"
        validate(db)
        ok = True
    finally:
        stopped = db.stop()
    print(json.dumps({"passed": ok, "database_stopped": stopped, "migrations": [m.name for m in MIGRATIONS]}))
    sys.exit(0 if ok and stopped else 1)


if __name__ == "__main__":
    main()
