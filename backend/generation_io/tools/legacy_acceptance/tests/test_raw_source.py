import csv
import hashlib
import io
import json
import zlib
from pathlib import Path

import pytest

from generation_io.source import SourceError, iter_raw_records


G01 = Path("/opt/data/workspaces/graduation-research-evidence/unified-work/G01/fixtures")


def raw_csv(rows, *, version="k_2.1", month="202602", bad_encoding=False):
    header = json.loads((G01 / "fixture-manifest.json").read_text(encoding="utf-8"))["header"]
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(header)
    selected_fields = {
        "version": "拡張版標準フォーマット種別・バージョン",
        "prefecture": "都道府県コード",
        "police": "警察署コード",
        "regulation": "共通規制種別コード",
        "update": "データ更新区分コード",
        "key": "ユニークキー",
        "branch": "枝番号",
        "location": "規制場所の経度緯度",
        "entrance": "進入方向(座標)",
    }
    indexes = {field: header.index(name) for field, name in selected_fields.items()}
    for selected in rows:
        raw = [""] * len(header)
        for field, value in selected.items():
            raw[indexes[field]] = value
        writer.writerow(raw)
    data = output.getvalue().encode("cp932")
    if bad_encoding:
        data = data[:-2] + b"\x81\r\n"
    contract = {
        "csv_sha256": hashlib.sha256(data).hexdigest(),
        "csv_bytes": len(data),
        "csv_crc32": f"{zlib.crc32(data) & 0xffffffff:08x}",
        "source_month": month,
        "version": version,
        "header": header,
    }
    return io.BytesIO(data), contract


def raw_row(**overrides):
    row = {
        "version": "k_2.1",
        "prefecture": "23",
        "police": "101",
        "regulation": "63",
        "update": "",
        "key": "Z" * 32,
        "branch": "",
        "location": "1 2",
        "entrance": "",
    }
    row.update(overrides)
    return row


def test_raw_parser_streams_cp932_csv_and_preserves_literal_selected_values():
    stream, contract = raw_csv([raw_row(key="0" * 32, location="-0.0 35.2")])

    rows = list(iter_raw_records(stream, contract))

    assert len(rows) == 1
    assert rows[0]["source_row"] == 2
    assert rows[0]["selected"]["key"] == "0" * 32
    assert rows[0]["selected"]["location"] == "-0.0 35.2"


def test_raw_parser_rejects_unknown_contract_and_duplicate_keys():
    stream, contract = raw_csv([raw_row()])
    bad_month = dict(contract, source_month="202605")
    with pytest.raises(SourceError, match="unknown_month"):
        list(iter_raw_records(stream, bad_month))

    stream, contract = raw_csv([raw_row(), raw_row()])
    with pytest.raises(SourceError, match="duplicate_raw_uid"):
        list(iter_raw_records(stream, contract))


def test_raw_parser_fails_closed_on_bad_encoding_version_and_row_width():
    stream, contract = raw_csv([raw_row()], bad_encoding=True)
    with pytest.raises(SourceError, match="invalid_encoding"):
        list(iter_raw_records(stream, contract))

    stream, contract = raw_csv([raw_row(version="k_9.9")])
    with pytest.raises(SourceError, match="unknown_version"):
        list(iter_raw_records(stream, contract))

    header = json.loads((G01 / "fixture-manifest.json").read_text(encoding="utf-8"))["header"]
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(header)
    writer.writerow(["k_2.1"])
    data = output.getvalue().encode("cp932")
    contract = {
        "csv_sha256": hashlib.sha256(data).hexdigest(),
        "csv_bytes": len(data),
        "csv_crc32": f"{zlib.crc32(data) & 0xffffffff:08x}",
        "source_month": "202602",
        "version": "k_2.1",
        "header": header,
    }
    with pytest.raises(SourceError, match="invalid_row_width"):
        list(iter_raw_records(io.BytesIO(data), contract))


def test_raw_parser_rejects_invalid_key_unknown_header_and_crc_mismatch():
    stream, contract = raw_csv([raw_row(key="not-a-32-character-key")])
    with pytest.raises(SourceError, match="invalid_raw_uid"):
        list(iter_raw_records(stream, contract))

    stream, contract = raw_csv([raw_row()])
    contract["header"][0] = "unrecognized header"
    with pytest.raises(SourceError, match="header_hash_mismatch"):
        list(iter_raw_records(stream, contract))

    stream, contract = raw_csv([raw_row()])
    contract["csv_crc32"] = "00000000"
    with pytest.raises(SourceError, match="source_crc_mismatch"):
        list(iter_raw_records(stream, contract))
