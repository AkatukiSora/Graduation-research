"""Portable P05-adopted candidate feature kernel.

Source: the P05-adopted kernel in this project's
``backend/generation/generation/features.py``. The implementation and public
API are retained for portable runtime use.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
from scipy.spatial import cKDTree

RADII = [12, 15, 18, 20, 25, 30, 40, 60, 100]

FEATURES = [
    'candidate_rank','candidate_distance_m','candidate_bearing',
    'p_d1','p_d2','p_d3','p_gap12','p_ratio12','q_d1','q_d2',
    'reciprocal_rank','mutual_nearest','min_axis_angle_other',
    'min_directed_angle_other','third_midpoint_distance_m',
    'bearing_mod_45','bearing_mod_90',
] + [f'p_n{r}' for r in RADII] + [f'q_n{r}' for r in RADII]


def circular_diff(a: float, b: float) -> float:
    return abs((a - b + 180.0) % 360.0 - 180.0)


def axis_diff(a: float, b: float) -> float:
    d = circular_diff(a, b)
    return min(d, 180.0 - d)


def bearing(dx: float, dy: float) -> float:
    return (math.degrees(math.atan2(dx, dy)) + 360.0) % 360.0


def mod_distance(value: float, step: float) -> float:
    r = value % step
    return min(r, step - r)


def project(lon: np.ndarray, lat: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    lat0 = float(np.nanmedian(lat))
    x = lon * (111_320.0 * math.cos(math.radians(lat0)))
    y = lat * 111_320.0
    return x, y


def build_candidate_features(
    points: pd.DataFrame,
    max_candidates: int = 6,
    candidate_radius: float = 80.0,
) -> pd.DataFrame:
    lon = points['lon'].to_numpy(float)
    lat = points['lat'].to_numpy(float)
    x, y = project(lon, lat)
    xy = np.column_stack([x, y])
    tree = cKDTree(xy)
    n = len(points)

    # Nearest 12 gives enough room after self/duplicate handling.
    dists, inds = tree.query(xy, k=min(14, n))
    if dists.ndim == 1:
        dists = dists[:, None]
        inds = inds[:, None]

    # Neighbor counts exclude self.
    counts = {r: tree.query_ball_point(xy, r, return_length=True) - 1 for r in RADII}

    neighbor_lists: list[list[tuple[int, float]]] = []
    for i in range(n):
        pairs: list[tuple[int, float]] = []
        for d, j in zip(dists[i], inds[i]):
            j = int(j)
            if j == i or not np.isfinite(d):
                continue
            if d <= candidate_radius:
                pairs.append((j, float(d)))
            if len(pairs) >= max_candidates:
                break
        neighbor_lists.append(pairs)

    rows: list[dict] = []
    for i, pairs in enumerate(neighbor_lists):
        if not pairs:
            continue
        point_row = points.iloc[i]
        record_id = str(point_row['jartic_record_id'])
        unique_key = str(point_row['ユニークキー'])
        p_ds = [d for _, d in pairs]
        p_d1 = p_ds[0]
        p_d2 = p_ds[1] if len(p_ds) > 1 else 999.0
        p_d3 = p_ds[2] if len(p_ds) > 2 else 999.0
        p_bearings = [bearing(x[j] - x[i], y[j] - y[i]) for j, _ in pairs]
        for rank, ((j, d), b) in enumerate(zip(pairs, p_bearings), start=1):
            q_pairs = neighbor_lists[j]
            q_ds = [qd for _, qd in q_pairs]
            q_d1 = q_ds[0] if q_ds else 999.0
            q_d2 = q_ds[1] if len(q_ds) > 1 else 999.0
            reciprocal_rank = 7
            for rr, (qj, _) in enumerate(q_pairs, start=1):
                if qj == i:
                    reciprocal_rank = rr
                    break

            other_b = [ob for k, ob in enumerate(p_bearings) if k != rank - 1]
            min_axis = min((axis_diff(b, ob) for ob in other_b), default=999.0)
            min_directed = min((circular_diff(b, ob) for ob in other_b), default=999.0)

            midpoint = (xy[i] + xy[j]) / 2.0
            md, mi = tree.query(midpoint, k=min(8, n))
            md = np.atleast_1d(md)
            mi = np.atleast_1d(mi)
            third_mid = 999.0
            for td, tj in zip(md, mi):
                tj = int(tj)
                if tj not in (i, j):
                    third_mid = float(td)
                    break

            row = {
                'point_index': i,
                'neighbor_index': j,
                'record_id': record_id,
                'uniqueKey': unique_key,
                'lon': lon[i],
                'lat': lat[i],
                'candidate_rank': rank,
                'candidate_distance_m': d,
                'candidate_bearing': b,
                'p_d1': p_d1,
                'p_d2': p_d2,
                'p_d3': p_d3,
                'p_gap12': p_d2 - p_d1,
                'p_ratio12': p_d2 / max(p_d1, 1e-6),
                'q_d1': q_d1,
                'q_d2': q_d2,
                'reciprocal_rank': reciprocal_rank,
                'mutual_nearest': int(reciprocal_rank == 1 and rank == 1),
                'min_axis_angle_other': min_axis,
                'min_directed_angle_other': min_directed,
                'third_midpoint_distance_m': third_mid,
                'bearing_mod_45': mod_distance(b, 45.0),
                'bearing_mod_90': mod_distance(b, 90.0),
            }
            for r in RADII:
                row[f'p_n{r}'] = int(counts[r][i])
                row[f'q_n{r}'] = int(counts[r][j])
            rows.append(row)
    return pd.DataFrame(rows)
