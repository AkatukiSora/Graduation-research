from decimal import Decimal

import pytest

from generation_io.region import RegionError, boundary_from_geojson


def feature(geometry, *, crs=None):
    result = {
        "type": "Feature",
        "properties": {"synthetic": True},
        "geometry": geometry,
    }
    if crs is not None:
        result["crs"] = crs
    return result


def polygon(outer, holes=()):
    return {"type": "Polygon", "coordinates": [outer, *holes]}


def test_covers_outer_edges_and_vertices_but_excludes_hole_interior():
    boundary = boundary_from_geojson(
        feature(
            polygon(
                [
                    [0, 0],
                    [4, 0],
                    [4, 4],
                    [0, 4],
                    [0, 0],
                ],
                [
                    [
                        [1, 1],
                        [3, 1],
                        [3, 3],
                        [1, 3],
                        [1, 1],
                    ]
                ],
            )
        )
    )

    assert boundary.covers(Decimal("0"), Decimal("2")) == (True, True)
    assert boundary.covers(Decimal("0"), Decimal("0")) == (True, True)
    assert boundary.covers(Decimal("2"), Decimal("1")) == (True, True)
    assert boundary.covers(Decimal("2"), Decimal("2")) == (False, False)
    assert boundary.covers(Decimal("0.5"), Decimal("0.5")) == (True, False)


def test_multipolygon_uses_union_and_keeps_one_coverage_result():
    boundary = boundary_from_geojson(
        feature(
            {
                "type": "MultiPolygon",
                "coordinates": [
                    [
                        [
                            [0, 0],
                            [1, 0],
                            [1, 1],
                            [0, 1],
                            [0, 0],
                        ]
                    ],
                    [
                        [
                            [3, 0],
                            [4, 0],
                            [4, 1],
                            [3, 1],
                            [3, 0],
                        ]
                    ],
                ],
            }
        )
    )

    assert boundary.covers(Decimal("0.5"), Decimal("0.5")) == (True, False)
    assert boundary.covers(Decimal("3"), Decimal("0.5")) == (True, True)
    assert boundary.covers(Decimal("2"), Decimal("0.5")) == (False, False)


def test_rejects_self_intersecting_ring():
    with pytest.raises(RegionError, match="self_intersection"):
        boundary_from_geojson(
            feature(
                polygon(
                    [
                        [0, 0],
                        [2, 2],
                        [0, 2],
                        [2, 0],
                        [0, 0],
                    ]
                )
            )
        )


def test_rejects_unknown_crs_and_open_ring():
    with pytest.raises(RegionError, match="unknown_crs"):
        boundary_from_geojson(
            feature(
                polygon([[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]),
                crs={"type": "name", "properties": {"name": "EPSG:3857"}},
            )
        )
    with pytest.raises(RegionError, match="unclosed_ring"):
        boundary_from_geojson(
            feature(polygon([[0, 0], [1, 0], [1, 1], [0, 1]]))
        )


def test_rejects_polygon_overlap_even_without_crossing_edges():
    with pytest.raises(RegionError, match="polygon_overlap"):
        boundary_from_geojson(
            feature(
                {
                    "type": "MultiPolygon",
                    "coordinates": [
                        [
                            [
                                [0, 0],
                                [3, 0],
                                [3, 3],
                                [0, 3],
                                [0, 0],
                            ]
                        ],
                        [
                            [
                                [1, 1],
                                [2, 1],
                                [2, 2],
                                [1, 2],
                                [1, 1],
                            ]
                        ],
                    ],
                }
            )
        )
