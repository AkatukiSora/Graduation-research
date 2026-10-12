from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .artifacts import (
    ArtifactError,
    generate_candidate_artifact,
    resume_candidate_artifact,
    verify_generation_manifest,
)
from .region import RegionError
from .source import SourceError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="generation_io")
    commands = parser.add_subparsers(dest="command", required=True)

    generate = commands.add_parser("generate")
    generate.add_argument("--source", required=True)
    generate.add_argument("--source-contract", required=True)
    generate.add_argument("--input-kind", required=True)
    generate.add_argument("--boundary", required=True)
    generate.add_argument("--region-id", required=True)
    generate.add_argument("--output", required=True)
    generate.add_argument("--ownership-receipt")
    generate.add_argument("--invocation-id")

    verify = commands.add_parser("verify")
    verify.add_argument("--manifest", required=True)

    resume = commands.add_parser("resume")
    resume.add_argument("--manifest", required=True)
    resume.add_argument("--output", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "generate":
            manifest = generate_candidate_artifact(
                source=args.source,
                source_contract=args.source_contract,
                input_kind=args.input_kind,
                boundary=args.boundary,
                region_id=args.region_id,
                output=args.output,
                ownership_receipt=args.ownership_receipt,
                invocation_id=args.invocation_id,
            )
            print(json.dumps({"status": "complete", "manifest": str(manifest)}, ensure_ascii=False))
            return 0
        if args.command == "resume":
            manifest_path = resume_candidate_artifact(
                manifest=Path(args.manifest), output=args.output
            )
            print(json.dumps({"status": "complete", "manifest": str(manifest_path)}, ensure_ascii=False))
            return 0
        manifest = verify_generation_manifest(Path(args.manifest))
        print(
            json.dumps(
                {
                    "status": "verified",
                    "task_id": manifest["task_id"],
                    "rows_total": manifest["rows_total"],
                    "candidate_rows": manifest["candidate_rows"],
                    "production_approved": False,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    except (ArtifactError, SourceError, RegionError) as exc:
        code = getattr(exc, "code", "generation_failed")
        print(json.dumps({"status": "failed", "error_code": code}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
