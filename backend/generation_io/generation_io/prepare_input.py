"""Create a schema-1 manifest of observed external input bytes.

This module does not acquire data, decide rights, or modify source inputs.
The SHA-256 and CRC values it records are observations for later integrity
checks; they are not evidence of authenticity, provenance, or permission.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import zlib
from pathlib import Path
from typing import Any

from .input_manifest import (
    InputError,
    _open_regular_readonly,
    _resolve_regular_file,
    _source_error_message,
    _validate_manifest,
    _validate_relative_path,
)
from .region import RegionError, load_boundary
from .source import (
    HEADER_SHA256,
    SOURCE_VERSION,
    SourceError,
    _iter_raw_records,
)


_INTEGRITY_NOTE = (
    "bytes/hash/CRC32は処理時点の観測値であり、真正性・出所・利用権の証明ではありません"
)
_PROVENANCE_FIELDS = (
    "dataset",
    "publisher",
    "edition",
    "retrieved_at",
    "terms_reference",
    "rights_status",
)


def prepare_input_manifest(
    data_dir: str | os.PathLike[str],
    source: str,
    boundary: str,
    source_month: str,
    region_id: str,
    manifest_out: str | os.PathLike[str],
    *,
    dataset: str = "unknown",
    publisher: str = "unknown",
    edition: str = "unknown",
    retrieved_at: str = "unknown",
    terms_reference: str = "unknown",
    rights_status: str = "unknown",
    synthetic: bool = False,
) -> dict[str, Any]:
    """Validate inputs read-only and exclusively create an observed manifest."""
    root = _resolve_data_root(data_dir)
    if not root.is_dir():
        _fail(
            "INPUT_DATA_DIR_INVALID",
            "指定data-dirがディレクトリではありません",
            "CSVと境界を置いたディレクトリを指定してください",
        )

    if not isinstance(source_month, str) or not _valid_month(source_month):
        _fail(
            "INVALID_SOURCE_MONTH",
            "source-monthが有効なYYYYMMではありません",
            "年4桁と01〜12の月を指定してください",
        )
    if not isinstance(region_id, str) or not region_id.strip():
        _fail(
            "BOUNDARY_REGION_ID_INVALID",
            "region-idが空または文字列ではありません",
            "境界と対応するregion-idを指定してください",
        )
    if not isinstance(synthetic, bool):
        _fail(
            "INPUT_MANIFEST_INVALID",
            "synthetic指定がbooleanではありません",
            "syntheticはtrueまたはfalseで指定してください",
        )

    source_relative, source_path = _input_file(root, source, "source", ".csv")
    boundary_relative, boundary_path = _input_file(
        root, boundary, "boundary", ".geojson"
    )
    if source_path == boundary_path:
        _fail(
            "INPUT_PATH_COLLISION",
            "source CSVとboundary GeoJSONが同じファイルです",
            "別々のdata-dir相対pathを指定してください",
        )

    source_bytes, source_sha256, source_crc32 = _observe_file(source_path, crc=True)
    boundary_bytes, boundary_sha256, _ = _observe_file(boundary_path, crc=False)

    source_contract = {
        "source_month": source_month,
        "version": SOURCE_VERSION,
        "header_sha256": HEADER_SHA256,
        "csv_sha256": source_sha256,
        "csv_crc32": source_crc32,
        "csv_bytes": source_bytes,
    }
    try:
        with _open_regular_readonly(source_path) as stream:
            for _ in _iter_raw_records(
                stream,
                source_contract,
                expected_month=source_month,
                include_raw=False,
            ):
                pass
    except SourceError as exc:
        cause, action = _source_error_message(exc.code)
        raise InputError(exc.code, cause, action) from exc
    except OSError as exc:
        raise InputError(
            "SOURCE_READ_ERROR",
            "CSVをread-onlyで検査できません",
            "CSVの存在・権限・data-dirを確認してください",
        ) from exc

    try:
        # The region loader is the authority for GeoJSON geometry and CRS.
        load_boundary(boundary_path)
    except RegionError as exc:
        raise InputError(
            exc.code,
            "境界GeoJSONのgeometryまたはCRSが対応形式ではありません",
            "region.load_boundaryが受理するEPSG:4326/CRS84のGeoJSONを指定してください",
        ) from exc

    source_after = _observe_file(source_path, crc=True)
    boundary_after = _observe_file(boundary_path, crc=False)
    if source_after != (source_bytes, source_sha256, source_crc32):
        _fail(
            "SOURCE_CHANGED_DURING_PREPARATION",
            "CSVの観測値が検査中に変化しました",
            "入力を変更せず、安定したCSVで再実行してください",
        )
    if boundary_after != (boundary_bytes, boundary_sha256, None):
        _fail(
            "BOUNDARY_CHANGED_DURING_PREPARATION",
            "境界GeoJSONの観測値が検査中に変化しました",
            "入力を変更せず、安定した境界fileで再実行してください",
        )

    provenance = {
        "dataset": dataset,
        "publisher": publisher,
        "edition": edition,
        "retrieved_at": retrieved_at,
        "terms_reference": terms_reference,
        "rights_status": rights_status,
    }
    if any(not isinstance(value, str) for value in provenance.values()):
        _fail(
            "INPUT_MANIFEST_INVALID",
            "provenanceの値が文字列ではありません",
            "provenance各fieldを文字列で指定してください",
        )

    manifest: dict[str, Any] = {
        "schema_version": 1,
        "source": {
            "path": source_relative.as_posix(),
            "bytes": source_bytes,
            "sha256": source_sha256,
            "crc32": source_crc32,
            "source_month": source_month,
            "format_version": SOURCE_VERSION,
            "encoding": "cp932",
            "header_sha256": HEADER_SHA256,
        },
        "boundary": {
            "path": boundary_relative.as_posix(),
            "bytes": boundary_bytes,
            "sha256": boundary_sha256,
            # GeoJSON's standard default CRS and the loader's accepted named
            # CRS aliases are WGS84 longitude/latitude.
            "crs": "EPSG:4326",
            "region_id": region_id,
        },
        "provenance": provenance,
        "synthetic": synthetic,
        "production_approved": False,
    }
    _validate_manifest(manifest)
    output_path = _manifest_output_path(manifest_out)
    resolved_output = output_path.resolve(strict=False)
    if resolved_output in {source_path, boundary_path}:
        _fail(
            "MANIFEST_INPUT_COLLISION",
            "manifest-outが入力CSVまたは境界ファイルと同じpathです",
            "入力を変更しない別のmanifest-out pathを指定してください",
        )
    if output_path.is_symlink():
        _fail(
            "MANIFEST_OUTPUT_SYMLINK",
            "manifest-outの既存pathがsymlinkです",
            "symlinkを使わず新しいmanifest pathを指定してください",
        )

    encoded = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode(
        "utf-8"
    )
    _create_exclusive(output_path, encoded)
    return {
        "schema_version": 1,
        "source": {
            "path": source_relative.as_posix(),
            "bytes": source_bytes,
            "sha256": source_sha256,
            "crc32": source_crc32,
        },
        "boundary": {
            "path": boundary_relative.as_posix(),
            "bytes": boundary_bytes,
            "sha256": boundary_sha256,
            "crs": "EPSG:4326",
        },
        "provenance": provenance,
        "synthetic": synthetic,
        "production_approved": False,
        "integrity_note": _INTEGRITY_NOTE,
        "manifest": str(output_path),
    }


def _input_file(root: Path, value: str, field: str, suffix: str) -> tuple[Path, Path]:
    relative = _validate_relative_path(value, field)
    if relative.suffix.lower() != suffix:
        _fail(
            "INPUT_FORMAT_UNSUPPORTED",
            f"{field}の拡張子が{suffix}ではありません",
            f"対応する{suffix}ファイルを指定してください",
        )
    path = _resolve_regular_file(
        root,
        relative,
        missing_code=("SOURCE_CSV_NOT_FOUND" if field == "source" else "BOUNDARY_NOT_FOUND"),
        display_name=("source CSV" if field == "source" else "boundary GeoJSON"),
    )
    return relative, path


def _resolve_data_root(value: str | os.PathLike[str]) -> Path:
    try:
        raw = os.fspath(value)
        if (
            not isinstance(raw, str)
            or not raw
            or "\x00" in raw
            or "\\" in raw
            or ".." in Path(raw).parts
        ):
            _fail(
                "INPUT_PATH_INVALID",
                "data-dirが空、不正、またはtraversalを含みます",
                "存在するdata-dirを指定し、..やbackslashを除いてください",
            )
        absolute = Path(os.path.abspath(raw))
        current = Path(absolute.anchor)
        for component in absolute.parts[1:]:
            current = current / component
            mode = current.lstat().st_mode
            if stat.S_ISLNK(mode):
                _fail(
                    "INPUT_PATH_SYMLINK",
                    "data-dir pathにsymlinkが含まれています",
                    "symlinkを使わず実体のdirectoryを指定してください",
                )
            if not stat.S_ISDIR(mode):
                _fail(
                    "INPUT_DATA_DIR_INVALID",
                    "data-dir path componentがdirectoryではありません",
                    "存在する入力directoryを指定してください",
                )
        return absolute.resolve(strict=True)
    except InputError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise InputError(
            "INPUT_DATA_DIR_INVALID",
            "指定data-dirを読み取れるdirectoryとして解決できません",
            "存在するCSVと境界の入力directoryを指定してください",
        ) from exc


def _observe_file(path: Path, *, crc: bool) -> tuple[int, str, str | None]:
    size = 0
    digest = hashlib.sha256()
    checksum = 0
    try:
        with _open_regular_readonly(path) as stream:
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                digest.update(chunk)
                if crc:
                    checksum = zlib.crc32(chunk, checksum)
    except InputError:
        raise
    except OSError as exc:
        raise InputError(
            "INPUT_FILE_READ_ERROR",
            "入力fileをread-onlyで観測できません",
            "fileの存在・権限・data-dirを確認してください",
        ) from exc
    crc32 = f"{checksum & 0xffffffff:08x}" if crc else None
    return size, digest.hexdigest(), crc32


def _manifest_output_path(value: str | os.PathLike[str]) -> Path:
    try:
        raw = os.fspath(value)
        if not isinstance(raw, str) or not raw or "\x00" in raw:
            raise ValueError("invalid path")
        candidate = Path(os.path.abspath(raw))
        parent = candidate.parent
        current = Path(parent.anchor)
        for component in parent.parts[1:]:
            current = current / component
            mode = current.lstat().st_mode
            if stat.S_ISLNK(mode):
                _fail(
                    "MANIFEST_OUTPUT_SYMLINK",
                    "manifest-outの親pathにsymlinkが含まれています",
                    "symlinkを使わず実在するdirectoryを指定してください",
                )
            if not stat.S_ISDIR(mode):
                _fail(
                    "MANIFEST_OUTPUT_PARENT_INVALID",
                    "manifest-outの親pathがdirectoryではありません",
                    "既存directory内の新しいmanifest filenameを指定してください",
                )
        if not parent.is_dir() or candidate.name in {"", ".", ".."}:
            _fail(
                "MANIFEST_OUTPUT_PARENT_INVALID",
                "manifest-outの親directoryが存在しないかfilenameが不正です",
                "親directoryを作成し、新しいmanifest filenameを指定してください",
            )
        return parent.resolve(strict=True) / candidate.name
    except InputError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise InputError(
            "MANIFEST_OUTPUT_PATH_INVALID",
            "manifest-outを安全なfile pathとして解決できません",
            "symlinkを含まない既存directory内にmanifestを指定してください",
        ) from exc


def _create_exclusive(path: Path, contents: bytes) -> None:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise InputError(
            "MANIFEST_OUTPUT_EXISTS",
            "manifest-outがすでに存在します",
            "既存manifestを上書きせず、別の新しいpathを指定してください",
        ) from exc
    except OSError as exc:
        raise InputError(
            "MANIFEST_OUTPUT_CREATE_FAILED",
            "manifest-outをexclusive createできません",
            "親directoryの権限とmanifest-out pathを確認してください",
        ) from exc
    info = os.fstat(descriptor)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        try:
            current = path.lstat()
            if stat.S_ISREG(current.st_mode) and (current.st_dev, current.st_ino) == (
                info.st_dev,
                info.st_ino,
            ):
                path.unlink()
        except OSError:
            pass
        raise InputError(
            "MANIFEST_OUTPUT_WRITE_FAILED",
            "manifestを完全に書き込めませんでした",
            "出力fileの権限・空き容量を確認してから別pathで再実行してください",
        ) from exc


def _valid_month(value: str) -> bool:
    return (
        len(value) == 6
        and value.isascii()
        and value.isdigit()
        and value[:4] != "0000"
        and 1 <= int(value[4:]) <= 12
    )


def _fail(code: str, cause: str, action: str) -> None:
    raise InputError(code, cause, action)
