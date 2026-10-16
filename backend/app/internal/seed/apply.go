package seed

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

const (
	// StatusInserted reports that the dataset was written by this call.
	StatusInserted = "inserted"
	// StatusUnchanged reports that the identical dataset was already present.
	StatusUnchanged = "unchanged"

	batchSize       = 500
	lockTimeout     = "60s"
	digestUniqueKey = "regulation_datasets_content_sha256_key"
)

var (
	// ErrDigestConflict means the dataset id exists with a different digest.
	ErrDigestConflict = errors.New("dataset id already exists with a different content digest")
	// ErrDigestInUse means the digest already belongs to another dataset id.
	ErrDigestInUse = errors.New("content digest already belongs to another dataset id")
	// ErrCountMismatch means stored row counts differ from the manifest.
	ErrCountMismatch = errors.New("stored row counts do not match the manifest")
)

// Beginner starts a transaction. *pgxpool.Pool and *pgx.Conn implement it.
type Beginner interface {
	Begin(ctx context.Context) (pgx.Tx, error)
}

// Result summarises an Apply call.
type Result struct {
	Status     string
	Points     int
	Candidates int
}

// Apply writes the dataset in one transaction. It validates the bundle first,
// serialises concurrent calls for the same dataset id with an advisory lock,
// treats an identical digest as a no-op, rejects a different digest under the
// same id, verifies stored row counts and commits only when they match. Any
// error rolls everything back. Rows of other datasets and the legacy
// stop_signs table are never read for update or modified.
func Apply(ctx context.Context, db Beginner, b *Bundle) (Result, error) {
	return applier{}.apply(ctx, db, b)
}

// applier carries test-only switches that exercise failure paths which
// validation would normally prevent.
type applier struct {
	skipValidation bool
	afterPoints    func() error
}

