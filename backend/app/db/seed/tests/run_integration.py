#!/usr/bin/env python3
"""Run the seed and API repository integration tests against a disposable PostGIS.

The script starts a fresh PostgreSQL cluster from the toolchain described by the
report file, applies every migration in ``db/migrations``, runs the Go
seed integration tests with ``TEST_DATABASE_URL`` pointing at the cluster's Unix
socket, then exercises the real ``cmd/seed`` binary over loopback TCP (apply,
replay, unknown dataset) and finally runs the integration tests of the
PostgreSQL stop point repository against the seeded dataset and the connection
pool's per-connection password resolution over loopback TCP. The cluster is
always stopped and removed. The script never reads application configuration and
never contacts a remote database.
"""
import argparse
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[3]
SCHEMA_TESTS = APP_DIR / "db" / "schema" / "tests" / "validate_schema.py"
MIGRATIONS = sorted((APP_DIR / "db" / "migrations").glob("*.sql"))
SEED_CLI_DATASET = "synthetic-v1"
SEED_CLI_V2_DATASET = "synthetic-v2"
SEED_CLI_RULE_TEST_DATASET = "nagoya-rule-test-202602-v1"
EXTERNAL_SQL_DATASET = "external-stop-approach-test-202602-v1"
EXTERNAL_SQL = APP_DIR / "db" / "seed" / EXTERNAL_SQL_DATASET / "seed.sql"


