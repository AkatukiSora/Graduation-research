from decimal import Decimal

import pytest

from generation_io.region import boundary_from_geojson
from generation_io.source import SourceError, normalize_selected_record


def rectangle():
    return boundary_from_geojson(
        {
            "type": "Feature",
            "properties": {"synthetic": True},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]]],
            },
        }
    )


def selected(**overrides):
    record = {
        "version": "k_2.1",
        "prefecture": "23",
        "police": "101",
        "regulation": "63",
        "update": "",
        "key": "A" * 32,
        "branch": "",
        "location": "1 1",
        "entrance": "",
    }
    record.update(overrides)
    return record


def test_normalized_stop_preserves_raw_tokens_and_marks_candidate_only():
    result = normalize_selected_record(
        selected(entrance="0.5 0.5;1.5 1.5"),
        source_row=12,
        source_month="202602",
        region_id="fixture:synthetic",
        boundary=rectangle(),
        selected_hash="a" * 64,
    )

    assert result["classification"] == "normalized"
    record = result["record"]
    assert record["source_row"] == 12
    assert record["raw_uid"] == "A" * 32
    assert record["source_location_token"] == "1 1"
    assert record["entrance_points"][0]["lon_token"] == "0.5"
    assert record["entrance_points"][1]["lat_token"] == "1.5"
    assert record["production_approved"] is False
    assert record["source_active_status"] == "unknown"
    assert record["update_semantics"] == "unknown"


def test_multiple_or_missing_stop_locations_are_rejected_without_representative_points():
    expectations = [
        ("", "missing_location"),
        ("0 0;1 1", "multiple_location_points"),
        ("0 0/1 1", "multiple_location_regions"),
    ]
    for location, reason in expectations:
        result = normalize_selected_record(
            selected(location=location),
            source_row=1,
            source_month="202602",
            region_id="fixture:synthetic",
            boundary=rectangle(),
            selected_hash="b" * 64,
        )
        assert result == {
            "classification": "rejected",
            "reason": reason,
            "raw_uid": "A" * 32,
            "source_row": 1,
            "selected_field_sha256": "b" * 64,
        }


def test_invalid_stop_location_fails_closed_but_invalid_entrance_is_quarantined():
    with pytest.raises(SourceError, match="invalid_location_geometry"):
        normalize_selected_record(
            selected(location="NaN 1"),
            source_row=1,
            source_month="202602",
            region_id="fixture:synthetic",
            boundary=rectangle(),
            selected_hash="c" * 64,
        )
    result = normalize_selected_record(
        selected(entrance="NaN 0.5"),
        source_row=2,
        source_month="202602",
        region_id="fixture:synthetic",
        boundary=rectangle(),
        selected_hash="d" * 64,
    )
    assert result["classification"] == "rejected"
    assert result["reason"] == "invalid_entrance_geometry"


def test_non_stop_and_unsupported_update_are_never_candidates():
    non_stop = normalize_selected_record(
        selected(regulation="85"),
        source_row=1,
        source_month="202602",
        region_id="fixture:synthetic",
        boundary=rectangle(),
        selected_hash="e" * 64,
    )
    unsupported = normalize_selected_record(
        selected(update="2"),
        source_row=2,
        source_month="202602",
        region_id="fixture:synthetic",
        boundary=rectangle(),
        selected_hash="f" * 64,
    )
    assert non_stop == {"classification": "excluded_non_stop"}
    assert unsupported["reason"] == "unsupported_update_code"
