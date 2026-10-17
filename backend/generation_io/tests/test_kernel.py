import numpy as np
import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

from generation_io.kernel import FEATURES, RADII, build_candidate_features


EXPECTED_RADII = [12, 15, 18, 20, 25, 30, 40, 60, 100]
EXPECTED_FEATURES = [
    "candidate_rank",
    "candidate_distance_m",
    "candidate_bearing",
    "p_d1",
    "p_d2",
    "p_d3",
    "p_gap12",
    "p_ratio12",
    "q_d1",
    "q_d2",
    "reciprocal_rank",
    "mutual_nearest",
    "min_axis_angle_other",
    "min_directed_angle_other",
    "third_midpoint_distance_m",
    "bearing_mod_45",
    "bearing_mod_90",
] + [f"p_n{radius}" for radius in EXPECTED_RADII] + [
    f"q_n{radius}" for radius in EXPECTED_RADII
]
EXPECTED_COLUMNS = [
    "point_index",
    "neighbor_index",
    "record_id",
    "uniqueKey",
    "lon",
    "lat",
] + EXPECTED_FEATURES[:17] + [
    column
    for radius in EXPECTED_RADII
    for column in (f"p_n{radius}", f"q_n{radius}")
]


def make_points(x_meters):
    return pd.DataFrame(
        {
            "lon": np.asarray(x_meters, dtype=float) / 111_320.0,
            "lat": np.zeros(len(x_meters), dtype=float),
            "jartic_record_id": [f"record-{index}" for index in range(len(x_meters))],
            "ユニークキー": [f"key-{index}" for index in range(len(x_meters))],
        }
    )


def test_exports_the_p05_feature_schema_in_exact_order():
    assert RADII == EXPECTED_RADII
    assert FEATURES == EXPECTED_FEATURES

    result = build_candidate_features(make_points([0.0, 30.0]))

    assert list(result.columns) == EXPECTED_COLUMNS
    int_columns = {
        "point_index",
        "neighbor_index",
        "candidate_rank",
        "reciprocal_rank",
        "mutual_nearest",
        *[f"p_n{radius}" for radius in EXPECTED_RADII],
        *[f"q_n{radius}" for radius in EXPECTED_RADII],
    }
    object_columns = {"record_id", "uniqueKey"}
    for column in EXPECTED_COLUMNS:
        expected_dtype = (
            "int64"
            if column in int_columns
            else "object"
            if column in object_columns
            else "float64"
        )
        assert str(result[column].dtype) == expected_dtype, column


def test_two_points_have_independent_expected_feature_values_and_order():
    result = build_candidate_features(make_points([0.0, 30.0]))

    expected_rows = []
    for point_index, neighbor_index, bearing_value in [(0, 1, 90.0), (1, 0, 270.0)]:
        row = {
            "point_index": point_index,
            "neighbor_index": neighbor_index,
            "record_id": f"record-{point_index}",
            "uniqueKey": f"key-{point_index}",
            "lon": [0.0, 30.0 / 111_320.0][point_index],
            "lat": 0.0,
            "candidate_rank": 1,
            "candidate_distance_m": 29.999999999999996,
            "candidate_bearing": bearing_value,
            "p_d1": 29.999999999999996,
            "p_d2": 999.0,
            "p_d3": 999.0,
            "p_gap12": 969.0,
            "p_ratio12": 33.300000000000004,
            "q_d1": 29.999999999999996,
            "q_d2": 999.0,
            "reciprocal_rank": 1,
            "mutual_nearest": 1,
            "min_axis_angle_other": 999.0,
            "min_directed_angle_other": 999.0,
            "third_midpoint_distance_m": 999.0,
            "bearing_mod_45": 0.0,
            "bearing_mod_90": 0.0,
        }
        for radius in EXPECTED_RADII:
            count = int(radius >= 30)
            row[f"p_n{radius}"] = count
            row[f"q_n{radius}"] = count
        expected_rows.append(row)

    expected = pd.DataFrame(expected_rows, columns=EXPECTED_COLUMNS)
    assert_frame_equal(result, expected, check_exact=True)


def test_zero_points_preserves_the_p05_kernel_error():
    empty = pd.DataFrame(
        columns=["lon", "lat", "jartic_record_id", "ユニークキー"]
    )

    with pytest.warns(RuntimeWarning, match="Mean of empty slice"):
        with pytest.raises(ValueError):
            build_candidate_features(empty)


def test_one_point_produces_an_empty_frame_without_invented_columns():
    result = build_candidate_features(make_points([0.0]))

    assert_frame_equal(result, pd.DataFrame())


def test_equal_distance_ties_keep_the_cKDTree_candidate_order():
    result = build_candidate_features(make_points([0.0, -10.0, 10.0]))
    center_candidates = result.loc[result["point_index"] == 0]

    assert center_candidates["neighbor_index"].tolist() == [2, 1]
    assert center_candidates["candidate_rank"].tolist() == [1, 2]
    assert center_candidates["candidate_distance_m"].tolist() == [10.0, 10.0]
    assert center_candidates["p_d1"].tolist() == [10.0, 10.0]
    assert center_candidates["p_d2"].tolist() == [10.0, 10.0]


def test_duplicate_coordinates_exclude_self_but_keep_the_other_duplicate():
    result = build_candidate_features(make_points([0.0, 0.0, 10.0]), candidate_radius=0.0)
    duplicate_rows = result.loc[result["candidate_distance_m"] == 0.0]

    assert duplicate_rows[["point_index", "neighbor_index"]].values.tolist() == [[0, 1], [1, 0]]
    assert duplicate_rows["candidate_rank"].tolist() == [1, 1]
    assert duplicate_rows["mutual_nearest"].tolist() == [1, 1]
    assert duplicate_rows["p_d1"].tolist() == [0.0, 0.0]


def test_candidate_radius_and_neighbor_count_radius_include_the_boundary():
    result = build_candidate_features(make_points([0.0, 20.0]), candidate_radius=20.0)

    assert result[["point_index", "neighbor_index"]].values.tolist() == [[0, 1], [1, 0]]
    assert result["candidate_distance_m"].tolist() == [20.0, 20.0]
    assert result["p_n20"].tolist() == [1, 1]
    assert result["q_n20"].tolist() == [1, 1]


def test_candidate_just_outside_radius_is_excluded():
    result = build_candidate_features(
        make_points([0.0, 20.0001]), candidate_radius=20.0
    )

    assert_frame_equal(result, pd.DataFrame())


def test_input_dataframe_is_not_modified():
    points = make_points([0.0, -10.0, 10.0])
    original = points.copy(deep=True)

    build_candidate_features(points)

    assert_frame_equal(points, original, check_exact=True)
