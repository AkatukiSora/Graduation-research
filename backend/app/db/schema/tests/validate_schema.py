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
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[3]
MIGRATIONS = sorted((APP_DIR / "db" / "migrations").glob("*.sql"))
DIGEST = "a" * 64
SQL_NULL = object()


class Cluster:
    def __init__(self, report_path: Path, scratch_dir: Path | None = None):
        toolchain_root = report_path.resolve().parent
        postgres_root = toolchain_root / "postgres" / "root"
        self.bin = postgres_root / "usr" / "lib" / "postgresql" / "17" / "bin"
        self.share = postgres_root / "usr" / "share" / "postgresql" / "17"
        if scratch_dir is None:
            scratch_dir = APP_DIR.parents[1] / "validation"
        scratch_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.tmp = Path(tempfile.mkdtemp(prefix="regulation-schema-", dir=scratch_dir))
        self.data = self.tmp / "data"
        self.sock = self.tmp / "socket"
        self.sock.mkdir(mode=0o700)
        self.env = {
            "HOME": os.environ.get("HOME", "/opt/data/home"),
            "PATH": "/usr/bin:/bin",
            "LD_LIBRARY_PATH": str(postgres_root / "usr" / "lib" / "x86_64-linux-gnu"),
            "PROJ_DATA": str(postgres_root / "usr" / "share" / "proj"),
        }
        self.port = 5432
        self.started = False
        self.stop_returncode = None

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

    def allow_loopback_trust(self, database):
        hba = self.data / "pg_hba.conf"
        hba.write_text(f"host {database} validator 127.0.0.1/32 trust\n" + hba.read_text())
        self.run("pg_ctl", "-D", self.data, "reload")

    def stop(self):
        if self.started:
            result = self.run("pg_ctl", "-D", self.data, "-m", "fast", "-w", "-t", 30, "stop", check=False)
            self.stop_returncode = result.returncode
            self.started = False
        pid_file = self.data / "postmaster.pid"
        stopped = self.stop_returncode == 0 and not pid_file.exists()
        if stopped:
            shutil.rmtree(self.tmp, ignore_errors=True)
        return stopped

    def sql(self, statement, database="gr9_dev", file=None):
        args = ["-h", self.sock, "-p", self.port, "-U", "validator", "-d", database,
                "-v", "ON_ERROR_STOP=1", "-At"]
        args += ["-f", file] if file else ["-c", statement]
        return self.run("psql", *args, check=False)


def expect_ok(db, statement, database="gr9_dev"):
    result = db.sql(statement, database=database)
    assert result.returncode == 0, f"expected success: {statement}\n{result.stderr}"
    return result.stdout.strip()


def expect_error(db, statement, needle, database="gr9_dev"):
    result = db.sql(statement, database=database)
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
    expect_ok(db, insert_point("p4", "uid4", direction="180", direction_status="confirmed", lon=136.903))
    # Free-form provenance and evidence columns are not constrained.
    expect_ok(db, "UPDATE regulation_points SET imputation_method='anything', direction_source='anything',"
                  " direction_evidence='[1]'::jsonb, validity_evidence='\"x\"'::jsonb WHERE entity_id='p3'")
    expect_error(db, insert_point("p1", "uid9"), "regulation_points_pkey")
    expect_error(db, insert_point("p9", "uid1"), "regulation_points_dataset_id_raw_uid_key")
    expect_error(db, insert_point("p9", "uid9", dataset="missing"), "regulation_points_dataset_id_fkey")
    expect_error(db, insert_point("p9", "uid9", direction="10"), "regulation_points_direction_status_degrees")
    expect_error(db, insert_point("p9", "uid9", direction_status="inferred"), "regulation_points_direction_status_degrees")
    expect_error(db, insert_point("p9", "uid9", direction="360", direction_status="confirmed"), "regulation_points_direction_range")
    expect_error(db, insert_point("p9", "uid9", direction="-1", direction_status="confirmed"), "regulation_points_direction_range")
    expect_error(db, insert_point("p9", "uid9", direction="10", direction_status="bogus"), "regulation_points_direction_status_values")
    expect_error(db, insert_point("p9", "uid9", validity="bogus"), "regulation_points_validity_status_values")
    expect_error(db, "INSERT INTO regulation_points (dataset_id, entity_id, raw_uid, direction_status,"
                     " validity_status, normalization_status) VALUES ('ds1','p9','uid9','unknown','unknown','normalized')",
                 "geom")
    plan = expect_ok(db, "SET enable_seqscan=off; EXPLAIN SELECT entity_id FROM regulation_points WHERE"
                         " ST_DWithin(geom, ST_SetSRID(ST_MakePoint(136.9,35.1),4326)::geography, 500)")
    assert "idx_regulation_points_geom" in plan, plan
    near = expect_ok(db, "SELECT count(*) FROM regulation_points WHERE ST_DWithin(geom,"
                         " ST_SetSRID(ST_MakePoint(136.9,35.1),4326)::geography, 500)")
    assert near == "4", near

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
    assert expect_ok(db, "SELECT count(*) FROM regulation_points WHERE entity_id IN ('p1','p2')"
                         " AND direction_status='unknown' AND direction_degrees IS NULL") == "2"


