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
    imputation_method text NOT NULL DEFAULT 'none',
    imputation_reason text NOT NULL DEFAULT 'legacy_unrecorded',
    direction_source text NOT NULL DEFAULT 'unknown',
    direction_evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
    validity_reason text NOT NULL DEFAULT 'legacy_unrecorded',
    validity_evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (dataset_id, entity_id),
    UNIQUE (dataset_id, raw_uid),
    CONSTRAINT regulation_points_direction_status_values CHECK (direction_status IN ('unknown', 'inferred', 'confirmed')),
    CONSTRAINT regulation_points_validity_status_values CHECK (validity_status IN ('unknown', 'valid', 'invalid')),
    CONSTRAINT regulation_points_direction_range CHECK (direction_degrees IS NULL OR (direction_degrees >= 0 AND direction_degrees < 360)),
    CONSTRAINT regulation_points_direction_consistency CHECK ((
        (
            direction_status = 'unknown'
            AND direction_degrees IS NULL
            AND imputation_method = 'none'
            AND direction_source = 'unknown'
            AND direction_evidence = '{}'::jsonb
            AND imputation_reason IN (
                'missing_entrance_coordinates', 'unsupported_entrance_geometry',
                'topology_only_unverified', 'source_identity_unverified', 'legacy_unrecorded',
                'no_neighbor_within_radius', 'zero_length_geometry',
                'ambiguous_nearest_neighbor', 'multiple_entrance_directions'
            )
        )
        OR (
            direction_status = 'inferred'
            AND direction_degrees IS NOT NULL
            AND imputation_method = 'source_entrance_geometry'
            AND direction_source = 'jartic_entrance'
            AND imputation_reason = 'source_geometry_derived'
            AND direction_evidence <> '{}'::jsonb
            AND jsonb_typeof(direction_evidence) = 'object'
            AND jsonb_typeof(direction_evidence->'schema_version') = 'number'
            AND jsonb_typeof(direction_evidence->'kind') = 'string'
            AND jsonb_typeof(direction_evidence->'source_csv_sha256') = 'string'
            AND jsonb_typeof(direction_evidence->'method_version') = 'string'
            AND jsonb_typeof(direction_evidence->'direction_semantics') = 'string'
            AND direction_evidence - ARRAY['schema_version', 'kind', 'source_csv_sha256', 'method_version', 'direction_semantics', 'entrance']::text[] = '{}'::jsonb
            AND direction_evidence ?& ARRAY['schema_version', 'kind', 'source_csv_sha256', 'method_version', 'direction_semantics', 'entrance']
            AND direction_evidence->'schema_version' = '2'::jsonb
            AND direction_evidence->>'kind' = 'source_geometry'
            AND direction_evidence->>'source_csv_sha256' ~ '^[0-9a-f]{64}$'
            AND direction_evidence->>'method_version' = 'entrance-to-stop-sphere-v1'
            AND direction_evidence->>'direction_semantics' = 'vehicle_approach_to_stop_estimate'
            AND jsonb_typeof(direction_evidence->'entrance') = 'object'
            AND ((direction_evidence->'entrance') - ARRAY['lon', 'lat']::text[]) = '{}'::jsonb
            AND (direction_evidence->'entrance') ?& ARRAY['lon', 'lat']
            AND jsonb_typeof(direction_evidence->'entrance'->'lon') = 'number'
            AND jsonb_typeof(direction_evidence->'entrance'->'lat') = 'number'
        )
        OR (
            direction_status = 'inferred'
            AND direction_degrees IS NOT NULL
            AND imputation_method = 'nearest_stop_reference'
            AND direction_source = 'nearest_stop'
            AND imputation_reason = 'topology_proxy_unverified'
            AND direction_evidence <> '{}'::jsonb
            AND jsonb_typeof(direction_evidence) = 'object'
            AND jsonb_typeof(direction_evidence->'schema_version') = 'number'
            AND jsonb_typeof(direction_evidence->'kind') = 'string'
            AND jsonb_typeof(direction_evidence->'source_csv_sha256') = 'string'
            AND jsonb_typeof(direction_evidence->'method_version') = 'string'
            AND jsonb_typeof(direction_evidence->'direction_semantics') = 'string'
            AND direction_evidence - ARRAY['schema_version', 'kind', 'source_csv_sha256', 'method_version', 'direction_semantics', 'neighbor_entity_id', 'candidate_rank']::text[] = '{}'::jsonb
            AND direction_evidence ?& ARRAY['schema_version', 'kind', 'source_csv_sha256', 'method_version', 'direction_semantics', 'neighbor_entity_id', 'candidate_rank']
            AND direction_evidence->'schema_version' = '2'::jsonb
            AND direction_evidence->>'kind' = 'nearest_stop_reference'
            AND direction_evidence->>'source_csv_sha256' ~ '^[0-9a-f]{64}$'
            AND direction_evidence->>'method_version' = 'nearest-stop-proxy-v1'
            AND direction_evidence->>'direction_semantics' = 'stop_to_neighbor_proxy'
            AND jsonb_typeof(direction_evidence->'neighbor_entity_id') = 'string'
            AND length(direction_evidence->>'neighbor_entity_id') > 0
            AND jsonb_typeof(direction_evidence->'candidate_rank') = 'number'
            AND direction_evidence->'candidate_rank' = '1'::jsonb
        )
        OR (
            direction_status = 'inferred'
            AND direction_degrees IS NOT NULL
            AND imputation_method = 'osm_fusion_cautious'
            AND direction_source = 'osm_fusion'
            AND imputation_reason = 'model_direction_inference'
            AND direction_evidence <> '{}'::jsonb
            AND jsonb_typeof(direction_evidence) = 'object'
            AND jsonb_typeof(direction_evidence->'schema_version') = 'number'
            AND jsonb_typeof(direction_evidence->'kind') = 'string'
            AND jsonb_typeof(direction_evidence->'source_csv_sha256') = 'string'
            AND jsonb_typeof(direction_evidence->'method_version') = 'string'
            AND jsonb_typeof(direction_evidence->'direction_semantics') = 'string'
            AND direction_evidence - ARRAY['schema_version', 'kind', 'source_csv_sha256', 'method_version', 'direction_semantics', 'artifact_sha256', 'record_ref', 'model_version', 'feature_parent_sha256']::text[] = '{}'::jsonb
            AND direction_evidence ?& ARRAY['schema_version', 'kind', 'source_csv_sha256', 'method_version', 'direction_semantics', 'artifact_sha256', 'record_ref', 'model_version', 'feature_parent_sha256']
            AND direction_evidence->'schema_version' = '2'::jsonb
            AND direction_evidence->>'kind' = 'model_inference'
            AND direction_evidence->>'source_csv_sha256' ~ '^[0-9a-f]{64}$'
            AND jsonb_typeof(direction_evidence->'method_version') = 'string'
            AND length(direction_evidence->>'method_version') > 0
            AND direction_evidence->>'direction_semantics' = 'vehicle_approach_to_stop_estimate'
            AND direction_evidence->>'artifact_sha256' ~ '^[0-9a-f]{64}$'
            AND jsonb_typeof(direction_evidence->'record_ref') = 'string'
            AND length(direction_evidence->>'record_ref') > 0
            AND jsonb_typeof(direction_evidence->'model_version') = 'string'
            AND length(direction_evidence->>'model_version') > 0
            AND direction_evidence->>'feature_parent_sha256' ~ '^[0-9a-f]{64}$'
        )
        OR (
            direction_status = 'confirmed'
            AND direction_degrees IS NOT NULL
            AND imputation_method = 'field_observation'
            AND direction_source = 'field_survey'
            AND imputation_reason = 'none'
            AND direction_evidence <> '{}'::jsonb
            AND jsonb_typeof(direction_evidence) = 'object'
            AND direction_evidence - ARRAY['schema_version', 'kind', 'entity_id', 'source_month', 'source_csv_sha256', 'artifact_sha256', 'record_ref', 'method_version', 'direction_semantics', 'observation_ref', 'observed_at', 'observation_sha256']::text[] = '{}'::jsonb
            AND direction_evidence ?& ARRAY['schema_version', 'kind', 'entity_id', 'source_month', 'source_csv_sha256', 'artifact_sha256', 'record_ref', 'method_version', 'direction_semantics', 'observation_ref', 'observed_at', 'observation_sha256']
            AND jsonb_typeof(direction_evidence->'schema_version') = 'number'
            AND direction_evidence->'schema_version' = '1'::jsonb
            AND jsonb_typeof(direction_evidence->'kind') = 'string'
            AND direction_evidence->>'kind' = 'field_observation'
            AND jsonb_typeof(direction_evidence->'entity_id') = 'string'
            AND length(direction_evidence->>'entity_id') > 0
            AND jsonb_typeof(direction_evidence->'source_month') = 'string'
            AND direction_evidence->>'source_month' ~ '^[0-9]{6}$'
            AND jsonb_typeof(direction_evidence->'source_csv_sha256') = 'string'
            AND direction_evidence->>'source_csv_sha256' ~ '^[0-9a-f]{64}$'
            AND jsonb_typeof(direction_evidence->'artifact_sha256') = 'string'
            AND direction_evidence->>'artifact_sha256' ~ '^[0-9a-f]{64}$'
            AND jsonb_typeof(direction_evidence->'record_ref') = 'string'
            AND length(direction_evidence->>'record_ref') > 0
            AND jsonb_typeof(direction_evidence->'method_version') = 'string'
            AND length(direction_evidence->>'method_version') > 0
            AND jsonb_typeof(direction_evidence->'direction_semantics') = 'string'
            AND direction_evidence->>'direction_semantics' = 'vehicle_approach_to_stop'
            AND jsonb_typeof(direction_evidence->'observation_ref') = 'string'
            AND length(direction_evidence->>'observation_ref') > 0
            AND jsonb_typeof(direction_evidence->'observed_at') = 'string'
            AND length(direction_evidence->>'observed_at') > 0
            AND jsonb_typeof(direction_evidence->'observation_sha256') = 'string'
            AND direction_evidence->>'observation_sha256' ~ '^[0-9a-f]{64}$'
        )
        OR (
            direction_status IN ('inferred', 'confirmed')
            AND direction_degrees IS NOT NULL
            AND imputation_method = 'none'
            AND imputation_reason = 'legacy_unrecorded'
            AND direction_source = 'unknown'
            AND direction_evidence = '{}'::jsonb
        )
    ) IS TRUE),
    CONSTRAINT regulation_points_imputation_method_values CHECK (
        imputation_method IN ('none', 'source_entrance_geometry', 'nearest_stop_reference', 'osm_fusion_cautious', 'field_observation')
    ),
    CONSTRAINT regulation_points_imputation_reason_values CHECK (
        imputation_reason IN (
            'none', 'missing_entrance_coordinates', 'unsupported_entrance_geometry',
            'topology_only_unverified', 'source_identity_unverified', 'legacy_unrecorded',
            'source_geometry_derived', 'model_direction_inference', 'topology_proxy_unverified',
            'no_neighbor_within_radius', 'zero_length_geometry', 'ambiguous_nearest_neighbor',
            'multiple_entrance_directions'
        )
    ),
    CONSTRAINT regulation_points_direction_source_values CHECK (
        direction_source IN ('unknown', 'jartic_entrance', 'nearest_stop', 'osm_fusion', 'field_survey')
    ),
    CONSTRAINT regulation_points_direction_evidence_object CHECK (
        jsonb_typeof(direction_evidence) = 'object'
    ),
    CONSTRAINT regulation_points_validity_reason_values CHECK (
        validity_reason IN ('source_validity_unverified', 'active_regulation_evidence', 'withdrawn_regulation_evidence', 'synthetic_fixture', 'legacy_unrecorded')
    ),
    CONSTRAINT regulation_points_validity_evidence_object CHECK (
        jsonb_typeof(validity_evidence) = 'object'
    ),
    CONSTRAINT regulation_points_validity_consistency CHECK ((
        (
            validity_status = 'unknown'
            AND validity_reason IN ('source_validity_unverified', 'synthetic_fixture', 'legacy_unrecorded')
            AND validity_evidence = '{}'::jsonb
        )
        OR (
            validity_status = 'valid'
            AND validity_reason = 'active_regulation_evidence'
            AND validity_evidence <> '{}'::jsonb
            AND jsonb_typeof(validity_evidence) = 'object'
            AND validity_evidence - ARRAY['schema_version', 'kind', 'entity_id', 'source_month', 'source_csv_sha256', 'artifact_sha256', 'record_ref', 'method_version', 'effective_at', 'validity_review_sha256']::text[] = '{}'::jsonb
            AND validity_evidence ?& ARRAY['schema_version', 'kind', 'entity_id', 'source_month', 'source_csv_sha256', 'artifact_sha256', 'record_ref', 'method_version', 'effective_at', 'validity_review_sha256']
            AND jsonb_typeof(validity_evidence->'schema_version') = 'number'
            AND validity_evidence->'schema_version' = '1'::jsonb
            AND jsonb_typeof(validity_evidence->'kind') = 'string'
            AND validity_evidence->>'kind' = 'regulation_validity'
            AND jsonb_typeof(validity_evidence->'entity_id') = 'string'
            AND length(validity_evidence->>'entity_id') > 0
            AND jsonb_typeof(validity_evidence->'source_month') = 'string'
            AND validity_evidence->>'source_month' ~ '^[0-9]{6}$'
            AND jsonb_typeof(validity_evidence->'source_csv_sha256') = 'string'
            AND validity_evidence->>'source_csv_sha256' ~ '^[0-9a-f]{64}$'
            AND jsonb_typeof(validity_evidence->'artifact_sha256') = 'string'
            AND validity_evidence->>'artifact_sha256' ~ '^[0-9a-f]{64}$'
            AND jsonb_typeof(validity_evidence->'record_ref') = 'string'
            AND length(validity_evidence->>'record_ref') > 0
            AND jsonb_typeof(validity_evidence->'method_version') = 'string'
            AND length(validity_evidence->>'method_version') > 0
            AND jsonb_typeof(validity_evidence->'effective_at') = 'string'
            AND length(validity_evidence->>'effective_at') > 0
            AND jsonb_typeof(validity_evidence->'validity_review_sha256') = 'string'
            AND validity_evidence->>'validity_review_sha256' ~ '^[0-9a-f]{64}$'
        )
        OR (
            validity_status = 'invalid'
            AND validity_reason = 'withdrawn_regulation_evidence'
            AND validity_evidence <> '{}'::jsonb
            AND jsonb_typeof(validity_evidence) = 'object'
            AND validity_evidence - ARRAY['schema_version', 'kind', 'entity_id', 'source_month', 'source_csv_sha256', 'artifact_sha256', 'record_ref', 'method_version', 'effective_at', 'validity_review_sha256']::text[] = '{}'::jsonb
            AND validity_evidence ?& ARRAY['schema_version', 'kind', 'entity_id', 'source_month', 'source_csv_sha256', 'artifact_sha256', 'record_ref', 'method_version', 'effective_at', 'validity_review_sha256']
            AND jsonb_typeof(validity_evidence->'schema_version') = 'number'
            AND validity_evidence->'schema_version' = '1'::jsonb
            AND jsonb_typeof(validity_evidence->'kind') = 'string'
            AND validity_evidence->>'kind' = 'regulation_validity'
            AND jsonb_typeof(validity_evidence->'entity_id') = 'string'
            AND length(validity_evidence->>'entity_id') > 0
            AND jsonb_typeof(validity_evidence->'source_month') = 'string'
            AND validity_evidence->>'source_month' ~ '^[0-9]{6}$'
            AND jsonb_typeof(validity_evidence->'source_csv_sha256') = 'string'
            AND validity_evidence->>'source_csv_sha256' ~ '^[0-9a-f]{64}$'
            AND jsonb_typeof(validity_evidence->'artifact_sha256') = 'string'
            AND validity_evidence->>'artifact_sha256' ~ '^[0-9a-f]{64}$'
            AND jsonb_typeof(validity_evidence->'record_ref') = 'string'
            AND length(validity_evidence->>'record_ref') > 0
            AND jsonb_typeof(validity_evidence->'method_version') = 'string'
            AND length(validity_evidence->>'method_version') > 0
            AND jsonb_typeof(validity_evidence->'effective_at') = 'string'
            AND length(validity_evidence->>'effective_at') > 0
            AND jsonb_typeof(validity_evidence->'validity_review_sha256') = 'string'
            AND validity_evidence->>'validity_review_sha256' ~ '^[0-9a-f]{64}$'
        )
        OR (validity_status IN ('valid', 'invalid') AND validity_reason = 'legacy_unrecorded' AND validity_evidence = '{}'::jsonb)
    ) IS TRUE)
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
