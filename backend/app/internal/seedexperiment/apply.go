package seedexperiment

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"reflect"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

const (
	batchSize       = 500
	lockTimeout     = "60s"
	digestUniqueKey = "experimental_direction_datasets_content_sha256_key"
)

var (
	ErrDigestConflict = errors.New("experimental dataset id already exists with a different content digest")
	ErrDigestInUse    = errors.New("experimental content digest already belongs to another dataset id")
	ErrCountMismatch  = errors.New("stored experimental row count does not match the manifest")
)

// Beginner is implemented by pgxpool.Pool and supports a single transaction.
type Beginner interface {
	Begin(ctx context.Context) (pgx.Tx, error)
}

// Apply writes a validated experimental dataset atomically and verifies every
// persisted row before commit. It never touches production regulation tables.
func Apply(ctx context.Context, db Beginner, bundle *Bundle) (Result, error) {
	if err := Validate(bundle); err != nil {
		return Result{}, err
	}
	tx, err := db.Begin(ctx)
	if err != nil {
		return Result{}, fmt.Errorf("begin experimental seed transaction: %w", err)
	}
	defer func() { _ = tx.Rollback(context.WithoutCancel(ctx)) }()
	if _, err := tx.Exec(ctx, "SET LOCAL lock_timeout = '"+lockTimeout+"'"); err != nil {
		return Result{}, fmt.Errorf("set experimental seed lock timeout: %w", err)
	}
	lockKey := "experimental_direction_datasets:" + bundle.Manifest.DatasetID
	if _, err := tx.Exec(ctx, "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", lockKey); err != nil {
		return Result{}, fmt.Errorf("lock experimental dataset %q: %w", bundle.Manifest.DatasetID, err)
	}

	var storedDigest string
	switch err := tx.QueryRow(ctx, `SELECT content_sha256 FROM experimental_direction_datasets WHERE dataset_id=$1`, bundle.Manifest.DatasetID).Scan(&storedDigest); {
	case err == nil:
		if storedDigest != bundle.ContentSHA256 {
			return Result{}, fmt.Errorf("%w: dataset %q stores %s; seed provides %s", ErrDigestConflict, bundle.Manifest.DatasetID, storedDigest, bundle.ContentSHA256)
		}
		result, err := verifyStored(ctx, tx, bundle)
		if err != nil {
			return Result{}, fmt.Errorf("verify existing experimental dataset: %w", err)
		}
		result.Status = "unchanged"
		return result, nil
	case errors.Is(err, pgx.ErrNoRows):
	default:
		return Result{}, fmt.Errorf("look up experimental dataset %q: %w", bundle.Manifest.DatasetID, err)
	}

	if _, err := tx.Exec(ctx, `INSERT INTO experimental_direction_datasets
		(dataset_id, content_sha256, row_count, data_purpose, production_approved, provenance)
		VALUES ($1, $2, $3, $4, false, $5::jsonb)`,
		bundle.Manifest.DatasetID, bundle.ContentSHA256, bundle.Manifest.Rows, bundle.Manifest.DataPurpose, []byte(bundle.Manifest.Provenance)); err != nil {
		var pgErr *pgconn.PgError
		if errors.As(err, &pgErr) && pgErr.Code == "23505" && pgErr.ConstraintName == digestUniqueKey {
			return Result{}, fmt.Errorf("%w: %q", ErrDigestInUse, bundle.Manifest.DatasetID)
		}
		return Result{}, fmt.Errorf("insert experimental dataset: %w", err)
	}
	if err := insertRows(ctx, tx, bundle); err != nil {
		return Result{}, err
	}
	result, err := verifyStored(ctx, tx, bundle)
	if err != nil {
		return Result{}, fmt.Errorf("verify inserted experimental dataset: %w", err)
	}
	if err := tx.Commit(ctx); err != nil {
		return Result{}, fmt.Errorf("commit experimental dataset: %w", err)
	}
	result.Status = "inserted"
	return result, nil
}

