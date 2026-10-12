from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "synthetic"
PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def run_cli(cwd: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    current_path = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(PACKAGE_ROOT), current_path) if part
    )
    return subprocess.run(
        [sys.executable, "-B", "-m", "generation_io", *arguments],
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def parse_stdout(result: subprocess.CompletedProcess[str]) -> dict:
    return json.loads(result.stdout)


def parse_stderr(result: subprocess.CompletedProcess[str]) -> dict:
    return json.loads(result.stderr)


def content_hashes(root: Path) -> dict[str, str]:
    return {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        for name in ("source.csv", "boundary.geojson", "input-manifest.json")
    }


def test_subprocess_prepare_generate_verify_resume_verify_is_portable_and_read_only(
    tmp_path: Path,
):
    data_dir = tmp_path / "data"
    shutil.copytree(FIXTURE_DIR, data_dir)
    before = content_hashes(data_dir)
    observed_manifest = data_dir / "observed-manifest.json"

    prepared = run_cli(
        tmp_path,
        "prepare-input",
        "--data-dir",
        "data",
        "--source",
        "source.csv",
        "--boundary",
        "boundary.geojson",
        "--source-month",
        "202701",
        "--region-id",
        "synthetic:rectangle",
        "--manifest-out",
        "data/observed-manifest.json",
        "--synthetic",
    )

    assert prepared.returncode == 0, prepared.stderr
    prepared_json = parse_stdout(prepared)
    assert prepared_json["status"] == "prepared"
    assert prepared_json["schema_version"] == 1
    assert prepared_json["production_approved"] is False
    manifest = json.loads(observed_manifest.read_text(encoding="utf-8"))
    assert manifest["synthetic"] is True
    assert manifest["production_approved"] is False
    assert set(manifest["provenance"].values()) == {"unknown"}
    assert manifest["source"]["encoding"] == "cp932"
    assert manifest["source"]["format_version"] == "k_2.1"
    assert manifest["source"]["header_sha256"]
    assert manifest["boundary"]["crs"] == "EPSG:4326"
    assert "真正性・出所・利用権の証明ではありません" in prepared_json["integrity_note"]
    assert content_hashes(data_dir) == before

    generated = run_cli(
        tmp_path,
        "generate",
        "--data-dir",
        "data",
        "--input-manifest",
        "observed-manifest.json",
        "--region-id",
        "synthetic:rectangle",
        "--output-dir",
        "generated",
    )
    assert generated.returncode == 0, generated.stderr
    generated_json = parse_stdout(generated)
    assert generated_json["status"] == "complete"
    assert generated_json["schema_version"] == 3
    assert generated_json["production_approved"] is False
    generation_manifest = Path(generated_json["manifest"])
    source_report = json.loads(
        (generation_manifest.parent / "source-manifest.json").read_text(encoding="utf-8")
    )
    assert set(source_report["provenance"].values()) == {"unknown"}
    assert generation_manifest == tmp_path / "generated" / "generation-manifest.json"

    verified = run_cli(tmp_path, "verify", "--manifest", str(generation_manifest))
    assert verified.returncode == 0, verified.stderr
    verified_json = parse_stdout(verified)
    assert verified_json["status"] == "verified"
    assert verified_json["schema_version"] == 3
    assert verified_json["production_approved"] is False
    assert verified_json["counts"]["candidate_rows"] == 0

    source_path = data_dir / "source.csv"
    source_path.rename(data_dir / "source.csv.unavailable")
    try:
        resumed = run_cli(
            tmp_path,
            "resume",
            "--manifest",
            str(generation_manifest),
            "--output-dir",
            str(tmp_path / "resumed"),
        )
    finally:
        (data_dir / "source.csv.unavailable").rename(source_path)
    assert resumed.returncode == 0, resumed.stderr
    resumed_json = parse_stdout(resumed)
    assert resumed_json["status"] == "complete"
    assert resumed_json["schema_version"] == 3
    assert resumed_json["production_approved"] is False

    resumed_verified = run_cli(
        tmp_path,
        "verify",
        "--manifest",
        resumed_json["manifest"],
    )
    assert resumed_verified.returncode == 0, resumed_verified.stderr
    assert parse_stdout(resumed_verified)["status"] == "verified"

    moved_output = tmp_path / "moved-generated"
    shutil.move(tmp_path / "generated", moved_output)
    moved_verified = run_cli(
        tmp_path,
        "verify",
        "--manifest",
        str(moved_output / "generation-manifest.json"),
    )
    assert moved_verified.returncode == 0, moved_verified.stderr
    assert content_hashes(data_dir) == before

    duplicate_manifest = run_cli(
        tmp_path,
        "prepare-input",
        "--data-dir",
        "data",
        "--source",
        "source.csv",
        "--boundary",
        "boundary.geojson",
        "--source-month",
        "202701",
        "--region-id",
        "synthetic:rectangle",
        "--manifest-out",
        "data/observed-manifest.json",
    )
    assert duplicate_manifest.returncode == 2
    assert parse_stderr(duplicate_manifest)["error_code"] == "MANIFEST_OUTPUT_EXISTS"
    assert content_hashes(data_dir) == before

    duplicate_output = run_cli(
        tmp_path,
        "generate",
        "--data-dir",
        "data",
        "--input-manifest",
        "observed-manifest.json",
        "--region-id",
        "synthetic:rectangle",
        "--output",
        str(moved_output),
    )
    assert duplicate_output.returncode == 2
    assert parse_stderr(duplicate_output)["error_code"] == "output_exists"


def test_missing_data_and_unsafe_input_paths_report_actionable_json(tmp_path: Path):
    missing = run_cli(
        tmp_path,
        "generate",
        "--data-dir",
        "absent",
        "--region-id",
        "synthetic:rectangle",
        "--output-dir",
        "missing-output",
    )
    assert missing.returncode == 2
    error = parse_stderr(missing)
    assert error["error_code"] == "INPUT_DATA_DIR_INVALID"
    assert error["cause"] and error["action"]
    assert not (tmp_path / "missing-output").exists()

    data_dir = tmp_path / "data"
    shutil.copytree(FIXTURE_DIR, data_dir)
    escaped = run_cli(
        tmp_path,
        "prepare-input",
        "--data-dir",
        "data",
        "--source",
        "../source.csv",
        "--boundary",
        "boundary.geojson",
        "--source-month",
        "202701",
        "--region-id",
        "synthetic:rectangle",
        "--manifest-out",
        "observed.json",
    )
    assert escaped.returncode == 2
    escaped_error = parse_stderr(escaped)
    assert escaped_error["error_code"] == "INPUT_PATH_INVALID"
    assert escaped_error["cause"] and escaped_error["action"]
    assert not (tmp_path / "observed.json").exists()


def test_prepare_input_rejects_existing_manifest_and_input_collision(tmp_path: Path):
    data_dir = tmp_path / "data"
    shutil.copytree(FIXTURE_DIR, data_dir)
    common = (
        "prepare-input",
        "--data-dir",
        "data",
        "--source",
        "source.csv",
        "--boundary",
        "boundary.geojson",
        "--source-month",
        "202701",
        "--region-id",
        "synthetic:rectangle",
    )
    collision = run_cli(
        tmp_path,
        *common,
        "--manifest-out",
        "data/source.csv",
    )
    assert collision.returncode == 2
    assert parse_stderr(collision)["error_code"] == "MANIFEST_INPUT_COLLISION"
    assert (data_dir / "source.csv").is_file()

    manifest_path = tmp_path / "data" / "new-manifest.json"
    manifest_path.write_text("existing", encoding="utf-8")
    existing = run_cli(
        tmp_path,
        *common,
        "--manifest-out",
        "data/new-manifest.json",
    )
    assert existing.returncode == 2
    assert parse_stderr(existing)["error_code"] == "MANIFEST_OUTPUT_EXISTS"
    assert manifest_path.read_text(encoding="utf-8") == "existing"


def test_prepare_input_rejects_symlink_bad_month_header_and_unsupported_crs(
    tmp_path: Path,
):
    data_dir = tmp_path / "data"
    shutil.copytree(FIXTURE_DIR, data_dir)
    common = (
        "prepare-input",
        "--data-dir",
        "data",
        "--source",
        "source.csv",
        "--boundary",
        "boundary.geojson",
        "--source-month",
        "202701",
        "--region-id",
        "synthetic:rectangle",
        "--manifest-out",
        "observed.json",
    )

    (data_dir / "linked.csv").symlink_to("source.csv")
    linked = run_cli(
        tmp_path,
        *common[:4],
        "linked.csv",
        *common[5:],
    )
    assert linked.returncode == 2
    assert parse_stderr(linked)["error_code"] == "INPUT_PATH_SYMLINK"
    (data_dir / "linked.csv").unlink()

    invalid_month = run_cli(
        tmp_path,
        *common[:8],
        "202713",
        *common[9:],
    )
    assert invalid_month.returncode == 2
    assert parse_stderr(invalid_month)["error_code"] == "INVALID_SOURCE_MONTH"

    source_path = data_dir / "source.csv"
    source_text = source_path.read_text(encoding="cp932")
    header_column = "拡張版標準フォーマット種別・バージョン"
    assert header_column in source_text
    source_path.write_bytes(source_text.replace(header_column, "invalid_header", 1).encode("cp932"))
    unsupported_source = run_cli(tmp_path, *common)
    assert unsupported_source.returncode == 2
    assert parse_stderr(unsupported_source)["error_code"] == "header_mismatch"
    source_path.write_bytes(FIXTURE_DIR.joinpath("source.csv").read_bytes())

    boundary_path = data_dir / "boundary.geojson"
    boundary = json.loads(boundary_path.read_text(encoding="utf-8"))
    boundary["crs"] = {"type": "name", "properties": {"name": "EPSG:3857"}}
    boundary_path.write_text(json.dumps(boundary), encoding="utf-8")
    unsupported_crs = run_cli(tmp_path, *common)
    assert unsupported_crs.returncode == 2
    assert parse_stderr(unsupported_crs)["error_code"] == "unknown_crs"


def test_argparse_errors_are_json_japanese_and_exit_two(tmp_path: Path):
    result = run_cli(tmp_path, "generate")

    assert result.returncode == 2
    assert result.stdout == ""
    error = parse_stderr(result)
    assert error["error_code"] == "CLI_ARGUMENT_INVALID"
    assert "引数" in error["cause"]
    assert "確認" in error["action"]


def test_legacy_research_options_are_explicitly_separate_from_portable_generate(
    tmp_path: Path,
):
    portable_help = run_cli(tmp_path, "generate", "--help")
    legacy_help = run_cli(tmp_path, "legacy-generate", "--help")

    assert portable_help.returncode == 0
    assert "--data-dir" in portable_help.stdout
    assert "--ownership-receipt" not in portable_help.stdout
    assert legacy_help.returncode == 0
    assert "--source-contract" in legacy_help.stdout
    assert "--ownership-receipt" in legacy_help.stdout