def apply_migrations(db, migrations, database="gr9_dev"):
    for migration in migrations:
        applied = db.sql("", database=database, file=str(migration))
        assert applied.returncode == 0, f"{migration.name}\n{applied.stdout}{applied.stderr}"


def validate_upgrade(db, migrations, report):
    s1 = next(migration for migration in migrations if migration.name == "202610140001_regulation_point_evidence.sql")
    later = [migration for migration in migrations if migration.name > s1.name]
    before_s1 = [migration for migration in migrations if migration.name < s1.name]
    assert before_s1 and before_s1[-1].name == "202610130001_regulation_datasets.sql", [m.name for m in before_s1]
    apply_migrations(db, before_s1, database="gr9_upgrade")
    db.allow_loopback_trust("gr9_upgrade")

    seed_env = {
        "HOME": "/opt/data/home", "PATH": "/usr/bin:/bin",
        "GOTOOLCHAIN": report["environment"]["GOTOOLCHAIN"],
        "GOCACHE": report["environment"]["GOCACHE"],
        "GOPATH": report["environment"]["GOPATH"],
        "AWS_REGION": "ap-northeast-3", "DB_HOST": "127.0.0.1", "DB_PORT": str(db.port),
        "DB_NAME": "gr9_upgrade", "DB_USER": "validator", "DB_PASSWORD": "local-disposable-test-only",
        "DB_IAM_AUTH": "false", "DB_SSL_MODE": "disable",
    }
    seed_command = [report["go_binary"], "run", "./cmd/seed", "--apply", "--dataset", "synthetic-v1"]
    seeded = subprocess.run(seed_command, cwd=APP_DIR, env=seed_env, capture_output=True,
                            text=True, timeout=240)
    assert seeded.returncode == 0, f"synthetic-v1 seed failed: {seeded.stdout}{seeded.stderr}"
    seed_result = json.loads(seeded.stdout.strip().splitlines()[-1])
    assert (seed_result["dataset_id"], seed_result["status"], seed_result["points"], seed_result["candidates"]) == (
        "synthetic-v1", "inserted", 12, 58,
    ), seed_result

    expect_ok(db, insert_dataset("legacy-synthetic", digest="b" * 64, synthetic="true"), database="gr9_upgrade")
    expect_ok(db, insert_point("legacy-unknown", "legacy-u", dataset="legacy-synthetic"), database="gr9_upgrade")
    expect_ok(db, insert_point("legacy-known", "legacy-k", direction="123.5", direction_status="inferred",
                               validity="valid", dataset="legacy-synthetic"), database="gr9_upgrade")
    expect_ok(db, insert_dataset("legacy-source", digest="c" * 64, synthetic="false"), database="gr9_upgrade")
    expect_ok(db, insert_point("source-unknown", "source-u", dataset="legacy-source"), database="gr9_upgrade")
    expect_ok(db, insert_point("source-confirmed", "source-c", direction="77", direction_status="confirmed",
                               dataset="legacy-source", lon=137.0, lat=35.2), database="gr9_upgrade")

    apply_migrations(db, [s1], database="gr9_upgrade")
    dataset_snapshot = expect_ok(db, "SELECT jsonb_agg(to_jsonb(d) ORDER BY dataset_id)::text FROM regulation_datasets d",
                                 database="gr9_upgrade")
    point_snapshot_query = (
        "SELECT jsonb_agg(jsonb_build_object("
        "'dataset_id', dataset_id, 'entity_id', entity_id, 'raw_uid', raw_uid,"
        "'geom_ewkb', encode(ST_AsEWKB(geom::geometry), 'hex'), 'direction_degrees', direction_degrees,"
        "'direction_status', direction_status, 'validity_status', validity_status,"
        "'normalization_status', normalization_status, 'created_at', created_at, 'updated_at', updated_at,"
        "'imputation_method', imputation_method, 'imputation_reason', imputation_reason,"
        "'direction_source', direction_source, 'direction_evidence', direction_evidence,"
        "'validity_reason', validity_reason, 'validity_evidence', validity_evidence)"
        " ORDER BY dataset_id, entity_id)::text FROM regulation_points"
    )
    points_snapshot = expect_ok(db, point_snapshot_query, database="gr9_upgrade")
    candidates_snapshot = expect_ok(db, "SELECT jsonb_agg(to_jsonb(c) ORDER BY dataset_id, source_entity_id, rank)::text "
                                       "FROM regulation_candidates c", database="gr9_upgrade")

    apply_migrations(db, later, database="gr9_upgrade")
    assert dataset_snapshot == expect_ok(db, "SELECT jsonb_agg(to_jsonb(d) ORDER BY dataset_id)::text FROM regulation_datasets d",
                                         database="gr9_upgrade"), "dataset rows changed during later migrations"
    assert points_snapshot == expect_ok(db, point_snapshot_query, database="gr9_upgrade"), "point rows changed during later migrations"
    assert candidates_snapshot == expect_ok(db, "SELECT jsonb_agg(to_jsonb(c) ORDER BY dataset_id, source_entity_id, rank)::text "
                                               "FROM regulation_candidates c", database="gr9_upgrade"), \
        "candidate rows changed during later migrations"

    expect_error(db, insert_point("bad-unknown", "bad-u", direction="10", dataset="legacy-synthetic"),
                 "regulation_points_direction_status_degrees", database="gr9_upgrade")

    rows_text = expect_ok(db, "SELECT jsonb_agg(jsonb_build_object("
                             "'entity_id', entity_id, 'direction_degrees', direction_degrees,"
                             "'direction_status', direction_status, 'validity_status', validity_status,"
                             "'imputation_method', imputation_method, 'imputation_reason', imputation_reason,"
                             "'direction_source', direction_source, 'direction_evidence', direction_evidence,"
                             "'validity_reason', validity_reason, 'validity_evidence', validity_evidence)"
                             " ORDER BY entity_id)::text FROM regulation_points", database="gr9_upgrade")
    rows = json.loads(rows_text)
    by_id = {row["entity_id"]: row for row in rows}
    synthetic_v1_backfill = expect_ok(db, "SELECT count(*)||','||count(*) FILTER (WHERE direction_status='unknown' "
                                      "AND direction_degrees IS NULL AND imputation_method='none' "
                                      "AND imputation_reason='legacy_unrecorded' AND direction_source='unknown' "
                                      "AND direction_evidence='{}'::jsonb)||','||count(*) FILTER (WHERE validity_status='unknown' "
                                      "AND validity_reason='synthetic_fixture' AND validity_evidence='{}'::jsonb) "
                                      "FROM regulation_points WHERE dataset_id='synthetic-v1'", database="gr9_upgrade")
    assert synthetic_v1_backfill == "12,12,12", synthetic_v1_backfill
    assert by_id["legacy-unknown"] == {
        "entity_id": "legacy-unknown", "direction_degrees": None, "direction_status": "unknown",
        "validity_status": "unknown", "imputation_method": "none", "imputation_reason": "legacy_unrecorded",
        "direction_source": "unknown", "direction_evidence": {}, "validity_reason": "synthetic_fixture",
        "validity_evidence": {},
    }, by_id["legacy-unknown"]
    known = by_id["legacy-known"]
    assert (known["direction_degrees"], known["direction_status"], known["validity_status"]) == (123.5, "inferred", "valid"), known
    assert (known["imputation_method"], known["imputation_reason"], known["direction_source"], known["direction_evidence"]) == (
        "none", "legacy_unrecorded", "unknown", {},
    ), known
    assert (known["validity_reason"], known["validity_evidence"]) == ("legacy_unrecorded", {}), known
    assert by_id["source-unknown"]["validity_reason"] == "legacy_unrecorded", by_id["source-unknown"]
    confirmed = by_id["source-confirmed"]
    assert (confirmed["direction_degrees"], confirmed["direction_status"], confirmed["validity_status"]) == (77, "confirmed", "unknown"), confirmed
    assert (confirmed["imputation_method"], confirmed["imputation_reason"], confirmed["direction_source"],
            confirmed["direction_evidence"], confirmed["validity_reason"], confirmed["validity_evidence"]) == (
        "none", "legacy_unrecorded", "unknown", {}, "legacy_unrecorded", {},
    ), confirmed

    # Old API SELECTs and old seed INSERTs omit the new columns and keep using
    # the approval columns/defaults during the expand phase.
    old_select = expect_ok(db, "SELECT d.production_approved::text||','||p.direction_status "
                              "FROM regulation_points p JOIN regulation_datasets d USING (dataset_id) "
                              "WHERE p.entity_id='legacy-unknown'", database="gr9_upgrade")
    assert old_select == "false,unknown", old_select
    expect_ok(db, insert_dataset("old-writer", digest="d" * 64, synthetic="true"), database="gr9_upgrade")
    expect_ok(db, insert_point("old-writer-a", "old-a", dataset="old-writer"), database="gr9_upgrade")
    expect_ok(db, insert_point("old-writer-b", "old-b", lon=136.901, dataset="old-writer"), database="gr9_upgrade")
    expect_ok(db, insert_candidate("old-writer-a", "old-writer-b", dataset="old-writer"), database="gr9_upgrade")
    defaults = expect_ok(db, "SELECT imputation_method||','||imputation_reason||','||direction_source||','||"
                            "validity_reason||','||direction_evidence::text||','||validity_evidence::text "
                            "FROM regulation_points WHERE entity_id='old-writer-a'", database="gr9_upgrade")
    assert defaults == "none,legacy_unrecorded,unknown,legacy_unrecorded,{},{}", defaults
    return {"legacy_rows_backfilled": len(rows), "synthetic_v1_seed": seed_result,
            "synthetic_v1_backfill": synthetic_v1_backfill, "known_values_preserved": True,
            "synthetic_unknown_validity": "synthetic_fixture", "non_synthetic_unknown_validity": "legacy_unrecorded",
            "old_consumer_select": old_select, "old_writer_defaults": defaults}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--toolchain-report", required=True, type=Path)
    parser.add_argument("--scratch-dir", type=Path)
    args = parser.parse_args()
    report_path = args.toolchain_report.resolve()
    scratch_dir = (args.scratch_dir or APP_DIR.parents[1] / "validation").resolve()
    if not report_path.is_file():
        sys.exit("toolchain report does not exist")
    if scratch_dir == report_path.parent:
        sys.exit("scratch directory must be separate from the read-only toolchain report directory")
    report = json.loads(report_path.read_text())
    if report.get("status") != "validated_stopped" or not report.get("postgres_available"):
        sys.exit("toolchain report is not a validated local PostGIS toolchain")

    migrations = sorted((APP_DIR / "db" / "migrations").glob("*.sql"))
    names = [migration.name for migration in migrations]
    assert names[0] == "202606160001_init_stop_signs.sql", names
    assert "202610130001_regulation_datasets.sql" in names, "base regulation migration is missing"
    assert "202610140001_regulation_point_evidence.sql" in names, "S1 evidence migration is missing"
    assert names[-1] == "202610290001_simplify_regulation_point_checks.sql", names
    cluster = Cluster(report_path, scratch_dir)
    summary = {"passed": False, "migrations": names, "scratch_directory": str(scratch_dir),
               "remote_connection_attempted": False}
    try:
        cluster.start()
        for setup in ("CREATE ROLE rds_iam NOLOGIN", "CREATE DATABASE gr9_dev", "CREATE DATABASE gr9_upgrade"):
            created = cluster.sql(setup, database="postgres")
            assert created.returncode == 0, created.stderr
        apply_migrations(cluster, migrations, database="gr9_dev")
        validate(cluster)
        summary["fresh_schema"] = "passed"
        summary["upgrade_schema"] = validate_upgrade(cluster, migrations, report)
        summary["passed"] = True
    except Exception as error:
        summary["error"] = f"{type(error).__name__}: {error}"
        summary["traceback"] = traceback.format_exc()
    finally:
        summary["database_stopped"] = cluster.stop()
        summary["pg_ctl_stop_returncode"] = cluster.stop_returncode
        summary["postmaster_pid_absent"] = not (cluster.data / "postmaster.pid").exists()

    output = json.dumps(summary, sort_keys=True)
    scratch_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    (scratch_dir / "schema-validation.log").write_text(output + "\n")
    print(output)
    sys.exit(0 if summary["passed"] and summary["database_stopped"] and summary["postmaster_pid_absent"] else 1)


if __name__ == "__main__":
    main()
