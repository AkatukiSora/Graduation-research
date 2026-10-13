-- Regulation point datasets keep source version, uncertainty and candidate
-- relationships separate from the legacy `stop_signs` table. Candidate bearings
-- describe the direction from one point to a neighbouring point only; they are
-- never an official sign direction.

CREATE TABLE IF NOT EXISTS regulation_datasets (
    dataset_id text PRIMARY KEY,
    content_sha256 text NOT NULL UNIQUE,
    source_month text NOT NULL,
    region_id text NOT NULL,
    synthetic boolean NOT NULL,
    production_approved boolean NOT NULL DEFAULT false,
    provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT regulation_datasets_digest_format CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT regulation_datasets_source_month_format CHECK (source_month ~ '^[0-9]{4}(0[1-9]|1[0-2])$'),
    CONSTRAINT regulation_datasets_synthetic_not_approved CHECK (NOT (synthetic AND production_approved))
);

CREATE TABLE IF NOT EXISTS regulation_points (
    dataset_id text NOT NULL REFERENCES regulation_datasets (dataset_id),
    entity_id text NOT NULL,
    raw_uid varchar(32) NOT NULL,
    geom geography(Point, 4326) NOT NULL,
    direction_degrees double precision,
    direction_status text NOT NULL DEFAULT 'unknown',
    validity_status text NOT NULL DEFAULT 'unknown',
    normalization_status text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (dataset_id, entity_id),
    UNIQUE (dataset_id, raw_uid),
    CONSTRAINT regulation_points_direction_status_values CHECK (direction_status IN ('unknown', 'inferred', 'confirmed')),
    CONSTRAINT regulation_points_validity_status_values CHECK (validity_status IN ('unknown', 'valid', 'invalid')),
    CONSTRAINT regulation_points_direction_range CHECK (direction_degrees IS NULL OR (direction_degrees >= 0 AND direction_degrees < 360)),
    CONSTRAINT regulation_points_direction_consistency CHECK (
        (direction_status <> 'unknown' OR direction_degrees IS NULL)
        AND (direction_status NOT IN ('inferred', 'confirmed') OR direction_degrees IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_regulation_points_geom ON regulation_points USING gist (geom);

CREATE TABLE IF NOT EXISTS regulation_candidates (
    dataset_id text NOT NULL,
    source_entity_id text NOT NULL,
    neighbor_entity_id text NOT NULL,
    rank integer NOT NULL,
    distance_m double precision NOT NULL,
    candidate_bearing double precision NOT NULL,
    features jsonb NOT NULL DEFAULT '{}'::jsonb,
    bearing_semantics text NOT NULL DEFAULT 'candidate_point_neighbor_only',
    production_approved boolean NOT NULL DEFAULT false,
    PRIMARY KEY (dataset_id, source_entity_id, rank),
    CONSTRAINT regulation_candidates_neighbor_unique UNIQUE (dataset_id, source_entity_id, neighbor_entity_id),
    CONSTRAINT regulation_candidates_source_fkey FOREIGN KEY (dataset_id, source_entity_id)
        REFERENCES regulation_points (dataset_id, entity_id),
    CONSTRAINT regulation_candidates_neighbor_fkey FOREIGN KEY (dataset_id, neighbor_entity_id)
        REFERENCES regulation_points (dataset_id, entity_id),
    CONSTRAINT regulation_candidates_not_self CHECK (source_entity_id <> neighbor_entity_id),
    CONSTRAINT regulation_candidates_rank_positive CHECK (rank >= 1),
    CONSTRAINT regulation_candidates_distance_nonnegative CHECK (distance_m >= 0 AND distance_m < 'Infinity'::double precision),
    CONSTRAINT regulation_candidates_bearing_range CHECK (candidate_bearing >= 0 AND candidate_bearing < 360),
    CONSTRAINT regulation_candidates_bearing_semantics CHECK (bearing_semantics = 'candidate_point_neighbor_only'),
    CONSTRAINT regulation_candidates_not_approved CHECK (NOT production_approved)
);