def load_cluster_class():
    spec = importlib.util.spec_from_file_location("validate_schema", SCHEMA_TESTS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Cluster


def go_env(report):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/tmp")}
    for key in ("GOTOOLCHAIN", "GOCACHE", "GOPATH"):
        env[key] = report["environment"][key]
    return env


def run(argv, env, cwd=APP_DIR, timeout=600):
    return subprocess.run(argv, env=env, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def allow_loopback_trust(cluster, database="gr9_dev"):
    """Let the disposable cluster accept its own loopback connections."""
    hba = cluster.data / "pg_hba.conf"
    hba.write_text(f"host {database} validator 127.0.0.1/32 trust\n" + hba.read_text())
    cluster.run("pg_ctl", "-D", cluster.data, "reload")


def migrate_cli(binary, cluster, database, atlas, seed_ids, port=None):
    """Run the real migrate binary (Atlas apply, then the optional seed step)."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "DB_HOST": "127.0.0.1", "DB_PORT": str(port if port is not None else cluster.port), "DB_NAME": database,
        "DB_USER": "validator", "DB_PASSWORD": "disposable-cluster-uses-trust-auth",
        "DB_IAM_AUTH": "false", "DB_SSL_MODE": "disable",
        "ATLAS_BINARY_PATH": atlas,
        "ATLAS_MIGRATION_DIR": f"file://{APP_DIR / 'db' / 'migrations'}",
    }
    if seed_ids is not None:
        env["SEED_DATASET_IDS"] = seed_ids
    return run([str(binary)], env, timeout=180)


def json_lines(text):
    values = []
    for line in text.splitlines():
        try:
            values.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return values


def psql_scalar(cluster, statement, database="gr9_dev"):
    result = cluster.sql(statement, database=database)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def external_sql_documents():
    text = EXTERNAL_SQL.read_text()
    points_marker = "$external_seed_points$"
    dataset_marker = "$external_seed_dataset_check$"
    points_start = text.index(points_marker) + len(points_marker)
    points_end = text.index(points_marker, points_start)
    dataset_start = text.index(dataset_marker) + len(dataset_marker)
    dataset_end = text.index(dataset_marker, dataset_start)
    return json.loads(text[points_start:points_end]), json.loads(text[dataset_start:dataset_end])


def external_psql_environment(env):
    return {key: env[key] for key in ("PGHOST", "PGPORT", "PGUSER", "PGDATABASE")}


def run_external_seed_sql(cluster, database):
    env = dict(cluster.env)
    env.update({
        "PGHOST": str(cluster.sock),
        "PGPORT": str(cluster.port),
        "PGUSER": "validator",
        "PGDATABASE": database,
    })
    command = [str(cluster.bin / "psql"), "-v", "ON_ERROR_STOP=1", "-f", str(EXTERNAL_SQL)]
    result = subprocess.run(command, env=env, cwd=APP_DIR, capture_output=True, text=True, timeout=180)
    return command, env, result


def external_point_snapshot(cluster):
    return psql_scalar(cluster, "SELECT jsonb_agg(jsonb_build_object("
        "'dataset_id', dataset_id, 'entity_id', entity_id, 'raw_uid', raw_uid,"
        "'longitude', ST_X(geom::geometry), 'latitude', ST_Y(geom::geometry),"
        "'direction_degrees', direction_degrees, 'direction_status', direction_status,"
        "'validity_status', validity_status, 'normalization_status', normalization_status,"
        "'created_at', created_at, 'updated_at', updated_at,"
        "'imputation_method', imputation_method, 'imputation_reason', imputation_reason,"
        "'direction_source', direction_source, 'direction_evidence', direction_evidence,"
        "'validity_reason', validity_reason, 'validity_evidence', validity_evidence)"
        " ORDER BY entity_id)::text FROM regulation_points WHERE dataset_id='" + EXTERNAL_SQL_DATASET + "'")


def sql_literal(value):
    return "'" + value.replace("'", "''") + "'"


def seed_cli(binary, cluster, *args, database="gr9_dev"):
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "DB_HOST": "127.0.0.1", "DB_PORT": str(cluster.port), "DB_NAME": database,
        "DB_USER": "validator", "DB_PASSWORD": "disposable-cluster-uses-trust-auth",
        "DB_IAM_AUTH": "false", "DB_SSL_MODE": "disable",
    }
    return run([str(binary), *args], env, timeout=120)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--toolchain-report", required=True, type=Path)
    parser.add_argument("--scratch-dir", type=Path, help="dedicated local cluster/scratch directory")
    parser.add_argument("--log", type=Path, help="write the go test output to this file")
    args = parser.parse_args()
    report_path = args.toolchain_report.resolve()
    scratch_dir = args.scratch_dir.resolve() if args.scratch_dir else None
    if scratch_dir is not None and (scratch_dir.is_relative_to(report_path.parent) or report_path.is_relative_to(scratch_dir)):
        sys.exit("scratch directory must be separate from the read-only toolchain report directory")
    report = json.loads(report_path.read_text())
    rule_manifest_path = APP_DIR / "internal" / "seeddata" / "data" / SEED_CLI_RULE_TEST_DATASET / "manifest.json"
    rule_manifest = json.loads(rule_manifest_path.read_text())
    rule_counts = rule_manifest["counts"]
    if rule_manifest.get("dataset_id") != SEED_CLI_RULE_TEST_DATASET or rule_manifest.get("provenance", {}).get("data_purpose") != "rule_test":
        sys.exit("embedded rule_test manifest identity or purpose mismatch")
    if report.get("status") != "validated_stopped":
        sys.exit("toolchain report is not a validated local toolchain")
    go = report["go_binary"]
    env = go_env(report)

    Cluster = load_cluster_class()
    db = Cluster(report_path, scratch_dir)
    summary: dict = {"passed": False, "scratch_directory": str(scratch_dir) if scratch_dir else None}
    scratch = tempfile.mkdtemp(prefix="seed-cli-", dir=db.tmp.parent)
    try:
        db.start()
        for setup in ("CREATE ROLE rds_iam NOLOGIN", "CREATE DATABASE gr9_dev", "CREATE DATABASE gr9_upgrade",
                      "CREATE DATABASE gr9_external_no_schema"):
            created = db.sql(setup, database="postgres")
            assert created.returncode == 0, created.stderr
        for migration in MIGRATIONS:
            applied = db.sql("", file=str(migration))
            assert applied.returncode == 0, f"{migration.name}\n{applied.stderr}"
        allow_loopback_trust(db)

        url = f"postgresql://validator@/gr9_dev?host={db.sock}&port={db.port}"
        tests = run([go, "test", "./internal/seed/...", "-run", "Integration", "-count=1", "-v"],
                    dict(env, TEST_DATABASE_URL=url))
        if args.log:
            args.log.write_text(tests.stdout + tests.stderr)
        assert tests.returncode == 0, tests.stdout + tests.stderr
        passed = [l for l in tests.stdout.splitlines() if l.startswith("--- PASS: TestIntegration")]
        skipped = [l for l in tests.stdout.splitlines() if l.startswith("--- SKIP")]
        assert len(passed) >= 10 and not skipped, tests.stdout
        summary["integration_tests_passed"] = len(passed)

        binary = Path(scratch) / "seed"
        built = run([go, "build", "-o", str(binary), "./cmd/seed"], env)
        assert built.returncode == 0, built.stderr
        first = seed_cli(binary, db, "--apply", "--dataset", SEED_CLI_DATASET)
        assert first.returncode == 0 and '"status":"inserted"' in first.stdout, first.stdout + first.stderr
        replay = seed_cli(binary, db, "--apply", "--dataset", SEED_CLI_DATASET)
        assert replay.returncode == 0 and '"status":"unchanged"' in replay.stdout, replay.stdout + replay.stderr
        missing = seed_cli(binary, db, "--apply", "--dataset", "missing-dataset")
        assert missing.returncode == 1, missing.stdout + missing.stderr
        counts = psql_scalar(db, "SELECT (SELECT count(*) FROM regulation_datasets WHERE dataset_id='synthetic-v1')||','||"
                                 "(SELECT count(*) FROM regulation_points WHERE dataset_id='synthetic-v1')||','||"
                                 "(SELECT count(*) FROM regulation_candidates WHERE dataset_id='synthetic-v1')")
        flags = psql_scalar(db, "SELECT synthetic::text||','||production_approved::text FROM regulation_datasets"
                                " WHERE dataset_id='synthetic-v1'")
        assert counts == "1,12,58", counts
        assert flags == "true,false", flags

        # Exercise the same streaming binary path with the compressed v2 fixture.
        v2_first = seed_cli(binary, db, "--apply", "--dataset", SEED_CLI_V2_DATASET)
        assert v2_first.returncode == 0 and '"status":"inserted"' in v2_first.stdout, v2_first.stdout + v2_first.stderr
        v2_replay = seed_cli(binary, db, "--apply", "--dataset", SEED_CLI_V2_DATASET)
        assert v2_replay.returncode == 0 and '"status":"unchanged"' in v2_replay.stdout, v2_replay.stdout + v2_replay.stderr
        v2_counts = psql_scalar(db, "SELECT (SELECT count(*) FROM regulation_datasets WHERE dataset_id='synthetic-v2')||','||"
                                    "(SELECT count(*) FROM regulation_points WHERE dataset_id='synthetic-v2')||','||"
                                    "(SELECT count(*) FROM regulation_candidates WHERE dataset_id='synthetic-v2')")
        v2_flags = psql_scalar(db, "SELECT synthetic::text||','||production_approved::text FROM regulation_datasets"
                                   " WHERE dataset_id='synthetic-v2'")
        v2_states = psql_scalar(db, "SELECT count(*) FILTER (WHERE direction_status='unknown' AND validity_status='unknown')"
                                    "||','||count(*) FILTER (WHERE direction_degrees IS NOT NULL) FROM regulation_points"
                                    " WHERE dataset_id='synthetic-v2'")
        assert v2_counts == "1,12,58", v2_counts
        assert v2_flags == "true,false", v2_flags
        assert v2_states == "12,0", v2_states

        rule_first = seed_cli(binary, db, "--apply", "--dataset", SEED_CLI_RULE_TEST_DATASET)
        assert rule_first.returncode == 0 and '"status":"inserted"' in rule_first.stdout, rule_first.stdout + rule_first.stderr
        rule_replay = seed_cli(binary, db, "--apply", "--dataset", SEED_CLI_RULE_TEST_DATASET)
        assert rule_replay.returncode == 0 and '"status":"unchanged"' in rule_replay.stdout, rule_replay.stdout + rule_replay.stderr
        rule_counts_text = psql_scalar(db, f"SELECT (SELECT count(*) FROM regulation_datasets WHERE dataset_id='{SEED_CLI_RULE_TEST_DATASET}')||','||"
                                           f"(SELECT count(*) FROM regulation_points WHERE dataset_id='{SEED_CLI_RULE_TEST_DATASET}')||','||"
                                           f"(SELECT count(*) FROM regulation_candidates WHERE dataset_id='{SEED_CLI_RULE_TEST_DATASET}')")
        rule_flags = psql_scalar(db, f"SELECT synthetic::text||','||production_approved::text FROM regulation_datasets"
                                     f" WHERE dataset_id='{SEED_CLI_RULE_TEST_DATASET}'")
        rule_states = psql_scalar(db, f"SELECT count(*) FILTER (WHERE direction_status='inferred')||','||"
                                      f"count(*) FILTER (WHERE direction_status='unknown')||','||"
                                      f"count(*) FILTER (WHERE direction_status='confirmed')||','||"
                                      f"count(*) FILTER (WHERE validity_status='unknown')||','||"
                                      f"count(*) FILTER (WHERE direction_status='inferred' AND direction_evidence->>'direction_semantics'='stop_to_neighbor_proxy')||','||"
                                      f"count(*) FILTER (WHERE validity_status<>'unknown') FROM regulation_points"
                                      f" WHERE dataset_id='{SEED_CLI_RULE_TEST_DATASET}'")
        expected_rule_counts = f"1,{rule_counts['points']},{rule_counts['candidates']}"
        expected_rule_states = ",".join(str(value) for value in (
            rule_counts["direction_status_counts"]["inferred"],
            rule_counts["direction_status_counts"]["unknown"],
            rule_counts["direction_status_counts"]["confirmed"],
            rule_counts["validity_status_counts"]["unknown"],
            rule_counts["direction_status_counts"]["inferred"],
            rule_counts["validity_status_counts"]["valid"] + rule_counts["validity_status_counts"]["invalid"],
        ))
        assert rule_counts_text == expected_rule_counts, rule_counts_text
        assert rule_flags == "true,false", rule_flags
        assert rule_states == expected_rule_states, rule_states

        # The external artifact is applied only by the explicit SQL file through
        # psql over this disposable cluster's private Unix socket.
        external_rows, external_dataset = external_sql_documents()
        assert len(external_rows) == 2333 and external_dataset["dataset_id"] == EXTERNAL_SQL_DATASET
        no_schema_command, no_schema_env, no_schema_result = run_external_seed_sql(db, "gr9_external_no_schema")
        no_schema_output = no_schema_result.stdout + no_schema_result.stderr
        assert no_schema_result.returncode != 0 and "regulation_datasets" in no_schema_output, no_schema_output
        no_schema_tables = psql_scalar(db, "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'",
                                       database="gr9_external_no_schema")
        assert no_schema_tables == "0", f"seed created persistent tables without migrations: {no_schema_tables}"

        digest = external_dataset["content_sha256"]
        conflict = db.sql(
            "INSERT INTO regulation_datasets (dataset_id, content_sha256, source_month, region_id, synthetic, production_approved) "
            f"VALUES ('external-digest-conflict', '{digest}', '202602', 'test:conflict', false, false)"
        )
        assert conflict.returncode == 0, conflict.stderr
        conflict_command, conflict_env, conflict_result = run_external_seed_sql(db, "gr9_dev")
        conflict_output = conflict_result.stdout + conflict_result.stderr
        assert conflict_result.returncode != 0 and "digest already belongs to another dataset ID" in conflict_output, conflict_output
        assert psql_scalar(db, f"SELECT count(*) FROM regulation_datasets WHERE dataset_id='{EXTERNAL_SQL_DATASET}'") == "0"
        removed = db.sql("DELETE FROM regulation_datasets WHERE dataset_id='external-digest-conflict'")
        assert removed.returncode == 0, removed.stderr

        fail_entity = max(point["entity_id"] for point in external_rows)
        function = db.sql(
            "CREATE FUNCTION external_seed_test_fail_late() RETURNS trigger LANGUAGE plpgsql AS $test$ "
            f"BEGIN IF NEW.entity_id = {sql_literal(fail_entity)} THEN "
            "RAISE EXCEPTION 'intentional disposable mid-seed failure'; END IF; RETURN NEW; END $test$"
        )
        assert function.returncode == 0, function.stderr
        trigger = db.sql("CREATE TRIGGER external_seed_test_fail_late BEFORE INSERT ON regulation_points "
                         "FOR EACH ROW EXECUTE FUNCTION external_seed_test_fail_late()")
        assert trigger.returncode == 0, trigger.stderr
        rollback_command, rollback_env, rollback_result = run_external_seed_sql(db, "gr9_dev")
        rollback_output = rollback_result.stdout + rollback_result.stderr
        assert rollback_result.returncode != 0 and "intentional disposable mid-seed failure" in rollback_output, rollback_output
        assert psql_scalar(db, f"SELECT count(*) FROM regulation_datasets WHERE dataset_id='{EXTERNAL_SQL_DATASET}'") == "0"
        assert psql_scalar(db, f"SELECT count(*) FROM regulation_points WHERE dataset_id='{EXTERNAL_SQL_DATASET}'") == "0"
        assert db.sql("DROP TRIGGER external_seed_test_fail_late ON regulation_points").returncode == 0
        assert db.sql("DROP FUNCTION external_seed_test_fail_late()").returncode == 0

        seed_sql_command, seed_sql_env, external_first = run_external_seed_sql(db, "gr9_dev")
        assert external_first.returncode == 0, external_first.stdout + external_first.stderr
        external_counts = psql_scalar(db, f"SELECT (SELECT count(*) FROM regulation_datasets WHERE dataset_id='{EXTERNAL_SQL_DATASET}')||','||"
                                          f"(SELECT count(*) FROM regulation_points WHERE dataset_id='{EXTERNAL_SQL_DATASET}')||','||"
                                          f"(SELECT count(*) FROM regulation_candidates WHERE dataset_id='{EXTERNAL_SQL_DATASET}')")
        assert external_counts == "1,2333,0", external_counts
        external_states = psql_scalar(db, f"SELECT count(*) FILTER (WHERE direction_status='inferred')||','||"
                                          f"count(*) FILTER (WHERE direction_status='unknown')||','||"
                                          f"count(*) FILTER (WHERE direction_status='confirmed')||','||"
                                          f"count(*) FILTER (WHERE validity_status='unknown')||','||"
                                          f"count(*) FILTER (WHERE direction_evidence ? 'confidence' AND "
                                          f"jsonb_typeof(direction_evidence->'confidence')='null') "
                                          f"FROM regulation_points WHERE dataset_id='{EXTERNAL_SQL_DATASET}'")
        assert external_states == "2325,8,0,2333,2325", external_states
        external_metadata = psql_scalar(db, f"SELECT source_month||','||region_id||','||synthetic::text||','||production_approved::text "
                                            f"FROM regulation_datasets WHERE dataset_id='{EXTERNAL_SQL_DATASET}'")
        assert external_metadata == "202602,jp:shiga-aichi:external-test,false,false", external_metadata
        points_before_replay = external_point_snapshot(db)
        external_replay_command, _, external_replay = run_external_seed_sql(db, "gr9_dev")
        assert external_replay.returncode == 0, external_replay.stdout + external_replay.stderr
        points_after_replay = external_point_snapshot(db)
        assert points_before_replay == points_after_replay, "idempotent SQL replay changed stored points"

        # Conflicting immutable state is rejected without being repaired in place.
        mutated_digest = "f" * 64
        assert mutated_digest != digest
        changed = db.sql(f"UPDATE regulation_datasets SET content_sha256='{mutated_digest}' WHERE dataset_id='{EXTERNAL_SQL_DATASET}'")
        assert changed.returncode == 0, changed.stderr
        digest_conflict_command, _, digest_conflict = run_external_seed_sql(db, "gr9_dev")
        digest_conflict_text = digest_conflict.stdout + digest_conflict.stderr
        assert digest_conflict.returncode != 0 and "different digest" in digest_conflict_text, digest_conflict_text
        assert psql_scalar(db, f"SELECT content_sha256 FROM regulation_datasets WHERE dataset_id='{EXTERNAL_SQL_DATASET}'") == mutated_digest
        assert db.sql(f"UPDATE regulation_datasets SET content_sha256='{digest}' WHERE dataset_id='{EXTERNAL_SQL_DATASET}'").returncode == 0

        damaged_entity = external_rows[0]["entity_id"]
        damaged = db.sql(f"UPDATE regulation_points SET normalization_status='tampered' "
                         f"WHERE dataset_id='{EXTERNAL_SQL_DATASET}' AND entity_id={sql_literal(damaged_entity)}")
        assert damaged.returncode == 0, damaged.stderr
        damaged_command, _, damaged_replay = run_external_seed_sql(db, "gr9_dev")
        damaged_text = damaged_replay.stdout + damaged_replay.stderr
        assert damaged_replay.returncode != 0 and "read-back differs" in damaged_text, damaged_text
        assert psql_scalar(db, f"SELECT normalization_status FROM regulation_points WHERE dataset_id='{EXTERNAL_SQL_DATASET}' "
                               f"AND entity_id={sql_literal(damaged_entity)}") == "tampered"
        assert db.sql(f"UPDATE regulation_points SET normalization_status='valid_candidate_only' "
                      f"WHERE dataset_id='{EXTERNAL_SQL_DATASET}' AND entity_id={sql_literal(damaged_entity)}").returncode == 0
        restored_command, _, restored = run_external_seed_sql(db, "gr9_dev")
        assert restored.returncode == 0, restored.stdout + restored.stderr

        summary.update(
            external_seed_connection_url=f"postgresql://validator@/gr9_dev?host={db.sock}&port={db.port}",
            external_seed_psql_environment=external_psql_environment(seed_sql_env),
            external_seed_no_schema_command=no_schema_command,
            external_seed_no_schema_psql_environment=external_psql_environment(no_schema_env),
            external_seed_no_schema_returncode=no_schema_result.returncode,
            external_seed_no_schema_public_tables=no_schema_tables,
            external_seed_digest_conflict_command=conflict_command,
            external_seed_digest_conflict_psql_environment=external_psql_environment(conflict_env),
            external_seed_digest_conflict_returncode=conflict_result.returncode,
            external_seed_digest_conflict_rejected=True,
            external_seed_midway_failure_command=rollback_command,
            external_seed_midway_failure_psql_environment=external_psql_environment(rollback_env),
            external_seed_midway_failure_returncode=rollback_result.returncode,
            external_seed_midway_failure_rolled_back=True,
            external_seed_first_command=seed_sql_command,
            external_seed_first_returncode=external_first.returncode,
            external_seed_replay_command=external_replay_command,
            external_seed_replay_returncode=external_replay.returncode,
            external_seed_replay_readback_unchanged=True,
            external_seed_digest_mismatch_command=digest_conflict_command,
            external_seed_digest_mismatch_returncode=digest_conflict.returncode,
            external_seed_corruption_command=damaged_command,
            external_seed_corruption_returncode=damaged_replay.returncode,
            external_seed_corruption_not_overwritten=True,
            external_seed_restored_replay_command=restored_command,
            external_seed_restored_replay_returncode=restored.returncode,
            external_seed_counts=external_counts,
            external_seed_states=external_states,
            external_seed_metadata=external_metadata,
            external_seed_content_sha256=digest,
        )

        # The API repository reads the dataset the CLI and explicit SQL just applied.
        repo_tests = run([go, "test", "./internal/repository/postgres/...", "-run", "Integration", "-count=1", "-v"],
                         dict(env, TEST_DATABASE_URL=url))
        if args.log:
            with args.log.open("a") as log:
                log.write(repo_tests.stdout + repo_tests.stderr)
        assert repo_tests.returncode == 0, repo_tests.stdout + repo_tests.stderr
        repo_passed = [l for l in repo_tests.stdout.splitlines() if l.startswith("--- PASS: TestIntegration")]
        repo_skipped = [l for l in repo_tests.stdout.splitlines() if l.startswith("--- SKIP")]
        assert len(repo_passed) >= 8 and not repo_skipped, repo_tests.stdout
        summary["repository_integration_tests_passed"] = len(repo_passed)

        # A real pool must resolve the password for each new connection.
        tcp_url = f"postgresql://validator@127.0.0.1:{db.port}/gr9_dev?sslmode=disable"
        pool_tests = run([go, "test", "./internal/db/...", "-run", "Integration", "-count=1", "-v"],
                         dict(env, TEST_DATABASE_TCP_URL=tcp_url))
        if args.log:
            with args.log.open("a") as log:
                log.write(pool_tests.stdout + pool_tests.stderr)
        assert pool_tests.returncode == 0, pool_tests.stdout + pool_tests.stderr
        pool_passed = [l for l in pool_tests.stdout.splitlines() if l.startswith("--- PASS: TestIntegration")]
        pool_skipped = [l for l in pool_tests.stdout.splitlines() if l.startswith("--- SKIP")]
        assert len(pool_passed) >= 1 and not pool_skipped, pool_tests.stdout
        summary["pool_integration_tests_passed"] = len(pool_passed)
        summary.update(cli_apply=json.loads(first.stdout), cli_replay=json.loads(replay.stdout),
                       cli_counts_datasets_points_candidates=counts, cli_flags_synthetic_production=flags,
                       rule_test_cli_apply=json.loads(rule_first.stdout), rule_test_cli_replay=json.loads(rule_replay.stdout),
                       rule_test_counts_datasets_points_candidates=rule_counts_text,
                       rule_test_flags_synthetic_production=rule_flags, rule_test_state_and_evidence_counts=rule_states)

        # Re-run both seed formats after dropping only the approval columns in a
        # separate disposable database, matching the post-contract seed schema.
        created = db.sql("CREATE DATABASE gr9_seed_no_approval", database="postgres")
        assert created.returncode == 0, created.stderr
        for migration in MIGRATIONS:
            applied = db.sql("", file=str(migration), database="gr9_seed_no_approval")
            assert applied.returncode == 0, f"{migration.name} on no-approval database\n{applied.stderr}"
        for statement in (
            "ALTER TABLE regulation_datasets DROP CONSTRAINT regulation_datasets_synthetic_not_approved, DROP COLUMN production_approved",
            "ALTER TABLE regulation_candidates DROP CONSTRAINT regulation_candidates_not_approved, DROP COLUMN production_approved",
        ):
            dropped = db.sql(statement, database="gr9_seed_no_approval")
            assert dropped.returncode == 0, dropped.stderr
        allow_loopback_trust(db, "gr9_seed_no_approval")
        compat_first = seed_cli(binary, db, "--apply", "--dataset", SEED_CLI_DATASET, database="gr9_seed_no_approval")
        compat_replay = seed_cli(binary, db, "--apply", "--dataset", SEED_CLI_DATASET, database="gr9_seed_no_approval")
        compat_v2_first = seed_cli(binary, db, "--apply", "--dataset", SEED_CLI_V2_DATASET, database="gr9_seed_no_approval")
        compat_v2_replay = seed_cli(binary, db, "--apply", "--dataset", SEED_CLI_V2_DATASET, database="gr9_seed_no_approval")
        compat_rule_first = seed_cli(binary, db, "--apply", "--dataset", SEED_CLI_RULE_TEST_DATASET, database="gr9_seed_no_approval")
        compat_rule_replay = seed_cli(binary, db, "--apply", "--dataset", SEED_CLI_RULE_TEST_DATASET, database="gr9_seed_no_approval")
        assert compat_first.returncode == 0 and '"status":"inserted"' in compat_first.stdout, compat_first.stdout + compat_first.stderr
        assert compat_replay.returncode == 0 and '"status":"unchanged"' in compat_replay.stdout, compat_replay.stdout + compat_replay.stderr
        assert compat_v2_first.returncode == 0 and '"status":"inserted"' in compat_v2_first.stdout, compat_v2_first.stdout + compat_v2_first.stderr
        assert compat_v2_replay.returncode == 0 and '"status":"unchanged"' in compat_v2_replay.stdout, compat_v2_replay.stdout + compat_v2_replay.stderr
        assert compat_rule_first.returncode == 0 and '"status":"inserted"' in compat_rule_first.stdout, compat_rule_first.stdout + compat_rule_first.stderr
        assert compat_rule_replay.returncode == 0 and '"status":"unchanged"' in compat_rule_replay.stdout, compat_rule_replay.stdout + compat_rule_replay.stderr
        compat_counts = psql_scalar(db, "SELECT (SELECT count(*) FROM regulation_datasets)||','||"
                                        "(SELECT count(*) FROM regulation_points)||','||"
                                        "(SELECT count(*) FROM regulation_candidates)", database="gr9_seed_no_approval")
        expected_compat_counts = f"3,{24 + rule_counts['points']},{116 + rule_counts['candidates']}"
        assert compat_counts == expected_compat_counts, compat_counts
        summary.update(seed_without_approval_columns="v1/v2/rule_test insert and replay passed", no_approval_counts=compat_counts)

        # The deployed path: migrate applies the schema to an empty database
        # and then seeds the datasets named in SEED_DATASET_IDS.
        migrate_binary = Path(scratch) / "migrate"
        built = run([go, "build", "-o", str(migrate_binary), "./cmd/migrate"], env)
        assert built.returncode == 0, built.stderr
        atlas = report["atlas_binary"]
        for database in ("gr9_migrate_only", "gr9_migrate_empty", "gr9_migrate_seed", "gr9_migrate_bad"):
            created = db.sql(f"CREATE DATABASE {database}", database="postgres")
            assert created.returncode == 0, created.stderr
            allow_loopback_trust(db, database)

        def table_count(database, table):
            result = db.sql(f"SELECT count(*) FROM {table}", database=database)
            return result.stdout.strip() if result.returncode == 0 else "missing"

        plain = migrate_cli(migrate_binary, db, "gr9_migrate_only", atlas, None)
        assert plain.returncode == 0, plain.stdout + plain.stderr
        assert table_count("gr9_migrate_only", "regulation_datasets") == "0", "schema applied, nothing seeded"

        empty = migrate_cli(migrate_binary, db, "gr9_migrate_empty", atlas, "")
        assert empty.returncode == 0, empty.stdout + empty.stderr
        assert table_count("gr9_migrate_empty", "regulation_datasets") == "0", "empty seed list must perform no writes"

        seed_ids = f"{SEED_CLI_DATASET},{SEED_CLI_V2_DATASET},{SEED_CLI_RULE_TEST_DATASET}"
        seeded = migrate_cli(migrate_binary, db, "gr9_migrate_seed", atlas, seed_ids)
        assert seeded.returncode == 0 and seeded.stdout.count('"status":"inserted"') == 3, seeded.stdout + seeded.stderr
        again = migrate_cli(migrate_binary, db, "gr9_migrate_seed", atlas, seed_ids)
        seeded_rows = json_lines(seeded.stdout)
        replay_rows = json_lines(again.stdout)
        expected_dataset_ids = {SEED_CLI_DATASET, SEED_CLI_V2_DATASET, SEED_CLI_RULE_TEST_DATASET}
        assert len(seeded_rows) == 3 and {row["dataset_id"] for row in seeded_rows} == expected_dataset_ids
        assert len(replay_rows) == 3 and {row["dataset_id"] for row in replay_rows} == expected_dataset_ids
        assert all(row["status"] == "inserted" for row in seeded_rows)
        assert all(row["status"] == "unchanged" for row in replay_rows)
        assert table_count("gr9_migrate_seed", "regulation_points") == str(24 + rule_counts["points"])
        assert table_count("gr9_migrate_seed", "regulation_candidates") == str(116 + rule_counts["candidates"])

        # Unknown IDs must fail even when DB_HOST points at an unavailable local
        # port, proving preflight precedes database pool creation and Atlas.
        offline_bad = migrate_cli(migrate_binary, db, "gr9_migrate_bad", atlas,
                                  seed_ids + ",missing-dataset", port=1)
        offline_output = offline_bad.stdout + offline_bad.stderr
        assert offline_bad.returncode == 1 and "missing-dataset" in offline_output, offline_output
        assert "connection refused" not in offline_output.lower(), offline_output

        # Unknown requested IDs must also leave a reachable fresh database schema untouched.
        bad = migrate_cli(migrate_binary, db, "gr9_migrate_bad", atlas, seed_ids + ",missing-dataset")
        bad_output = bad.stdout + bad.stderr
        assert bad.returncode == 1 and "missing-dataset" in bad_output, bad_output
        public_tables = psql_scalar(db, "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'",
                                    database="gr9_migrate_bad")
        assert public_tables == "0", f"preflight failure applied schema: found {public_tables} public tables"
        summary.update(migrate_schema_only="ok", migrate_empty_seed_no_writes=True,
                       migrate_seed_inserted=json_lines(seeded.stdout),
                       migrate_seed_replay=json_lines(again.stdout),
                       migrate_unknown_dataset_rejected_before_database_or_atlas=True,
                       migrate_unknown_dataset_rejected_without_db_connection=True,
                       migrate_bad_database_public_tables=public_tables)
        summary["passed"] = True
    finally:
        summary["database_stopped"] = db.stop()
        try:
            for leftover in Path(scratch).glob("*"):
                leftover.unlink()
            Path(scratch).rmdir()
        except OSError:
            pass
    rendered_summary = json.dumps(summary, sort_keys=True)
    if args.log:
        with args.log.open("a") as log:
            log.write("\nIntegration summary and exact external psql commands:\n" + rendered_summary + "\n")
    print(rendered_summary)
    sys.exit(0 if summary["passed"] and summary["database_stopped"] else 1)


if __name__ == "__main__":
    main()
