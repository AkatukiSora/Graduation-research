-- name: FindRegulationPointByRawUID :one
-- Looks one point up by its source identifier inside a single dataset version.
-- Only the columns the public API exposes are selected; provenance and
-- evidence columns stay in the database.
SELECT
    p.raw_uid,
    ST_Y(p.geom::geometry)::float8 AS latitude,
    ST_X(p.geom::geometry)::float8 AS longitude,
    p.direction_degrees,
    p.direction_status
FROM regulation_points AS p
WHERE p.dataset_id = sqlc.arg(dataset_id)::text
  AND p.raw_uid = sqlc.arg(raw_uid)::text;

-- name: ListRegulationPointsInRange :many
-- Geographic search inside one dataset version. The radius is in metres on the
-- geography type. Equal distances are ordered by entity_id so results are stable.
SELECT
    p.raw_uid,
    ST_Y(p.geom::geometry)::float8 AS latitude,
    ST_X(p.geom::geometry)::float8 AS longitude,
    p.direction_degrees,
    p.direction_status
FROM regulation_points AS p
WHERE p.dataset_id = sqlc.arg(dataset_id)::text
  AND ST_DWithin(
        p.geom,
        ST_SetSRID(ST_MakePoint(sqlc.arg(longitude)::float8, sqlc.arg(latitude)::float8), 4326)::geography,
        sqlc.arg(radius_meters)::float8
      )
ORDER BY
    ST_Distance(p.geom, ST_SetSRID(ST_MakePoint(sqlc.arg(longitude)::float8, sqlc.arg(latitude)::float8), 4326)::geography),
    p.entity_id
LIMIT sqlc.arg(max_rows)::int;
