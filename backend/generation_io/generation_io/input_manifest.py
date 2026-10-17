"""Read-only loader and streaming reader for external input manifests.

Schema 1 is intentionally limited to the observed k_2.1/CP932/170-column
source schema. Hashes establish byte integrity only; provenance and rights
values are preserved verbatim and are never interpreted as approval.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import zlib
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, BinaryIO, Iterator

from .source import HEADER_SHA256, SOURCE_VERSION, SourceError, _iter_raw_records


_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_CRC32_RE = re.compile(r"[0-9a-f]{8}\Z", re.ASCII)
_MONTH_RE = re.compile(r"([0-9]{4})(0[1-9]|1[0-2])\Z", re.ASCII)
_SUPPORTED_CRS = {
    "EPSG:4326",
    "URN:OGC:DEF:CRS:EPSG::4326",
    "URN:OGC:DEF:CRS:OGC::CRS84",
    "URN:OGC:DEF:CRS:OGC:1.3:CRS84",
    "OGC:CRS84",
}
_TOP_FIELDS = {
    "schema_version",
    "source",
    "boundary",
    "provenance",
    "synthetic",
}
_SOURCE_FIELDS = {
    "path",
    "bytes",
    "sha256",
    "crc32",
    "source_month",
    "format_version",
    "encoding",
    "header_sha256",
}
_BOUNDARY_FIELDS = {"path", "bytes", "sha256", "crs", "region_id"}
_PROVENANCE_FIELDS = {
    "dataset",
    "publisher",
    "edition",
    "retrieved_at",
    "terms_reference",
    "rights_status",
}


class InputError(ValueError):
    """Actionable error from the public portable-input API."""

    def __init__(self, code: str, cause: str, action: str):
        self.code = code
        self.cause = cause
        self.action = action
        super().__init__(f"{code}: 原因: {cause}。対処: {action}。")


@dataclass(frozen=True)
class InputManifest:
    """Validated manifest plus its confined, resolved input file paths.

    ``manifest`` preserves the caller-supplied schema-1 values, including
    unknown provenance values. ``source_path`` and ``boundary_path`` are
    absolute paths resolved below ``data_dir``; callers should not write to
    any of these input paths.
    """

    data_dir: Path
    manifest: dict[str, Any]
    source_path: Path
    boundary_path: Path


def load_input_manifest(
    data_dir: str | os.PathLike[str],
    manifest_name: str = "input-manifest.json",
) -> InputManifest:
    """Validate a schema-1 input manifest and both referenced files read-only.

    Paths in the manifest and ``manifest_name`` must be relative to
    ``data_dir`` and may not traverse ``..`` or use symlinks. Source and
    boundary bytes/SHA-256 are streamed; source CRC32 is streamed as well.
    Boundary GeoJSON contents/geometry are deliberately left for the region
    API to validate, while manifest CRS and region ID are checked here.
    """
    try:
        root = Path(data_dir).resolve(strict=True)
    except (OSError, RuntimeError, TypeError) as exc:
        raise InputError(
            "INPUT_DATA_DIR_INVALID",
            "指定data-dirを読み取れるディレクトリとして解決できません",
            "存在する入力ディレクトリを指定してください",
        ) from exc
    if not root.is_dir():
        raise InputError(
            "INPUT_DATA_DIR_INVALID",
            "指定data-dirがディレクトリではありません",
            "CSVと境界を置いたディレクトリを指定してください",
        )

    manifest_relative = _validate_relative_path(manifest_name, "manifest_name")
    manifest_path = _resolve_regular_file(
        root,
        manifest_relative,
        missing_code="INPUT_MANIFEST_NOT_FOUND",
        display_name="input manifest",
    )
    try:
        with _open_regular_readonly(manifest_path) as stream:
            raw_manifest = stream.read()
        parsed = json.loads(
            raw_manifest.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonstandard_number,
        )
    except InputError:
        raise
    except OSError as exc:
        raise InputError(
            "INPUT_MANIFEST_READ_ERROR",
            "manifestをread-onlyで開けません",
            "manifestのpath権限とdata-dirを確認してください",
        ) from exc
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise InputError(
            "INPUT_MANIFEST_INVALID",
            "manifestがUTF-8 JSONではないか、JSON keyが重複しています",
            "重複keyを除きschema 1のJSONとして保存し直してください",
        ) from exc

    source_relative, boundary_relative = _validate_manifest(parsed)
    # Validate both lexical paths before either referenced file is looked up.
    source_path = _resolve_regular_file(
        root,
        source_relative,
        missing_code="SOURCE_CSV_NOT_FOUND",
        display_name="source CSV",
    )
    boundary_path = _resolve_regular_file(
        root,
        boundary_relative,
        missing_code="BOUNDARY_NOT_FOUND",
        display_name="boundary file",
    )

    source = parsed["source"]
    boundary = parsed["boundary"]
    _verify_file(
        source_path,
        expected_bytes=source["bytes"],
        expected_sha256=source["sha256"],
        expected_crc32=source["crc32"],
        prefix="SOURCE",
    )
    _verify_file(
        boundary_path,
        expected_bytes=boundary["bytes"],
        expected_sha256=boundary["sha256"],
        expected_crc32=None,
        prefix="BOUNDARY",
    )
    return InputManifest(
        data_dir=root,
        manifest=parsed,
        source_path=source_path,
        boundary_path=boundary_path,
    )


def iter_portable_raw_records(
    source_path: str | os.PathLike[str], manifest: InputManifest
) -> Iterator[dict[str, Any]]:
    """Yield full raw rows, selected fields, and unchanged provenance.

    ``manifest`` must be the validated ``InputManifest`` returned by
    :func:`load_input_manifest`, and ``source_path`` must be its
    ``source_path``. CSV integrity is rechecked as a streaming pass immediately
    before CP932 parsing. The iterator keeps only the current row and the set
    of seen UIDs in memory; it never materializes the full CSV.
    """
    if not isinstance(manifest, InputManifest):
        raise InputError(
            "INPUT_MANIFEST_UNVALIDATED",
            "CSV readerに未検証のmanifestが渡されました",
            "load_input_manifestの返値をmanifestとして渡してください",
        )
    try:
        requested = Path(source_path)
        if not requested.is_absolute():
            requested = Path.cwd() / requested
    except (TypeError, OSError) as exc:
        raise InputError(
            "SOURCE_PATH_INVALID",
            "source_pathをファイルpathとして解釈できません",
            "load_input_manifestが返すsource_pathをそのまま指定してください",
        ) from exc
    if requested != manifest.source_path:
        raise InputError(
            "SOURCE_PATH_MISMATCH",
            "source_pathが検証済みmanifestのCSV pathと一致しません",
            "load_input_manifestが返すsource_pathをそのまま指定してください",
        )

    source = manifest.manifest["source"]
    contract = {
        "source_month": source["source_month"],
        "version": source["format_version"],
        "header_sha256": source["header_sha256"],
        "csv_sha256": source["sha256"],
        "csv_crc32": source["crc32"],
        "csv_bytes": source["bytes"],
    }
    try:
        # Re-resolve and reject a replaced symlink/file before opening it.
        relative = _validate_relative_path(source["path"], "source.path")
        actual_path = _resolve_regular_file(
            manifest.data_dir,
            relative,
            missing_code="SOURCE_CSV_NOT_FOUND",
            display_name="source CSV",
        )
        if actual_path != manifest.source_path:
            raise InputError(
                "SOURCE_PATH_MISMATCH",
                "manifestのsource pathが検証後に変更されました",
                "入力manifestを読み直してからCSVを再検証してください",
            )
        stream = _open_regular_readonly(actual_path)
        records = _iter_raw_records(
            stream,
            contract,
            expected_month=source["source_month"],
            include_raw=True,
        )
        try:
            for record in records:
                yield {
                    "source_row": record["source_row"],
                    "raw": record["raw"],
                    "selected": record["selected"],
                    "source_month": source["source_month"],
                    "format_version": source["format_version"],
                    "provenance": manifest.manifest["provenance"],
                    "synthetic": manifest.manifest["synthetic"],
                    "production_approved": manifest.manifest.get(
                        "production_approved", False
                    ),
                }
        finally:
            records.close()
            stream.close()
    except SourceError as exc:
        cause, action = _source_error_message(exc.code)
        raise InputError(exc.code, cause, action) from exc
    except InputError:
        raise
    except OSError as exc:
        raise InputError(
            "SOURCE_READ_ERROR",
            "CSVを読み取り専用で開けないか、読み取り中にI/O errorが発生しました",
            "ファイルの存在・権限・manifestのhashを確認してください",
        ) from exc


def _validate_manifest(value: Any) -> tuple[Path, Path]:
    if not isinstance(value, dict):
        _fail("INPUT_MANIFEST_INVALID", "manifestの最上位がobjectではありません", "schema 1のobjectを指定してください")
    allowed_top = _TOP_FIELDS | {"production_approved"}
    if set(value) - allowed_top or not _TOP_FIELDS.issubset(value):
        _fail("INPUT_MANIFEST_INVALID", "manifestの必須fieldまたはfield構成が不正です", "schema 1の必須fieldだけを指定してください")
    schema_version = value["schema_version"]
    if isinstance(schema_version, bool) or schema_version != 1 or not isinstance(schema_version, int):
        _fail("INPUT_MANIFEST_SCHEMA_UNSUPPORTED", "schema_versionが1ではありません", "schema_versionを整数1にしてください")
    if not isinstance(value["synthetic"], bool):
        _fail("INPUT_MANIFEST_INVALID", "syntheticがbooleanではありません", "syntheticをtrueまたはfalseで明示してください")
    if "production_approved" in value and value["production_approved"] is not False:
        _fail("PRODUCTION_APPROVAL_FORBIDDEN", "D1はproduction approvalを付与・受理しません", "production_approvedをfalseにし、権利判断は別途行ってください")

    source = value["source"]
    boundary = value["boundary"]
    provenance = value["provenance"]
    if not isinstance(source, dict) or set(source) != _SOURCE_FIELDS:
        _fail("INPUT_MANIFEST_INVALID", "source field構成がschema 1と一致しません", "sourceの全必須fieldを指定してください")
    if not isinstance(boundary, dict) or set(boundary) != _BOUNDARY_FIELDS:
        _fail("INPUT_MANIFEST_INVALID", "boundary field構成がschema 1と一致しません", "boundaryの全必須fieldを指定してください")
    if not isinstance(provenance, dict) or set(provenance) != _PROVENANCE_FIELDS:
        _fail("INPUT_MANIFEST_INVALID", "provenance field構成がschema 1と一致しません", "provenanceの全fieldを明示し、不明値は文字列unknownとして記録してください")
    if any(not isinstance(item, str) for item in provenance.values()):
        _fail("INPUT_MANIFEST_INVALID", "provenanceのfieldが文字列ではありません", "provenanceの各値を文字列で記録してください")

    source_relative = _validate_relative_path(source["path"], "source.path")
    boundary_relative = _validate_relative_path(boundary["path"], "boundary.path")
    _validate_byte_count(source["bytes"], "source.bytes")
    _validate_byte_count(boundary["bytes"], "boundary.bytes")
    _validate_hex(source["sha256"], _SHA256_RE, "source.sha256", "INVALID_SHA256")
    _validate_hex(source["crc32"], _CRC32_RE, "source.crc32", "INVALID_CRC32")
    _validate_hex(source["header_sha256"], _SHA256_RE, "source.header_sha256", "INVALID_HEADER_SHA256")
    _validate_hex(boundary["sha256"], _SHA256_RE, "boundary.sha256", "INVALID_SHA256")

    month = source["source_month"]
    match = _MONTH_RE.fullmatch(month) if isinstance(month, str) else None
    if match is None or int(match.group(1)) == 0:
        _fail("INVALID_SOURCE_MONTH", "source_monthが有効なYYYYMMではありません", "年4桁と01〜12の月を指定してください")
    if source["format_version"] != SOURCE_VERSION:
        _fail("UNSUPPORTED_FORMAT_VERSION", "format_versionはk_2.1以外です", "現対応のk_2.1 CSVのみを指定してください")
    if source["encoding"] != "cp932":
        _fail("UNSUPPORTED_ENCODING", "encodingはcp932以外です", "CP932 strictのCSVを指定してください")
    if source["header_sha256"] != HEADER_SHA256:
        _fail("HEADER_MISMATCH", "header_sha256が対応する170列schemaと一致しません", "現対応headerのSHA-256を確認してください")

    crs = boundary["crs"]
    region_id = boundary["region_id"]
    if not isinstance(crs, str) or crs.upper() not in _SUPPORTED_CRS:
        _fail("BOUNDARY_CRS_UNSUPPORTED", "boundary CRSがWGS84/CRS84ではありません", "EPSG:4326または対応するCRS84 aliasを指定してください")
    if not isinstance(region_id, str) or not region_id.strip():
        _fail("BOUNDARY_REGION_ID_INVALID", "boundary region_idが空または文字列以外です", "boundaryと対応するregion_idを文字列で指定してください")
    return source_relative, boundary_relative


def _validate_relative_path(value: Any, field: str) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        _fail("INPUT_PATH_INVALID", f"{field}が空または文字列ではありません", "data-dir相対のregular file pathを指定してください")
    windows_path = PureWindowsPath(value)
    # Backslashes are rejected rather than interpreted differently on hosts.
    normalized_parts = value.replace("\\", "/").split("/")
    if (
        Path(value).is_absolute()
        or windows_path.is_absolute()
        or bool(windows_path.drive)
        or any(part == ".." for part in normalized_parts)
        or "\\" in value
    ):
        _fail("INPUT_PATH_INVALID", f"{field}がabsolute pathまたはtraversalを含みます", "data-dir内の相対pathを指定し、..を除いてください")
    relative = Path(value)
    if any(part in {"", ".", ".."} for part in relative.parts):
        _fail("INPUT_PATH_INVALID", f"{field}が安全な相対pathではありません", "通常のdata-dir相対pathを指定してください")
    return relative


def _resolve_regular_file(
    root: Path, relative: Path, *, missing_code: str, display_name: str
) -> Path:
    candidate = root
    for index, part in enumerate(relative.parts):
        candidate = candidate / part
        try:
            mode = candidate.lstat().st_mode
        except OSError as exc:
            if isinstance(exc, FileNotFoundError):
                raise InputError(
                    missing_code,
                    f"{display_name}がdata-dir内にありません",
                    f"manifestのpathと入力ファイルの配置を確認してください",
                ) from exc
            raise InputError(
                "INPUT_FILE_UNREADABLE",
                f"{display_name}のpath情報を確認できません",
                "data-dir内のpath権限を確認してください",
            ) from exc
        if stat.S_ISLNK(mode):
            raise InputError(
                "INPUT_PATH_SYMLINK",
                f"{display_name}のpathにsymlinkが含まれています",
                "symlinkを使わずdata-dir内へregular fileを配置してください",
            )
        is_final = index == len(relative.parts) - 1
        if not is_final and not stat.S_ISDIR(mode):
            raise InputError(
                "INPUT_PATH_INVALID",
                f"{display_name}の親pathがdirectoryではありません",
                "data-dir内のdirectory構成を確認してください",
            )
        if is_final and not stat.S_ISREG(mode):
            raise InputError(
                "INPUT_FILE_NOT_REGULAR",
                f"{display_name}がregular fileではありません",
                "通常のファイルを指定してください",
            )
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise InputError(
            "INPUT_PATH_INVALID",
            f"{display_name}がdata-dir外へ解決されます",
            "data-dir内に実体があるregular fileを指定してください",
        ) from exc
    return resolved


def _open_regular_readonly(path: Path) -> BinaryIO:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise InputError(
                "INPUT_FILE_NOT_REGULAR",
                "open後の入力がregular fileではありません",
                "通常の入力ファイルを指定してください",
            )
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


def _verify_file(
    path: Path,
    *,
    expected_bytes: int,
    expected_sha256: str,
    expected_crc32: str | None,
    prefix: str,
) -> None:
    digest = hashlib.sha256()
    crc = 0
    size = 0
    try:
        with _open_regular_readonly(path) as stream:
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                digest.update(chunk)
                if expected_crc32 is not None:
                    crc = zlib.crc32(chunk, crc)
    except InputError:
        raise
    except OSError as exc:
        raise InputError(
            f"{prefix}_READ_ERROR",
            "入力ファイルをread-onlyで読み取れません",
            "ファイル権限とdata-dirを確認してください",
        ) from exc
    if size != expected_bytes:
        raise InputError(
            f"{prefix}_SIZE_MISMATCH",
            f"{prefix.lower()} fileのbyte数がmanifestと一致しません",
            "manifestの観測byte数と対象fileを照合してください",
        )
    if digest.hexdigest() != expected_sha256:
        raise InputError(
            f"{prefix}_HASH_MISMATCH",
            f"{prefix.lower()} fileのSHA-256がmanifestと一致しません",
            "提供されたhashとfileを再確認してください",
        )
    if expected_crc32 is not None and f"{crc & 0xffffffff:08x}" != expected_crc32:
        raise InputError(
            f"{prefix}_CRC_MISMATCH",
            "source CSVのCRC32がmanifestと一致しません",
            "manifestのCRC32とCSVを照合してください",
        )


def _validate_byte_count(value: Any, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _fail("INPUT_MANIFEST_INVALID", f"{field}が非負integerではありません", "bytesを0以上の整数で記録してください")


def _validate_hex(value: Any, pattern: re.Pattern[str], field: str, code: str) -> None:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        _fail(code, f"{field}が規定桁のlowercase hexではありません", "hash/CRCを規定桁のlowercase hexで記録してください")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_nonstandard_number(value: str) -> None:
    raise ValueError(f"non-standard JSON number: {value}")


def _source_error_message(code: str) -> tuple[str, str]:
    return {
        "unknown_month": ("CSV契約のsource monthがmanifestと一致しません", "source_monthを確認してCSVを読み直してください"),
        "unknown_version": ("CSV rowのformat versionがk_2.1ではありません", "対応版k_2.1のCSVのみを指定してください"),
        "header_hash_mismatch": ("CSV headerが対応schemaと一致しません", "170列headerとheader_sha256を確認してください"),
        "header_mismatch": ("CSVの先頭headerが対応schemaと一致しません", "CSVの列名と順序を確認してください"),
        "source_hash_mismatch": ("CSVのsizeまたはSHA-256がmanifestと一致しません", "manifestの観測値と入力CSVを照合してください"),
        "source_crc_mismatch": ("CSVのCRC32がmanifestと一致しません", "manifestのCRC32と入力CSVを照合してください"),
        "invalid_row_width": ("CSV rowの列数が170列headerと一致しません", "全rowの列幅をheaderに合わせてください"),
        "invalid_raw_uid": ("CSV rowのUIDが32文字英数字ではありません", "ユニークキーを32文字のASCII英数字にしてください"),
        "duplicate_raw_uid": ("CSV内でUIDが重複しています", "重複のないsource CSVを用意してください"),
        "invalid_encoding": ("CSVをCP932 strictで復号できません", "入力をCP932で確認し、置換文字を使わず再取得してください"),
        "invalid_csv": ("CSVの引用符またはrecord構造が不正です", "CSVをstrict CSVとして修復または再取得してください"),
        "empty_source": ("CSVにheaderがありません", "170列headerを含むCSVを指定してください"),
        "invalid_source_manifest": ("source integrity fieldが不正です", "bytes/hash/CRCの値を再確認してください"),
    }.get(
        code,
        ("CSV入力がsource parserの検査に失敗しました", "codeと入力内容を確認し、manifestとCSVを照合してください"),
    )


def _fail(code: str, cause: str, action: str) -> None:
    raise InputError(code, cause, action)
