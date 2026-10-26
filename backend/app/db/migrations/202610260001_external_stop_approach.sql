-- Extend the existing regulation schema only for the isolated external test dataset.
-- The original datasets and all prior direction-consistency cases remain valid.
ALTER TABLE regulation_datasets
    ADD CONSTRAINT regulation_datasets_external_direction_test_not_approved CHECK (
        dataset_id <> 'external-stop-approach-test-202602-v1' OR production_approved IS FALSE
    );

ALTER TABLE regulation_points
    DROP CONSTRAINT regulation_points_direction_consistency,
    DROP CONSTRAINT regulation_points_imputation_method_values,
    DROP CONSTRAINT regulation_points_direction_source_values;

ALTER TABLE regulation_points
    ADD CONSTRAINT regulation_points_imputation_method_values CHECK (
        imputation_method IN ('none', 'source_entrance_geometry', 'nearest_stop_reference', 'osm_fusion_cautious', 'field_observation', 'external_local_stop_approach')
    ),
    ADD CONSTRAINT regulation_points_direction_source_values CHECK (
        direction_source IN ('unknown', 'jartic_entrance', 'nearest_stop', 'osm_fusion', 'field_survey', 'external_local_stop_approach')
    ),
    ADD CONSTRAINT regulation_points_direction_consistency CHECK ((
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
            direction_status = 'inferred'
            AND direction_degrees IS NOT NULL
            AND direction_degrees > '-Infinity'::double precision
            AND direction_degrees < 'Infinity'::double precision
            AND direction_degrees >= 0
            AND direction_degrees < 360
            AND imputation_method = 'external_local_stop_approach'
            AND direction_source = 'external_local_stop_approach'
            AND imputation_reason = 'model_direction_inference'
            AND dataset_id = 'external-stop-approach-test-202602-v1'
            AND direction_evidence <> '{}'::jsonb
            AND jsonb_typeof(direction_evidence) = 'object'
            AND direction_evidence - ARRAY['schema_version', 'kind', 'source_csv_sha256', 'method_version', 'direction_semantics', 'artifact_sha256', 'record_ref', 'subset', 'confidence']::text[] = '{}'::jsonb
            AND direction_evidence ?& ARRAY['schema_version', 'kind', 'source_csv_sha256', 'method_version', 'direction_semantics', 'artifact_sha256', 'record_ref', 'subset', 'confidence']
            AND jsonb_typeof(direction_evidence->'schema_version') = 'number'
            AND direction_evidence->'schema_version' = '1'::jsonb
            AND jsonb_typeof(direction_evidence->'kind') = 'string'
            AND direction_evidence->>'kind' = 'external_local_stop_approach'
            AND jsonb_typeof(direction_evidence->'source_csv_sha256') = 'string'
            AND direction_evidence->>'source_csv_sha256' ~ '^[0-9a-f]{64}$'
            AND jsonb_typeof(direction_evidence->'method_version') = 'string'
            AND direction_evidence->>'method_version' = 'local-stop-approach-offline-v1'
            AND jsonb_typeof(direction_evidence->'direction_semantics') = 'string'
            AND direction_evidence->>'direction_semantics' = 'vehicle_approach_to_stop_estimate'
            AND jsonb_typeof(direction_evidence->'artifact_sha256') = 'string'
            AND direction_evidence->>'artifact_sha256' = '02eeb6998b5e926844448b4260d29c95f5b9a8a1b4216397789cbf1973493ebe'
            AND jsonb_typeof(direction_evidence->'record_ref') = 'string'
            AND direction_evidence->>'record_ref' = entity_id
            AND jsonb_typeof(direction_evidence->'subset') = 'string'
            AND direction_evidence->>'subset' IN ('teacher', 'target')
            AND (
                (direction_evidence->>'subset' = 'teacher' AND direction_evidence->>'source_csv_sha256' = '2a69fbe3a130da8fb3a48a91e38e8c9a2bd13561dfeec9c258a3f30284a222c1')
                OR (direction_evidence->>'subset' = 'target' AND direction_evidence->>'source_csv_sha256' = '0d902b472980d40fa7b472998e3424c73ad4730108fac9655ac1b23e82ee573d')
            )
            AND jsonb_typeof(direction_evidence->'confidence') = 'null'
        )
        OR (
            direction_status IN ('inferred', 'confirmed')
            AND direction_degrees IS NOT NULL
            AND imputation_method = 'none'
            AND imputation_reason = 'legacy_unrecorded'
            AND direction_source = 'unknown'
            AND direction_evidence = '{}'::jsonb
        )
    ) IS TRUE);
