from generation_io import rule_test_seed


def test_inferred_direction_records_nearest_neighbor_proxy_evidence():
    point = {"entrance_points": []}
    nearest = ("neighbor-1", 24.0, 90.0)
    second = ("neighbor-2", 31.0, 270.0)

    result = rule_test_seed._direction_for_point(
        point,
        nearest,
        second,
        "a" * 64,
        nearest_actual=(24.0, 31.0, "neighbor-1"),
    )

    assert result["direction_status"] == "inferred"
    assert result["direction_degrees"] == 90.0
    assert result["imputation_method"] == "nearest_stop_reference"
    assert result["imputation_reason"] == "topology_proxy_unverified"
    assert result["direction_evidence"]["direction_semantics"] == "stop_to_neighbor_proxy"
    assert result["direction_evidence"]["neighbor_entity_id"] == "neighbor-1"


def test_tied_nearest_neighbor_remains_unknown_without_evidence():
    point = {"entrance_points": []}
    nearest = ("neighbor-1", 24.0, 90.0)
    second = ("neighbor-2", 24.0000005, 270.0)

    result = rule_test_seed._direction_for_point(
        point,
        nearest,
        second,
        "a" * 64,
        nearest_actual=(24.0, 24.0000005, "neighbor-1"),
    )

    assert result["direction_status"] == "unknown"
    assert result["direction_degrees"] is None
    assert result["imputation_reason"] == "ambiguous_nearest_neighbor"
    assert result["direction_evidence"] == {}