func insertRows(ctx context.Context, tx pgx.Tx, bundle *Bundle) error {
	for start := 0; start < len(bundle.Rows); start += batchSize {
		end := min(start+batchSize, len(bundle.Rows))
		payload, err := json.Marshal(bundle.Rows[start:end])
		if err != nil {
			return fmt.Errorf("encode experimental rows %d-%d: %w", start, end-1, err)
		}
		_, err = tx.Exec(ctx, `INSERT INTO experimental_direction_estimates
			(dataset_id, entity_id, raw_uid, subset, geom, direction_degrees, direction_status, source, confidence, error)
			SELECT $1, r.entity_id, r.raw_uid, r.subset,
			       ST_SetSRID(ST_MakePoint(r.lon, r.lat), 4326)::geography,
			       r.direction_degrees,
			       CASE WHEN r.direction_degrees IS NULL THEN 'unknown' ELSE 'inferred' END,
			       r.source, r.confidence, r.error
			FROM jsonb_to_recordset($2::jsonb) AS r(
			       entity_id text, raw_uid text, subset text, lat double precision, lon double precision,
			       direction_degrees double precision, source text, confidence double precision, error text)`,
			bundle.Manifest.DatasetID, payload)
		if err != nil {
			return fmt.Errorf("insert experimental rows %d-%d: %w", start, end-1, err)
		}
	}
	return nil
}

func verifyStored(ctx context.Context, tx pgx.Tx, bundle *Bundle) (Result, error) {
	var digest, purpose string
	var rowCount int
	var productionApproved bool
	var provenance []byte
	if err := tx.QueryRow(ctx, `SELECT content_sha256, row_count, data_purpose, production_approved, provenance
		FROM experimental_direction_datasets WHERE dataset_id=$1`, bundle.Manifest.DatasetID).
		Scan(&digest, &rowCount, &purpose, &productionApproved, &provenance); err != nil {
		return Result{}, fmt.Errorf("read experimental dataset metadata: %w", err)
	}
	if digest != bundle.ContentSHA256 || rowCount != bundle.Manifest.Rows || purpose != PurposeTest || productionApproved {
		return Result{}, fmt.Errorf("%w: metadata digest/count/purpose/approval mismatch", ErrCountMismatch)
	}
	var wantProvenance, gotProvenance any
	if err := json.Unmarshal(bundle.Manifest.Provenance, &wantProvenance); err != nil {
		return Result{}, fmt.Errorf("decode manifest provenance: %w", err)
	}
	if err := json.Unmarshal(provenance, &gotProvenance); err != nil || !reflect.DeepEqual(gotProvenance, wantProvenance) {
		return Result{}, errors.New("stored provenance differs from the manifest")
	}

	rows, err := tx.Query(ctx, `SELECT entity_id, raw_uid, subset, ST_Y(geom::geometry), ST_X(geom::geometry),
		direction_degrees, direction_status, source, confidence, error
		FROM experimental_direction_estimates WHERE dataset_id=$1 ORDER BY entity_id`, bundle.Manifest.DatasetID)
	if err != nil {
		return Result{}, fmt.Errorf("read back experimental estimates: %w", err)
	}
	defer rows.Close()
	want := make(map[string]Row, len(bundle.Rows))
	for _, row := range bundle.Rows {
		want[row.EntityID] = row
	}
	result := Result{}
	for rows.Next() {
		var entityID, rawUID, subset, status, source string
		var lat, lon float64
		var direction, confidence *float64
		var errorText *string
		if err := rows.Scan(&entityID, &rawUID, &subset, &lat, &lon, &direction, &status, &source, &confidence, &errorText); err != nil {
			return Result{}, fmt.Errorf("scan experimental estimate: %w", err)
		}
		expected, ok := want[entityID]
		if !ok {
			return Result{}, fmt.Errorf("unexpected stored estimate %q", entityID)
		}
		delete(want, entityID)
		wantStatus := "inferred"
		if expected.DirectionDegrees == nil {
			wantStatus = "unknown"
			result.Unknown++
		} else {
			result.Inferred++
		}
		if rawUID != expected.RawUID || subset != expected.Subset || lat != expected.Lat || lon != expected.Lon ||
			!equalFloatPointer(direction, expected.DirectionDegrees) || status != wantStatus || source != expected.Source ||
			confidence != nil || !equalStringPointer(errorText, expected.Error) {
			return Result{}, fmt.Errorf("stored estimate %q differs from its embedded input", entityID)
		}
		result.Rows++
	}
	if err := rows.Err(); err != nil {
		return Result{}, fmt.Errorf("iterate experimental estimates: %w", err)
	}
	if len(want) != 0 || result.Rows != rowCount || result.Rows != bundle.Manifest.Rows {
		return Result{}, fmt.Errorf("%w: stored %d rows, metadata %d, manifest %d, missing %d",
			ErrCountMismatch, result.Rows, rowCount, bundle.Manifest.Rows, len(want))
	}
	return result, nil
}

func equalFloatPointer(left, right *float64) bool {
	if left == nil || right == nil {
		return left == nil && right == nil
	}
	return *left == *right
}

func equalStringPointer(left, right *string) bool {
	if left == nil || right == nil {
		return left == nil && right == nil
	}
	return *left == *right
}
