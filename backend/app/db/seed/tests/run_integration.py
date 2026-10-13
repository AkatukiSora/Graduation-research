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


def migrate_cli(binary, cluster, database, atlas, seed_ids):
    """Run the real migrate binary (Atlas apply, then the optional seed step)."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "DB_HOST": "127.0.0.1", "DB_PORT": str(cluster.port), "DB_NAME": database,
        "DB_USER": "validator", "DB_PASSWORD": "disposable-cluster-uses-trust-auth",
        "DB_IAM_AUTH": "false", "DB_SSL_MODE": "disable",
        "ATLAS_BINARY_PATH": atlas,
        "ATLAS_MIGRATION_DIR": f"file://{APP_DIR / 'db' / 'migrations'}",
    }
    if seed_ids is not None:
        env["SEED_DATASET_IDS"] = seed_ids
    return run([str(binary)], env, timeout=180)


def psql_scalar(cluster, statement):
    result = cluster.sql(statement)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def seed_cli(binary, cluster, *args):
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "DB_HOST": "127.0.0.1", "DB_PORT": str(cluster.port), "DB_NAME": "gr9_dev",
        "DB_USER": "validator", "DB_PASSWORD": "disposable-cluster-uses-trust-auth",
        "DB_IAM_AUTH": "false", "DB_SSL_MODE": "disable",
    }
    return run([str(binary), *args], env, timeout=120)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--toolchain-report", required=True, type=Path)
    parser.add_argument("--log", type=Path, help="write the go test output to this file")
    args = parser.parse_args()
    report = json.loads(args.toolchain_report.read_text())
    if report.get("status") != "validated_stopped":
        sys.exit("toolchain report is not a validated local toolchain")
    go = report["go_binary"]
    env = go_env(report)

    Cluster = load_cluster_class()
    db = Cluster(args.toolchain_report)
    summary: dict = {"passed": False}
    scratch = tempfile.mkdtemp(prefix="seed-cli-", dir=db.tmp.parent)
    try:
        db.start()
        for setup in ("CREATE ROLE rds_iam NOLOGIN", "CREATE DATABASE gr9_dev"):
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

        # The API repository reads the dataset the CLI just applied.
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
                       cli_counts_datasets_points_candidates=counts, cli_flags_synthetic_production=flags)

        # The deployed path: migrate applies the schema to an empty database
        # and then seeds the datasets named in SEED_DATASET_IDS.
        migrate_binary = Path(scratch) / "migrate"
        built = run([go, "build", "-o", str(migrate_binary), "./cmd/migrate"], env)
        assert built.returncode == 0, built.stderr
        atlas = report["atlas_binary"]
        for database in ("gr9_migrate_only", "gr9_migrate_seed", "gr9_migrate_bad"):
            created = db.sql(f"CREATE DATABASE {database}", database="postgres")
            assert created.returncode == 0, created.stderr
            allow_loopback_trust(db, database)

        def table_count(database, table):
            result = db.sql(f"SELECT count(*) FROM {table}", database=database)
            return result.stdout.strip() if result.returncode == 0 else "missing"

        plain = migrate_cli(migrate_binary, db, "gr9_migrate_only", atlas, None)
        assert plain.returncode == 0, plain.stdout + plain.stderr
        assert table_count("gr9_migrate_only", "regulation_datasets") == "0", "schema applied, nothing seeded"

        seeded = migrate_cli(migrate_binary, db, "gr9_migrate_seed", atlas, SEED_CLI_DATASET)
        assert seeded.returncode == 0 and '"status":"inserted"' in seeded.stdout, seeded.stdout + seeded.stderr
        again = migrate_cli(migrate_binary, db, "gr9_migrate_seed", atlas, SEED_CLI_DATASET)
        assert again.returncode == 0 and '"status":"unchanged"' in again.stdout, again.stdout + again.stderr
        assert table_count("gr9_migrate_seed", "regulation_points") == "12"
        assert table_count("gr9_migrate_seed", "regulation_candidates") == "58"

        # An unknown dataset id must fail the run and leave the data tables empty.
        bad = migrate_cli(migrate_binary, db, "gr9_migrate_bad", atlas, SEED_CLI_DATASET + ",missing-dataset")
        assert bad.returncode == 1, bad.stdout + bad.stderr
        assert table_count("gr9_migrate_bad", "regulation_points") == "0", "validation must precede any write"
        summary.update(migrate_schema_only="ok", migrate_seed_inserted=json.loads(seeded.stdout.strip().splitlines()[-1]),
                       migrate_seed_replay=json.loads(again.stdout.strip().splitlines()[-1]),
                       migrate_unknown_dataset_rejected_without_writes=True)
        summary["passed"] = True
    finally:
        summary["database_stopped"] = db.stop()
        try:
            for leftover in Path(scratch).glob("*"):
                leftover.unlink()
            Path(scratch).rmdir()
        except OSError:
            pass
    print(json.dumps(summary))
    sys.exit(0 if summary["passed"] and summary["database_stopped"] else 1)


if __name__ == "__main__":
    main()
