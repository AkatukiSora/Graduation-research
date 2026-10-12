from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path
from typing import Any


class RegionError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


Point = tuple[Fraction, Fraction]
Ring = tuple[Point, ...]
PolygonRings = tuple[Ring, ...]


@dataclass(frozen=True)
class Boundary:
    polygons: tuple[PolygonRings, ...]
    bbox: tuple[Fraction, Fraction, Fraction, Fraction]

    def covers(self, lon: Decimal, lat: Decimal) -> tuple[bool, bool]:
        point = (_fraction(lon), _fraction(lat))
        min_x, min_y, max_x, max_y = self.bbox
        if not (min_x <= point[0] <= max_x and min_y <= point[1] <= max_y):
            return False, False
        covered = False
        included_boundary = False
        for rings in self.polygons:
            inside, boundary = _covers_polygon(point, rings)
            covered |= inside
            included_boundary |= inside and boundary
        return covered, included_boundary


def load_boundary(path: str | Path) -> Boundary:
    try:
        with Path(path).open("r", encoding="utf-8", newline="") as stream:
            document = json.load(stream, parse_float=Decimal, parse_int=Decimal)
    except (OSError, UnicodeError, json.JSONDecodeError, InvalidOperation) as exc:
        raise RegionError("invalid_boundary_json") from exc
    return boundary_from_geojson(document)


def boundary_from_geojson(document: Any) -> Boundary:
    if not isinstance(document, dict) or document.get("type") != "Feature":
        raise RegionError("invalid_boundary_feature")
    _validate_crs(document.get("crs"))
    geometry = document.get("geometry")
    if not isinstance(geometry, dict):
        raise RegionError("invalid_boundary_geometry")
    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if geometry_type == "Polygon":
        raw_polygons = [coordinates]
    elif geometry_type == "MultiPolygon":
        raw_polygons = coordinates
    else:
        raise RegionError("unsupported_boundary_geometry")
    if not isinstance(raw_polygons, list) or not raw_polygons:
        raise RegionError("empty_boundary")

    polygons = tuple(_validate_polygon(raw) for raw in raw_polygons)
    for left_index, left in enumerate(polygons):
        for right in polygons[left_index + 1 :]:
            if _polygons_overlap(left, right):
                raise RegionError("polygon_overlap")

    points = [point for polygon in polygons for ring in polygon for point in ring]
    bbox = (
        min(point[0] for point in points),
        min(point[1] for point in points),
        max(point[0] for point in points),
        max(point[1] for point in points),
    )
    return Boundary(polygons=polygons, bbox=bbox)


def _validate_crs(crs: Any) -> None:
    if crs is None:
        return
    if not isinstance(crs, dict) or crs.get("type") != "name":
        raise RegionError("unknown_crs")
    properties = crs.get("properties")
    name = properties.get("name") if isinstance(properties, dict) else None
    accepted = {
        "EPSG:4326",
        "URN:OGC:DEF:CRS:EPSG::4326",
        "URN:OGC:DEF:CRS:OGC::CRS84",
        "URN:OGC:DEF:CRS:OGC:1.3:CRS84",
        "OGC:CRS84",
    }
    if not isinstance(name, str) or name.upper() not in accepted:
        raise RegionError("unknown_crs")


def _validate_polygon(raw: Any) -> PolygonRings:
    if not isinstance(raw, list) or not raw:
        raise RegionError("invalid_polygon")
    rings = tuple(_validate_ring(ring) for ring in raw)
    exterior = rings[0]
    holes = rings[1:]
    for hole in holes:
        if _rings_intersect(exterior, hole):
            raise RegionError("ring_intersection")
        if _point_in_ring(hole[0], exterior) != 1:
            raise RegionError("hole_outside_exterior")
    for index, left in enumerate(holes):
        for right in holes[index + 1 :]:
            if _rings_intersect(left, right) or _point_in_ring(left[0], right) != 0 or _point_in_ring(right[0], left) != 0:
                raise RegionError("ring_intersection")
    return rings


