from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, NoReturn, Sequence

from .artifacts import (
    ArtifactError,
    generate_candidate_artifact,
    resume_candidate_artifact,
    verify_generation_manifest,
)
from .input_manifest import InputError, _source_error_message
from .portable_artifacts import (
    PortableArtifactError,
    generate_portable_artifact,
    resume_portable_artifact,
    verify_portable_manifest,
)
from .prepare_input import prepare_input_manifest
from .region import RegionError
from .source import SourceError


_ERROR_MESSAGES: dict[str, tuple[str, str]] = {
    "INPUT_DATA_DIR_INVALID": (
        "指定data-dirを読み取れる入力directoryとして解決できません",
        "CSVと境界を置いた既存directoryを--data-dirに指定してください",
    ),
    "INPUT_MANIFEST_NOT_FOUND": (
        "data-dir内にinput manifestがありません",
        "prepare-inputで別manifestを作成するか、--input-manifest pathを確認してください",
    ),
    "SOURCE_CSV_NOT_FOUND": (
        "input manifestが参照するsource CSVが見つかりません",
        "CSVをdata-dir内に配置しmanifestの相対pathを確認してください",
    ),
    "BOUNDARY_NOT_FOUND": (
        "input manifestが参照する境界GeoJSONが見つかりません",
        "境界をdata-dir内に配置しmanifestの相対pathを確認してください",
    ),
    "output_exists": (
        "指定output pathがすでに存在します",
        "既存outputを変更せず、存在しない新しい--output-dirを指定してください",
    ),
    "output_parent_missing": (
        "outputの親directoryが存在しません",
        "先に親directoryを作成し、その中の新しいoutput pathを指定してください",
    ),
    "input_output_overlap": (
        "outputが入力dataまたは既存artifactと重なります",
        "入力/stateを変更しない別の新規output directoryを指定してください",
    ),
    "region_id_mismatch": (
        "指定region-idがinput manifestのregion-idと一致しません",
        "manifestと同じregion-idを指定してください",
    ),
    "generation_manifest_schema_mismatch": (
        "generation manifestのschemaがportable schema 3と一致しません",
        "schema 3で生成したgeneration-manifest.jsonを指定してください",
    ),
    "unsupported_manifest_schema": (
        "generation manifestのschema versionに対応していません",
        "対応するportable schema 3のmanifestを指定してください",
    ),
    "output_budget_exceeded": (
        "artifact出力の合計が32 MiBの上限を超えました",
        "対象データ量を確認し、上限内の入力で再実行してください",
    ),
    "report_budget_exceeded": (
        "manifest等のreport出力が8 MiBの上限を超えました",
        "provenance等のmetadata量を確認してください",
    ),
    "path_invalid": (
        "pathが安全なportable pathではありません",
        "symlinkや..を避け、通常の相対pathまたは絶対pathを指定してください",
    ),
    "INPUT_PATH_INVALID": (
        "入力pathが安全なdata-dir相対pathではありません",
        "source/boundary/manifestの相対pathからabsolute pathと..を除いてください",
    ),
    "INPUT_PATH_SYMLINK": (
        "入力pathにsymlinkが含まれています",
        "symlinkを使わずdata-dir内へregular fileを配置してください",
    ),
    "BOUNDARY_CRS_UNSUPPORTED": (
        "境界manifestのCRSがWGS84/CRS84ではありません",
        "EPSG:4326またはloader対応のCRS84を使ってmanifestを作り直してください",
    ),
    "invalid_boundary_json": (
        "境界GeoJSONを検証できません",
        "UTF-8のGeoJSONと対応geometry/CRSを確認してください",
    ),
    "unsupported_boundary_geometry": (
        "境界geometry typeに対応していません",
        "PolygonまたはMultiPolygonのGeoJSONを指定してください",
    ),
    "MANIFEST_OUTPUT_EXISTS": (
        "manifest-outがすでに存在します",
        "既存manifestを上書きせず、別の新しいpathを指定してください",
    ),
    "MANIFEST_INPUT_COLLISION": (
        "manifest-outが入力CSVまたは境界と同じpathです",
        "入力を変更しない別のmanifest-out pathを指定してください",
    ),
    "INPUT_FORMAT_UNSUPPORTED": (
        "入力fileの拡張子が未対応です",
        "sourceには.csv、boundaryには.geojsonを指定してください",
    ),
    "HEADER_MISMATCH": (
        "CSV headerが対応する170列schemaと一致しません",
        "CP932のk_2.1対応headerを確認してください",
    ),
    "UNSUPPORTED_FORMAT_VERSION": (
        "CSVが対応形式k_2.1ではありません",
        "k_2.1形式のCSVを指定してください",
    ),
    "INVALID_SOURCE_MONTH": (
        "source-monthが有効なYYYYMMではありません",
        "年4桁と01〜12の月を指定してください",
    ),
    "CLI_ARGUMENT_INVALID": (
        "コマンド引数を解釈できませんでした",
        "コマンド名と各必須optionを確認し、--helpで使い方を表示してください",
    ),
}


