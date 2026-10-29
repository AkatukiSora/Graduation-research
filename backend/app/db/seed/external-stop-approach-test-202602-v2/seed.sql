-- Test-only seed for the direction-estimation results in points.csv.gz.
-- Requires the migrations. Run from this directory so the relative path resolves:
--   psql -v ON_ERROR_STOP=1 -f seed.sql
-- Fails and rolls back if the dataset already exists.
BEGIN;

CREATE TEMP TABLE seed_points (
    uid varchar(32) PRIMARY KEY,
    lat double precision NOT NULL,
    lon double precision NOT NULL,
    az double precision
) ON COMMIT DROP;

\copy seed_points FROM PROGRAM 'gzip -dc points.csv.gz' WITH (FORMAT csv)

-- content_sha256 is the SHA-256 of the source result_local.json.
INSERT INTO regulation_datasets (dataset_id, content_sha256, source_month, region_id, synthetic)
VALUES ('external-stop-approach-test-202602-v2',
        '0c19ade31372def466d7faa18568a367814be3434379916f4cb1a5cf62a6dbe7',
        '202602', 'jp:external-test', false);

INSERT INTO regulation_points (dataset_id, entity_id, raw_uid, geom, direction_degrees, direction_status, normalization_status)
SELECT 'external-stop-approach-test-202602-v2', uid, uid,
       ST_SetSRID(ST_MakePoint(lon, lat), 4326)::geography,
       az, CASE WHEN az IS NULL THEN 'unknown' ELSE 'inferred' END, 'valid_candidate_only'
FROM seed_points;

DO $$
BEGIN
    IF (SELECT count(*) FROM regulation_points WHERE dataset_id = 'external-stop-approach-test-202602-v2') <> 168652 THEN
        RAISE EXCEPTION 'unexpected point count';
    END IF;
END
$$;

COMMIT;
