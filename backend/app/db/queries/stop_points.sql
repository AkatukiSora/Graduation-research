-- name: FindRegulationPointByRawUID :one
-- Looks one point up by its source identifier inside a single dataset version.
SELECT
    p.raw_uid,
    ST_Y(p.geom::geometry)::float8 AS latitude,
    ST_X(p.geom::geometry)::float8 AS longitude,
    p.direction_degrees,
    p.direction_status,
    p.validity_status,
    p.imputation_method,
    p.imputation_reason,
    p.direction_source,
    p.direction_evidence,
    p.validity_reason,
    p.validity_evidence,
    d.dataset_id,
    d.source_month,
    d.synthetic,
    CASE
        WHEN d.provenance->>'data_purpose' = 'rule_test' THEN 'rule_test'
        WHEN d.synthetic THEN 'synthetic_fixture'
        ELSE 'observational'
    END AS data_purpose,
    p.created_at,
    p.updated_at
FROM regulation_points AS p
JOIN regulation_datasets AS d ON d.dataset_id = p.dataset_id
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
    p.direction_status,
    p.validity_status,
    p.imputation_method,
    p.imputation_reason,
    p.direction_source,
    p.direction_evidence,
    p.validity_reason,
    p.validity_evidence,
    d.dataset_id,
    d.source_month,
    d.synthetic,
    CASE
        WHEN d.provenance->>'data_purpose' = 'rule_test' THEN 'rule_test'
        WHEN d.synthetic THEN 'synthetic_fixture'
        ELSE 'observational'
    END AS data_purpose
FROM regulation_points AS p
JOIN regulation_datasets AS d ON d.dataset_id = p.dataset_id
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