class _JapaneseArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("add_help", False)
        super().__init__(*args, **kwargs)
        self.add_argument("-h", "--help", action="help", help="このヘルプを表示して終了")

    def error(self, message: str) -> NoReturn:
        del message  # Do not echo raw arguments or implementation details.
        _print_error("CLI_ARGUMENT_INVALID", *_ERROR_MESSAGES["CLI_ARGUMENT_INVALID"])
        raise SystemExit(2)


def build_parser() -> argparse.ArgumentParser:
    parser = _JapaneseArgumentParser(
        prog="generation_io",
        description="外部入力からportable candidate-only artifactを生成・検証します。",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    generate = commands.add_parser(
        "generate",
        help="外部input manifestからschema 3 artifactを新規生成",
        description="外部CSVと境界をread-onlyで検証して候補artifactを生成します。",
    )
    generate.add_argument("--data-dir", required=True, help="CSV・境界・input manifestのdirectory")
    generate.add_argument(
        "--input-manifest",
        default="input-manifest.json",
        help="data-dir相対のschema 1 manifest (default: input-manifest.json)",
    )
    generate.add_argument("--region-id", required=True, help="manifestと一致するregion ID")
    generate.add_argument(
        "--output-dir",
        "--output",
        dest="output",
        required=True,
        help="存在しないoutput directory (--outputは互換alias)",
    )
    generate.add_argument("--ownership-receipt", help=argparse.SUPPRESS)
    generate.add_argument("--published-receipt", help=argparse.SUPPRESS)
    generate.add_argument("--invocation-id", help=argparse.SUPPRESS)

    verify = commands.add_parser(
        "verify",
        help="portable generation manifestとartifactをread-only検証",
        description="外部CSV・境界なしでartifactを検証します。",
    )
    verify.add_argument("--manifest", required=True, help="generation-manifest.jsonのpath")

    resume = commands.add_parser(
        "resume",
        help="既存artifact stateからkernel部分だけを新規再生成",
        description="検証済みportable artifactからraw CSVを再読込せず新しいstateを作ります。",
    )
    resume.add_argument("--manifest", required=True, help="元generation-manifest.jsonのpath")
    resume.add_argument(
        "--output-dir",
        "--output",
        dest="output",
        required=True,
        help="存在しないoutput directory (--outputは互換alias)",
    )
    resume.add_argument("--ownership-receipt", help=argparse.SUPPRESS)
    resume.add_argument("--published-receipt", help=argparse.SUPPRESS)
    resume.add_argument("--invocation-id", help=argparse.SUPPRESS)

    legacy_generate = commands.add_parser(
        "legacy-generate",
        help="固定projection研究用のschema 2 artifactを生成 (legacy)",
        description="固定projection研究CLIです。portable generateとは入力契約が異なります。",
    )
    legacy_generate.add_argument("--source", required=True, help="固定selected-projection JSONL")
    legacy_generate.add_argument("--source-contract", required=True, help="固定projection contract JSON")
    legacy_generate.add_argument("--input-kind", required=True, help="legacy input kind")
    legacy_generate.add_argument("--boundary", required=True, help="legacy GeoJSON boundary")
    legacy_generate.add_argument("--region-id", required=True, help="legacy fixture region ID")
    legacy_generate.add_argument("--output", required=True, help="新しいlegacy output directory")
    legacy_generate.add_argument("--ownership-receipt", help="legacy supervisor ownership receipt")
    legacy_generate.add_argument("--invocation-id", help="legacy invocation ID")

    legacy_verify = commands.add_parser(
        "legacy-verify",
        help="固定projection研究用のschema 2 manifestを検証 (legacy)",
    )
    legacy_verify.add_argument("--manifest", required=True, help="legacy generation manifest")

    legacy_resume = commands.add_parser(
        "legacy-resume",
        help="固定projection研究用artifactをresume (legacy)",
    )
    legacy_resume.add_argument("--manifest", required=True, help="legacy generation manifest")
    legacy_resume.add_argument("--output", required=True, help="新しいlegacy output directory")

    prepare = commands.add_parser(
        "prepare-input",
        help="外部CSV・GeoJSONの観測値を別schema 1 manifestへ記録",
        description="入力を変更せず、hash/CRCの観測値と利用者提供metadataを記録します。",
    )
    prepare.add_argument("--data-dir", required=True, help="外部入力directory")
    prepare.add_argument("--source", required=True, help="data-dir相対のk_2.1 CSV path")
    prepare.add_argument("--boundary", required=True, help="data-dir相対のGeoJSON path")
    prepare.add_argument("--source-month", required=True, help="source month (YYYYMM)")
    prepare.add_argument("--region-id", required=True, help="境界に対応するregion ID")
    prepare.add_argument("--manifest-out", required=True, help="exclusive createする別manifest path")
    for option, label in (
        ("--dataset", "dataset名"),
        ("--publisher", "publisher名"),
        ("--edition", "edition"),
        ("--retrieved-at", "取得日時の利用者申告値"),
        ("--terms-reference", "利用者申告の規約参照"),
        ("--rights-status", "利用者申告の権利状態"),
    ):
        prepare.add_argument(option, default="unknown", help=f"{label} (default: unknown)")
    prepare.add_argument(
        "--synthetic",
        action="store_true",
        help="利用者が合成入力と申告するときだけ指定 (production approvalはfalse)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "prepare-input":
            observed = prepare_input_manifest(
                args.data_dir,
                args.source,
                args.boundary,
                args.source_month,
                args.region_id,
                args.manifest_out,
                dataset=args.dataset,
                publisher=args.publisher,
                edition=args.edition,
                retrieved_at=args.retrieved_at,
                terms_reference=args.terms_reference,
                rights_status=args.rights_status,
                synthetic=args.synthetic,
            )
            _print_success({"status": "prepared", **observed})
            return 0

        if args.command == "legacy-generate":
            manifest_path = generate_candidate_artifact(
                source=args.source,
                source_contract=args.source_contract,
                input_kind=args.input_kind,
                boundary=args.boundary,
                region_id=args.region_id,
                output=args.output,
                ownership_receipt=args.ownership_receipt,
                invocation_id=args.invocation_id,
            )
            legacy_manifest = verify_generation_manifest(manifest_path)
            _print_success(
                {
                    "status": "complete",
                    "manifest": str(manifest_path),
                    "schema_version": legacy_manifest.get("schema_version", 2),
                    "production_approved": False,
                }
            )
            return 0

        if args.command == "legacy-resume":
            manifest_path = resume_candidate_artifact(
                manifest=args.manifest,
                output=args.output,
            )
            legacy_manifest = verify_generation_manifest(manifest_path)
            _print_success(
                {
                    "status": "complete",
                    "manifest": str(manifest_path),
                    "schema_version": legacy_manifest.get("schema_version", 2),
                    "production_approved": False,
                }
            )
            return 0

        if args.command == "legacy-verify":
            legacy_manifest = verify_generation_manifest(args.manifest)
            _print_success(
                {
                    "status": "verified",
                    "manifest": str(Path(args.manifest)),
                    "schema_version": legacy_manifest.get("schema_version", 2),
                    "task_id": legacy_manifest.get("task_id"),
                    "rows_total": legacy_manifest.get("rows_total"),
                    "candidate_rows": legacy_manifest.get("candidate_rows"),
                    "production_approved": False,
                }
            )
            return 0

        if args.command == "generate":
            input_manifest = _input_manifest_argument(args.data_dir, args.input_manifest)
            manifest_path = generate_portable_artifact(
                args.data_dir,
                input_manifest,
                region_id=args.region_id,
                output=args.output,
                ownership_receipt=args.ownership_receipt,
                published_receipt=args.published_receipt,
                invocation_id=args.invocation_id,
            )
            manifest = verify_portable_manifest(manifest_path)
            _print_success(
                {
                    "status": "complete",
                    "manifest": str(manifest_path),
                    "schema_version": manifest["schema_version"],
                    "region_id": manifest["region_id"],
                    "source_month": manifest["source_month"],
                    "counts": manifest["counts"],
                    "production_approved": False,
                }
            )
            return 0

        if args.command == "resume":
            manifest_path = resume_portable_artifact(
                args.manifest,
                args.output,
                ownership_receipt=args.ownership_receipt,
                published_receipt=args.published_receipt,
                invocation_id=args.invocation_id,
            )
            manifest = verify_portable_manifest(manifest_path)
            _print_success(
                {
                    "status": "complete",
                    "manifest": str(manifest_path),
                    "schema_version": manifest["schema_version"],
                    "region_id": manifest["region_id"],
                    "source_month": manifest["source_month"],
                    "counts": manifest["counts"],
                    "production_approved": False,
                }
            )
            return 0

        manifest = verify_portable_manifest(args.manifest)
        _print_success(
            {
                "status": "verified",
                "manifest": str(Path(args.manifest)),
                "schema_version": manifest["schema_version"],
                "region_id": manifest["region_id"],
                "source_month": manifest["source_month"],
                "counts": manifest["counts"],
                "production_approved": False,
            }
        )
        return 0
    except InputError as exc:
        _print_error(exc.code, exc.cause, exc.action)
    except SourceError as exc:
        cause, action = _source_error_message(exc.code)
        _print_error(exc.code, cause, action)
    except (ArtifactError, PortableArtifactError, RegionError) as exc:
        code = getattr(exc, "code", "generation_failed")
        cause, action = _ERROR_MESSAGES.get(
            code,
            (
                "portable artifactの検証または生成に失敗しました",
                "error_codeに対応する入力・manifest・output条件を確認して再実行してください",
            ),
        )
        _print_error(code, cause, action)
    except (OSError, TypeError, ValueError) as exc:
        del exc
        _print_error(
            "CLI_PATH_OR_VALUE_INVALID",
            "pathまたは指定値を安全に解釈できませんでした",
            "入力path、manifest、region-idを確認してください",
        )
    except Exception:
        # Do not print exception text or tracebacks: they could contain CSV data.
        _print_error(
            "CLI_FAILED",
            "内部処理で予期しない失敗が発生しました",
            "個別CSV値は表示せず処理を停止しました。error_codeと入力形式を確認してください",
        )
    return 2


def _input_manifest_argument(data_dir: str, value: str) -> str:
    raw = Path(value)
    if not raw.is_absolute():
        return value
    try:
        root = Path(data_dir).resolve(strict=True)
        absolute = Path(os.path.abspath(raw))
        relative = absolute.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise InputError(
            "INPUT_PATH_INVALID",
            "absolute input manifest pathがdata-dir内にありません",
            "data-dir内のmanifest pathを指定してください",
        ) from exc
    return relative.as_posix()


def _print_success(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _print_error(code: str, cause: str, action: str) -> None:
    print(
        json.dumps(
            {"status": "failed", "error_code": code, "cause": cause, "action": action},
            ensure_ascii=False,
            sort_keys=True,
        ),
        file=sys.stderr,
    )


if __name__ == "__main__":
    raise SystemExit(main())