def _validate_ring(raw: Any) -> Ring:
    if not isinstance(raw, list) or len(raw) < 4:
        raise RegionError("invalid_ring")
    points: list[Point] = []
    for coordinate in raw:
        if not isinstance(coordinate, list) or len(coordinate) != 2:
            raise RegionError("invalid_coordinate")
        lon = _coordinate(coordinate[0], -180, 180)
        lat = _coordinate(coordinate[1], -90, 90)
        points.append((_fraction(lon), _fraction(lat)))
    ring = tuple(points)
    if ring[0] != ring[-1]:
        raise RegionError("unclosed_ring")
    if len(set(ring[:-1])) < 3:
        raise RegionError("zero_area_ring")
    edge_count = len(ring) - 1
    for left_index in range(edge_count):
        a, b = ring[left_index], ring[left_index + 1]
        if a == b:
            raise RegionError("zero_length_segment")
        for right_index in range(left_index + 1, edge_count):
            if right_index == left_index + 1 or (left_index == 0 and right_index == edge_count - 1):
                continue
            if _segments_intersect(a, b, ring[right_index], ring[right_index + 1]):
                raise RegionError("self_intersection")
    if _signed_double_area(ring) == 0:
        raise RegionError("zero_area_ring")
    return ring


def _coordinate(value: Any, lower: int, upper: int) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (Decimal, int, float)):
        raise RegionError("invalid_coordinate")
    try:
        token = value if isinstance(value, Decimal) else Decimal(str(value))
    except InvalidOperation as exc:
        raise RegionError("invalid_coordinate") from exc
    if not token.is_finite() or token < lower or token > upper:
        raise RegionError("coordinate_out_of_range")
    return token


def _fraction(value: Decimal) -> Fraction:
    return Fraction(value)


def _signed_double_area(ring: Ring) -> Fraction:
    terms = (
        (
            ring[index][0] * ring[index + 1][1]
            - ring[index + 1][0] * ring[index][1]
        )
        for index in range(len(ring) - 1)
    )
    return sum(terms, start=Fraction(0))


def _orientation(a: Point, b: Point, c: Point) -> Fraction:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _on_segment(point: Point, a: Point, b: Point) -> bool:
    return (
        _orientation(a, b, point) == 0
        and min(a[0], b[0]) <= point[0] <= max(a[0], b[0])
        and min(a[1], b[1]) <= point[1] <= max(a[1], b[1])
    )


def _segments_intersect(a: Point, b: Point, c: Point, d: Point) -> bool:
    o1, o2 = _orientation(a, b, c), _orientation(a, b, d)
    o3, o4 = _orientation(c, d, a), _orientation(c, d, b)
    if o1 == 0 and _on_segment(c, a, b):
        return True
    if o2 == 0 and _on_segment(d, a, b):
        return True
    if o3 == 0 and _on_segment(a, c, d):
        return True
    if o4 == 0 and _on_segment(b, c, d):
        return True
    return (o1 > 0) != (o2 > 0) and (o3 > 0) != (o4 > 0)


def _rings_intersect(left: Ring, right: Ring) -> bool:
    return any(
        _segments_intersect(a, b, c, d)
        for a, b in zip(left, left[1:])
        for c, d in zip(right, right[1:])
    )


def _point_in_ring(point: Point, ring: Ring) -> int:
    """Return 1 inside, 0 outside, and -1 on the ring."""
    inside = False
    x, y = point
    for a, b in zip(ring, ring[1:]):
        if _on_segment(point, a, b):
            return -1
        if (a[1] > y) != (b[1] > y):
            crossing_x = a[0] + (y - a[1]) * (b[0] - a[0]) / (b[1] - a[1])
            if crossing_x > x:
                inside = not inside
    return 1 if inside else 0


def _covers_polygon(point: Point, rings: PolygonRings) -> tuple[bool, bool]:
    exterior = _point_in_ring(point, rings[0])
    if exterior == 0:
        return False, False
    boundary = exterior == -1
    for hole in rings[1:]:
        location = _point_in_ring(point, hole)
        if location == 1:
            return False, False
        boundary |= location == -1
    return True, boundary


def _polygons_overlap(left: PolygonRings, right: PolygonRings) -> bool:
    if any(_rings_intersect(a, b) for a in left for b in right):
        return True
    left_outer = left[0][0]
    right_outer = right[0][0]
    left_in_right, _ = _covers_polygon(left_outer, right)
    right_in_left, _ = _covers_polygon(right_outer, left)
    return left_in_right or right_in_left
