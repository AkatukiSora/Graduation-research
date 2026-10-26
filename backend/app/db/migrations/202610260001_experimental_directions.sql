-- Experimental direction estimates are isolated from regulation_points and every
-- API selector. These tables accept only unapproved direction-test datasets.
CREATE TABLE experimental_direction_datasets (
    dataset_id text PRIMARY KEY,
    content_sha256 text NOT NULL UNIQUE,
    row_count integer NOT NULL,
    data_purpose text NOT NULL,
    production_approved boolean NOT NULL DEFAULT false,
    provenance jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT experimental_direction_datasets_digest_format
        CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT experimental_direction_datasets_row_count_positive
        CHECK (row_count > 0),
    CONSTRAINT experimental_direction_datasets_data_purpose_test_only
        CHECK (data_purpose = 'direction_test'),
    CONSTRAINT experimental_direction_datasets_not_production_approved
        CHECK (production_approved = false),
    CONSTRAINT experimental_direction_datasets_provenance_object
        CHECK (jsonb_typeof(provenance) = 'object')
);

CREATE TABLE experimental_direction_estimates (
    dataset_id text NOT NULL REFERENCES experimental_direction_datasets (dataset_id),
    entity_id text NOT NULL,
    raw_uid varchar(32) NOT NULL,
    subset text NOT NULL,
    geom geography(Point, 4326) NOT NULL,
    direction_degrees double precision,
    direction_status text NOT NULL,
    source text NOT NULL,
    confidence double precision,
    error text,
    PRIMARY KEY (dataset_id, entity_id),
    UNIQUE (dataset_id, raw_uid),
    CONSTRAINT experimental_direction_estimates_subset_values
        CHECK (subset IN ('teacher', 'target')),
    CONSTRAINT experimental_direction_estimates_direction_range
        CHECK (direction_degrees IS NULL OR (direction_degrees >= 0 AND direction_degrees < 360)),
    CONSTRAINT experimental_direction_estimates_direction_consistency
        CHECK ((direction_status = 'unknown' AND direction_degrees IS NULL)
            OR (direction_status = 'inferred' AND direction_degrees IS NOT NULL)),
    CONSTRAINT experimental_direction_estimates_source_only
        CHECK (source = 'external_local_stop_approach'),
    CONSTRAINT experimental_direction_estimates_confidence_null
        CHECK (confidence IS NULL),
    CONSTRAINT experimental_direction_estimates_error_consistency
        CHECK ((direction_status = 'unknown' AND error IS NOT NULL AND length(btrim(error)) > 0)
            OR (direction_status = 'inferred' AND error IS NULL))
);

-- The baseline grants SELECT on all public tables to gr9app by default. Keep
-- experimental rows inaccessible to the production API reader.
REVOKE ALL PRIVILEGES ON experimental_direction_datasets, experimental_direction_estimates FROM gr9app;

COMMENT ON TABLE experimental_direction_datasets IS
    'Isolated, non-production direction-test datasets; not read by the application API.';
COMMENT ON TABLE experimental_direction_estimates IS
    'External vehicle-approach estimates for controlled experiments only; never official directions.';
