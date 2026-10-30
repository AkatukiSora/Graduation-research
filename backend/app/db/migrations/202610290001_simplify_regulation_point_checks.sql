-- Keep only the checks the application relies on. The provenance and evidence
-- columns stay as plain data; their shape is no longer enforced by the database.
ALTER TABLE regulation_datasets
    DROP CONSTRAINT regulation_datasets_external_direction_test_not_approved;

ALTER TABLE regulation_points
    DROP CONSTRAINT regulation_points_direction_consistency,
    DROP CONSTRAINT regulation_points_imputation_method_values,
    DROP CONSTRAINT regulation_points_imputation_reason_values,
    DROP CONSTRAINT regulation_points_direction_source_values,
    DROP CONSTRAINT regulation_points_direction_evidence_object,
    DROP CONSTRAINT regulation_points_validity_reason_values,
    DROP CONSTRAINT regulation_points_validity_evidence_object,
    DROP CONSTRAINT regulation_points_validity_consistency,
    ADD CONSTRAINT regulation_points_direction_status_degrees CHECK (
        (direction_status = 'unknown') = (direction_degrees IS NULL)
    );