func (a applier) apply(ctx context.Context, db Beginner, b *Bundle) (result Result, err error) {
	if !a.skipValidation {
		if b != nil && b.v2 != nil {
			err = b.validateV2(ctx)
		} else {
			err = Validate(b)
		}
		if err != nil {
			return Result{}, err
		}
	}
	tx, err := db.Begin(ctx)
	if err != nil {
		return Result{}, fmt.Errorf("begin transaction: %w", err)
	}
	defer func() {
		// After a successful commit this returns pgx.ErrTxClosed, which is ignored.
		_ = tx.Rollback(context.WithoutCancel(ctx))
	}()

	id := b.Manifest.DatasetID
	if _, err := tx.Exec(ctx, "SET LOCAL lock_timeout = '"+lockTimeout+"'"); err != nil {
		return Result{}, fmt.Errorf("set lock timeout: %w", err)
	}
	if _, err := tx.Exec(ctx, "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", "regulation_datasets:"+id); err != nil {
		return Result{}, fmt.Errorf("lock dataset %q: %w", id, err)
	}

	var storedDigest string
	switch err := tx.QueryRow(ctx, "SELECT content_sha256 FROM regulation_datasets WHERE dataset_id = $1", id).Scan(&storedDigest); {
	case err == nil:
		if storedDigest != b.Manifest.ContentSHA256 {
			return Result{}, fmt.Errorf("%w: dataset %q holds %s, seed provides %s", ErrDigestConflict, id, storedDigest, b.Manifest.ContentSHA256)
		}
		points, candidates, err := storedCounts(ctx, tx, id)
		if err != nil {
			return Result{}, err
		}
		if points != b.Manifest.Counts.Points || candidates != b.Manifest.Counts.Candidates {
			return Result{}, fmt.Errorf("%w: existing dataset %q holds %d points and %d candidates", ErrCountMismatch, id, points, candidates)
		}
		if b.v2 != nil {
			if err := verifyV2StoredState(ctx, tx, b); err != nil {
				return Result{}, fmt.Errorf("replay dataset %q: %w", id, err)
			}
		}
		return Result{Status: StatusUnchanged, Points: points, Candidates: candidates}, nil
	case errors.Is(err, pgx.ErrNoRows):
	default:
		return Result{}, fmt.Errorf("look up dataset %q: %w", id, err)
	}

	if b.v2 != nil {
		provenance, err := json.Marshal(b.v2.manifest.Provenance)
		if err != nil {
			return Result{}, fmt.Errorf("encode v2 provenance: %w", err)
		}
		if _, err := tx.Exec(ctx, `INSERT INTO regulation_datasets
			(dataset_id, content_sha256, source_month, region_id, synthetic, provenance)
			VALUES ($1, $2, $3, $4, $5, $6::jsonb)`,
			id, b.Manifest.ContentSHA256, b.Manifest.SourceMonth, b.Manifest.RegionID, b.Manifest.Synthetic, provenance); err != nil {
			var pgErr *pgconn.PgError
			if errors.As(err, &pgErr) && pgErr.Code == "23505" && pgErr.ConstraintName == digestUniqueKey {
				return Result{}, fmt.Errorf("%w: dataset %q", ErrDigestInUse, id)
			}
			return Result{}, fmt.Errorf("insert dataset %q: %w", id, err)
		}
	} else if _, err := tx.Exec(ctx, `INSERT INTO regulation_datasets
		(dataset_id, content_sha256, source_month, region_id, synthetic, provenance)
		VALUES ($1, $2, $3, $4, $5, $6::jsonb)`,
		id, b.Manifest.ContentSHA256, b.Manifest.SourceMonth, b.Manifest.RegionID,
		b.Manifest.Synthetic, []byte(b.Manifest.Provenance)); err != nil {
		var pgErr *pgconn.PgError
		if errors.As(err, &pgErr) && pgErr.Code == "23505" && pgErr.ConstraintName == digestUniqueKey {
			return Result{}, fmt.Errorf("%w: dataset %q", ErrDigestInUse, id)
		}
		return Result{}, fmt.Errorf("insert dataset %q: %w", id, err)
	}
	if b.v2 != nil {
		if err := insertV2Points(ctx, tx, b.v2, id); err != nil {
			return Result{}, err
		}
	} else if err := insertPoints(ctx, tx, id, b.Dataset.Points); err != nil {
		return Result{}, err
	}
	if a.afterPoints != nil {
		if err := a.afterPoints(); err != nil {
			return Result{}, err
		}
	}
	if b.v2 != nil {
		if err := insertV2Candidates(ctx, tx, b.v2, id); err != nil {
			return Result{}, err
		}
	} else if err := insertCandidates(ctx, tx, id, b.Dataset.Candidates); err != nil {
		return Result{}, err
	}

	points, candidates, err := storedCounts(ctx, tx, id)
	if err != nil {
		return Result{}, err
	}
	expectedPoints, expectedCandidates := len(b.Dataset.Points), len(b.Dataset.Candidates)
	if b.v2 != nil {
		expectedPoints, expectedCandidates = b.v2.manifest.Counts.Points, b.v2.manifest.Counts.Candidates
	}
	if points != expectedPoints || points != b.Manifest.Counts.Points ||
		candidates != expectedCandidates || candidates != b.Manifest.Counts.Candidates {
		return Result{}, fmt.Errorf("%w: dataset %q stored %d points and %d candidates, manifest lists %d and %d",
			ErrCountMismatch, id, points, candidates, b.Manifest.Counts.Points, b.Manifest.Counts.Candidates)
	}
	if b.v2 != nil {
		if err := verifyV2StoredState(ctx, tx, b); err != nil {
			return Result{}, fmt.Errorf("verify v2 point state of %q: %w", id, err)
		}
	} else {
		var unknown int
		if err := tx.QueryRow(ctx, `SELECT count(*) FROM regulation_points WHERE dataset_id = $1
			AND direction_degrees IS NULL AND direction_status = 'unknown' AND validity_status = 'unknown'`, id).Scan(&unknown); err != nil {
			return Result{}, fmt.Errorf("verify uncertainty states of %q: %w", id, err)
		}
		if unknown != points {
			return Result{}, fmt.Errorf("%w: dataset %q stored %d points with known direction or validity", ErrCountMismatch, id, points-unknown)
		}
	}
	if err := tx.Commit(ctx); err != nil {
		return Result{}, fmt.Errorf("commit dataset %q: %w", id, err)
	}
	return Result{Status: StatusInserted, Points: points, Candidates: candidates}, nil
}

