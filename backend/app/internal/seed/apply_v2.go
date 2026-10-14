package seed

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"
)

func insertV2Points(ctx context.Context, tx pgx.Tx, source *v2Source, datasetID string) error {
	batch := make([]Point, 0, batchSize)
	flush := func() error {
		if len(batch) == 0 {
			return nil
		}
		entities, uids, norms := make([]string, 0, len(batch)), make([]string, 0, len(batch)), make([]string, 0, len(batch))
		methods, reasons, sources := make([]string, 0, len(batch)), make([]string, 0, len(batch)), make([]string, 0, len(batch))
		statuses, validityStatuses, validityReasons := make([]string, 0, len(batch)), make([]string, 0, len(batch)), make([]string, 0, len(batch))
		lons, lats := make([]float64, 0, len(batch)), make([]float64, 0, len(batch))
		degrees := make([]pgtype.Float8, 0, len(batch))
		directionEvidence, validityEvidence := make([]string, 0, len(batch)), make([]string, 0, len(batch))
		for _, point := range batch {
			entities, uids, norms = append(entities, point.EntityID), append(uids, point.RawUID), append(norms, point.NormalizationStatus)
			methods, reasons, sources = append(methods, point.ImputationMethod), append(reasons, point.ImputationReason), append(sources, point.DirectionSource)
			statuses, validityStatuses, validityReasons = append(statuses, point.DirectionStatus), append(validityStatuses, point.ValidityStatus), append(validityReasons, point.ValidityReason)
			lons, lats = append(lons, point.Lon), append(lats, point.Lat)
			degree := pgtype.Float8{}
			if point.DirectionDegrees != nil {
				degree.Float64, degree.Valid = *point.DirectionDegrees, true
			}
			degrees = append(degrees, degree)
			directionEvidence, validityEvidence = append(directionEvidence, string(point.DirectionEvidence)), append(validityEvidence, string(point.ValidityEvidence))
		}
		if _, err := tx.Exec(ctx, `INSERT INTO regulation_points
			(dataset_id, entity_id, raw_uid, geom, direction_degrees, direction_status, validity_status, normalization_status,
			 imputation_method, imputation_reason, direction_source, direction_evidence, validity_reason, validity_evidence)
			SELECT $1::text, t.entity_id, t.raw_uid, ST_SetSRID(ST_MakePoint(t.lon, t.lat), 4326)::geography,
			       t.direction_degrees, t.direction_status, t.validity_status, t.normalization_status,
			       t.imputation_method, t.imputation_reason, t.direction_source, t.direction_evidence::jsonb,
			       t.validity_reason, t.validity_evidence::jsonb
			FROM unnest($2::text[], $3::text[], $4::float8[], $5::float8[], $6::float8[], $7::text[], $8::text[], $9::text[],
			            $10::text[], $11::text[], $12::text[], $13::text[], $14::text[], $15::text[])
			     AS t(entity_id, raw_uid, lon, lat, direction_degrees, direction_status, validity_status, normalization_status,
			          imputation_method, imputation_reason, direction_source, direction_evidence, validity_reason, validity_evidence)`,
			datasetID, entities, uids, lons, lats, degrees, statuses, validityStatuses, norms, methods, reasons, sources,
			directionEvidence, validityReasons, validityEvidence); err != nil {
			return fmt.Errorf("insert v2 points batch: %w", err)
		}
		batch = batch[:0]
		return nil
	}
	if err := scanV2Kind(ctx, source, "points", func(line []byte, rowNumber int) error {
		var point Point
		if err := decodeV2Strict(line, &point, v2PointKeys); err != nil {
			return fmt.Errorf("decode point row %d: %w", rowNumber, err)
		}
		batch = append(batch, point)
		if len(batch) == batchSize {
			return flush()
		}
		return nil
	}); err != nil {
		return err
	}
	return flush()
}