func storedCounts(ctx context.Context, tx pgx.Tx, id string) (points, candidates int, err error) {
	if err := tx.QueryRow(ctx, "SELECT count(*) FROM regulation_points WHERE dataset_id = $1", id).Scan(&points); err != nil {
		return 0, 0, fmt.Errorf("count points of %q: %w", id, err)
	}
	if err := tx.QueryRow(ctx, "SELECT count(*) FROM regulation_candidates WHERE dataset_id = $1", id).Scan(&candidates); err != nil {
		return 0, 0, fmt.Errorf("count candidates of %q: %w", id, err)
	}
	return points, candidates, nil
}

func insertPoints(ctx context.Context, tx pgx.Tx, id string, points []Point) error {
	for start := 0; start < len(points); start += batchSize {
		end := min(start+batchSize, len(points))
		n := end - start
		entities, uids, norms := make([]string, 0, n), make([]string, 0, n), make([]string, 0, n)
		lons, lats := make([]float64, 0, n), make([]float64, 0, n)
		for _, p := range points[start:end] {
			entities, uids, norms = append(entities, p.EntityID), append(uids, p.RawUID), append(norms, p.NormalizationStatus)
			lons, lats = append(lons, p.Lon), append(lats, p.Lat)
		}
		// Direction and validity are written as unknown by construction; the
		// seed never derives an official direction from candidate bearings.
		if _, err := tx.Exec(ctx, `INSERT INTO regulation_points
			(dataset_id, entity_id, raw_uid, geom, direction_degrees, direction_status, validity_status, normalization_status)
			SELECT $1::text, t.entity_id, t.raw_uid, ST_SetSRID(ST_MakePoint(t.lon, t.lat), 4326)::geography,
			       NULL, 'unknown', 'unknown', t.normalization_status
			FROM unnest($2::text[], $3::text[], $4::float8[], $5::float8[], $6::text[])
			     AS t(entity_id, raw_uid, lon, lat, normalization_status)`,
			id, entities, uids, lons, lats, norms); err != nil {
			return fmt.Errorf("insert points %d-%d of %q: %w", start, end-1, id, err)
		}
	}
	return nil
}

func insertCandidates(ctx context.Context, tx pgx.Tx, id string, candidates []Candidate) error {
	for start := 0; start < len(candidates); start += batchSize {
		end := min(start+batchSize, len(candidates))
		n := end - start
		sources, neighbors, features := make([]string, 0, n), make([]string, 0, n), make([]string, 0, n)
		ranks := make([]int32, 0, n)
		distances, bearings := make([]float64, 0, n), make([]float64, 0, n)
		for _, c := range candidates[start:end] {
			sources, neighbors, features = append(sources, c.SourceEntityID), append(neighbors, c.NeighborEntityID), append(features, string(c.Features))
			ranks = append(ranks, int32(c.Rank))
			distances, bearings = append(distances, c.DistanceM), append(bearings, c.CandidateBearing)
		}
		if _, err := tx.Exec(ctx, `INSERT INTO regulation_candidates
			(dataset_id, source_entity_id, neighbor_entity_id, rank, distance_m, candidate_bearing, features, bearing_semantics)
			SELECT $1::text, t.source_entity_id, t.neighbor_entity_id, t.rank, t.distance_m, t.candidate_bearing, t.features::jsonb,
			       'candidate_point_neighbor_only'
			FROM unnest($2::text[], $3::text[], $4::int4[], $5::float8[], $6::float8[], $7::text[])
			     AS t(source_entity_id, neighbor_entity_id, rank, distance_m, candidate_bearing, features)`,
			id, sources, neighbors, ranks, distances, bearings, features); err != nil {
			return fmt.Errorf("insert candidates %d-%d of %q: %w", start, end-1, id, err)
		}
	}
	return nil
}