func insertV2Candidates(ctx context.Context, tx pgx.Tx, source *v2Source, datasetID string) error {
	batch := make([]Candidate, 0, batchSize)
	flush := func() error {
		if len(batch) == 0 {
			return nil
		}
		sources, neighbors, features := make([]string, 0, len(batch)), make([]string, 0, len(batch)), make([]string, 0, len(batch))
		ranks := make([]int32, 0, len(batch))
		distances, bearings := make([]float64, 0, len(batch)), make([]float64, 0, len(batch))
		for _, candidate := range batch {
			sources, neighbors, features = append(sources, candidate.SourceEntityID), append(neighbors, candidate.NeighborEntityID), append(features, string(candidate.Features))
			ranks = append(ranks, int32(candidate.Rank))
			distances, bearings = append(distances, candidate.DistanceM), append(bearings, candidate.CandidateBearing)
		}
		if _, err := tx.Exec(ctx, `INSERT INTO regulation_candidates
			(dataset_id, source_entity_id, neighbor_entity_id, rank, distance_m, candidate_bearing, features, bearing_semantics)
			SELECT $1::text, t.source_entity_id, t.neighbor_entity_id, t.rank, t.distance_m, t.candidate_bearing, t.features::jsonb, $8::text
			FROM unnest($2::text[], $3::text[], $4::int4[], $5::float8[], $6::float8[], $7::text[])
			     AS t(source_entity_id, neighbor_entity_id, rank, distance_m, candidate_bearing, features)`,
			datasetID, sources, neighbors, ranks, distances, bearings, features, BearingSemantics); err != nil {
			return fmt.Errorf("insert v2 candidate batch: %w", err)
		}
		batch = batch[:0]
		return nil
	}
	if err := scanV2Kind(ctx, source, "candidates", func(line []byte, rowNumber int) error {
		var candidate Candidate
		if err := decodeV2Strict(line, &candidate, v2CandidateKeys); err != nil {
			return fmt.Errorf("decode candidate row %d: %w", rowNumber, err)
		}
		batch = append(batch, candidate)
		if len(batch) == batchSize {
			return flush()
		}
		return nil
	}); err != nil {
		return err
	}
	return flush()
}

func verifyV2StoredState(ctx context.Context, tx pgx.Tx, bundle *Bundle) error {
	rows, err := tx.Query(ctx, `SELECT entity_id, direction_degrees, direction_status, imputation_method, imputation_reason,
		direction_source, direction_evidence::text, validity_status, validity_reason, validity_evidence::text
		FROM regulation_points WHERE dataset_id=$1 ORDER BY entity_id`, bundle.Manifest.DatasetID)
	if err != nil {
		return fmt.Errorf("read stored v2 point state: %w", err)
	}
	defer rows.Close()
	rowNumber := 0
	err = scanV2Kind(ctx, bundle.v2, "points", func(line []byte, _ int) error {
		var expected Point
		if err := decodeV2Strict(line, &expected, v2PointKeys); err != nil {
			return err
		}
		if !rows.Next() {
			return invalid(CodeCount, "stored v2 point state has fewer rows than the seed")
		}
		var gotEntity, gotDirectionStatus, gotMethod, gotReason, gotSource string
		var gotDegree pgtype.Float8
		var gotDirectionEvidence, gotValidityEvidence []byte
		var gotValidityStatus, gotValidityReason string
		if err := rows.Scan(&gotEntity, &gotDegree, &gotDirectionStatus, &gotMethod, &gotReason, &gotSource,
			&gotDirectionEvidence, &gotValidityStatus, &gotValidityReason, &gotValidityEvidence); err != nil {
			return err
		}
		rowNumber++
		if gotEntity != expected.EntityID || gotDirectionStatus != expected.DirectionStatus || gotMethod != expected.ImputationMethod ||
			gotReason != expected.ImputationReason || gotSource != expected.DirectionSource || gotValidityStatus != expected.ValidityStatus || gotValidityReason != expected.ValidityReason {
			return invalid(CodeDirection, "stored state fields differ for point %q", expected.EntityID)
		}
		if (expected.DirectionDegrees == nil) != !gotDegree.Valid || expected.DirectionDegrees != nil && *expected.DirectionDegrees != gotDegree.Float64 {
			return invalid(CodeDirection, "stored direction differs for point %q", expected.EntityID)
		}
		if !jsonValuesEqual(expected.DirectionEvidence, gotDirectionEvidence) || !jsonValuesEqual(expected.ValidityEvidence, gotValidityEvidence) {
			return invalid(CodeDirection, "stored state evidence differs for point %q", expected.EntityID)
		}
		return nil
	})
	if err != nil {
		return fmt.Errorf("verify stored v2 state after %d points: %w", rowNumber, err)
	}
	if rows.Next() {
		return invalid(CodeCount, "stored v2 point state has more rows than the seed")
	}
	if err := rows.Err(); err != nil {
		return fmt.Errorf("read stored v2 point state: %w", err)
	}
	return nil
}

func jsonValuesEqual(left, right []byte) bool {
	canonicalLeft, err := canonicalAnyJSON(left)
	if err != nil {
		return false
	}
	canonicalRight, err := canonicalAnyJSON(right)
	return err == nil && bytes.Equal(canonicalLeft, canonicalRight)
}

func canonicalAnyJSON(raw []byte) ([]byte, error) {
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	var value any
	if err := decoder.Decode(&value); err != nil {
		return nil, err
	}
	var out bytes.Buffer
	encoder := json.NewEncoder(&out)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(value); err != nil {
		return nil, err
	}
	return bytes.TrimSuffix(out.Bytes(), []byte("\n")), nil
}
