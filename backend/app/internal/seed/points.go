package seed

import (
	"compress/gzip"
	"context"
	"encoding/csv"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"math"
	"path"
	"strconv"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

const (
	// PointsMetaFileName and PointsFileName are the files of a points-only dataset directory.
	PointsMetaFileName = "meta.json"
	PointsFileName     = "points.csv.gz"

	pointsBatchSize = 5000
)

// PointsDataset is a points-only dataset: one row per point with its location and an
// optional direction. It has no candidates, evidence or provenance.
type PointsDataset struct {
	DatasetID     string `json:"dataset_id"`
	SourceMonth   string `json:"source_month"`
	RegionID      string `json:"region_id"`
	ContentSHA256 string `json:"content_sha256"`
	PointCount    int    `json:"points"`

	Rows []PointRow `json:"-"`
}

// PointRow is one line of points.csv.gz: uid,lat,lon,az. A missing az means the
// direction is unknown.
type PointRow struct {
	UID string
	Lat float64
	Lon float64
	Az  *float64
}

// HasPoints reports whether fsys holds a points-only dataset for id.
func HasPoints(fsys fs.FS, id string) bool {
	_, err := fs.Stat(fsys, path.Join(id, PointsMetaFileName))
	return err == nil
}

// LoadPoints reads and validates <id>/meta.json and <id>/points.csv.gz.
func LoadPoints(fsys fs.FS, id string) (*PointsDataset, error) {
	if !ValidDatasetID(id) {
		return nil, invalid(CodeDatasetID, "dataset id %q is not a safe identifier", id)
	}
	metaBytes, err := readAll(fsys, path.Join(id, PointsMetaFileName))
	if err != nil {
		return nil, fmt.Errorf("dataset %q is not available: %w", id, err)
	}
	d := &PointsDataset{}
	if err := decodeStrict(metaBytes, d); err != nil {
		return nil, fmt.Errorf("decode meta of %q: %w", id, err)
	}
	if d.DatasetID != id {
		return nil, invalid(CodeDatasetID, "dataset %q declares id %q", id, d.DatasetID)
	}
	file, err := fsys.Open(path.Join(id, PointsFileName))
	if err != nil {
		return nil, fmt.Errorf("dataset %q is not available: %w", id, err)
	}
	defer file.Close()
	gz, err := gzip.NewReader(file)
	if err != nil {
		return nil, fmt.Errorf("open %s of %q: %w", PointsFileName, id, err)
	}
	defer gz.Close()

	reader := csv.NewReader(gz)
	reader.FieldsPerRecord = 4
	seen := map[string]struct{}{}
	for {
		record, err := reader.Read()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			return nil, fmt.Errorf("read %s of %q: %w", PointsFileName, id, err)
		}
		row := PointRow{UID: record[0]}
		if row.UID == "" || len(row.UID) > 32 {
			return nil, invalid(CodeUID, "point %q has an empty or over-long uid", row.UID)
		}
		if _, dup := seen[row.UID]; dup {
			return nil, invalid(CodeDuplicate, "uid %q appears more than once", row.UID)
		}
		seen[row.UID] = struct{}{}
		if row.Lat, err = strconv.ParseFloat(record[1], 64); err != nil || math.IsNaN(row.Lat) || row.Lat < -90 || row.Lat > 90 {
			return nil, invalid(CodeCoordinate, "point %q has invalid latitude %q", row.UID, record[1])
		}
		if row.Lon, err = strconv.ParseFloat(record[2], 64); err != nil || math.IsNaN(row.Lon) || row.Lon < -180 || row.Lon > 180 {
			return nil, invalid(CodeCoordinate, "point %q has invalid longitude %q", row.UID, record[2])
		}
		if record[3] != "" {
			az, err := strconv.ParseFloat(record[3], 64)
			if err != nil || math.IsNaN(az) || az < 0 || az >= 360 {
				return nil, invalid(CodeDirection, "point %q has invalid az %q", row.UID, record[3])
			}
			row.Az = &az
		}
		d.Rows = append(d.Rows, row)
	}
	if len(d.Rows) != d.PointCount {
		return nil, invalid(CodeCount, "%s of %q has %d rows, meta lists %d", PointsFileName, id, len(d.Rows), d.PointCount)
	}
	return d, nil
}

// ApplyPoints writes the dataset in one transaction. An identical dataset that is
// already stored is a no-op; the same id with another digest, or the same digest under
// another id, is rejected. Rows of other datasets are never modified.
func ApplyPoints(ctx context.Context, db Beginner, d *PointsDataset) (Result, error) {
	tx, err := db.Begin(ctx)
	if err != nil {
		return Result{}, fmt.Errorf("begin transaction: %w", err)
	}
	defer func() { _ = tx.Rollback(context.WithoutCancel(ctx)) }()

	if _, err := tx.Exec(ctx, "SET LOCAL lock_timeout = '"+lockTimeout+"'"); err != nil {
		return Result{}, fmt.Errorf("set lock timeout: %w", err)
	}
	if _, err := tx.Exec(ctx, "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", "regulation_datasets:"+d.DatasetID); err != nil {
		return Result{}, fmt.Errorf("lock dataset %q: %w", d.DatasetID, err)
	}

	var storedDigest string
	switch err := tx.QueryRow(ctx, "SELECT content_sha256 FROM regulation_datasets WHERE dataset_id = $1", d.DatasetID).Scan(&storedDigest); {
	case err == nil:
		if storedDigest != d.ContentSHA256 {
			return Result{}, fmt.Errorf("%w: dataset %q holds %s, seed provides %s", ErrDigestConflict, d.DatasetID, storedDigest, d.ContentSHA256)
		}
		var stored int
		if err := tx.QueryRow(ctx, "SELECT count(*) FROM regulation_points WHERE dataset_id = $1", d.DatasetID).Scan(&stored); err != nil {
			return Result{}, fmt.Errorf("count points of %q: %w", d.DatasetID, err)
		}
		if stored != d.PointCount {
			return Result{}, fmt.Errorf("%w: existing dataset %q holds %d points", ErrCountMismatch, d.DatasetID, stored)
		}
		return Result{Status: StatusUnchanged, Points: stored}, nil
	case errors.Is(err, pgx.ErrNoRows):
	default:
		return Result{}, fmt.Errorf("look up dataset %q: %w", d.DatasetID, err)
	}

	if _, err := tx.Exec(ctx, `INSERT INTO regulation_datasets (dataset_id, content_sha256, source_month, region_id, synthetic)
		VALUES ($1, $2, $3, $4, false)`, d.DatasetID, d.ContentSHA256, d.SourceMonth, d.RegionID); err != nil {
		var pgErr *pgconn.PgError
		if errors.As(err, &pgErr) && pgErr.Code == "23505" && pgErr.ConstraintName == digestUniqueKey {
			return Result{}, fmt.Errorf("%w: dataset %q", ErrDigestInUse, d.DatasetID)
		}
		return Result{}, fmt.Errorf("insert dataset %q: %w", d.DatasetID, err)
	}
	for start := 0; start < len(d.Rows); start += pointsBatchSize {
		batch := d.Rows[start:min(start+pointsBatchSize, len(d.Rows))]
		uids, lats, lons := make([]string, len(batch)), make([]float64, len(batch)), make([]float64, len(batch))
		azs := make([]*float64, len(batch))
		for i, row := range batch {
			uids[i], lats[i], lons[i], azs[i] = row.UID, row.Lat, row.Lon, row.Az
		}
		if _, err := tx.Exec(ctx, `INSERT INTO regulation_points
			(dataset_id, entity_id, raw_uid, geom, direction_degrees, direction_status, normalization_status)
			SELECT $1, t.uid, t.uid, ST_SetSRID(ST_MakePoint(t.lon, t.lat), 4326)::geography, t.az,
			       CASE WHEN t.az IS NULL THEN 'unknown' ELSE 'inferred' END, 'valid_candidate_only'
			FROM unnest($2::text[], $3::float8[], $4::float8[], $5::float8[]) AS t(uid, lat, lon, az)`,
			d.DatasetID, uids, lats, lons, azs); err != nil {
			return Result{}, fmt.Errorf("insert points of %q: %w", d.DatasetID, err)
		}
	}
	var stored int
	if err := tx.QueryRow(ctx, "SELECT count(*) FROM regulation_points WHERE dataset_id = $1", d.DatasetID).Scan(&stored); err != nil {
		return Result{}, fmt.Errorf("count points of %q: %w", d.DatasetID, err)
	}
	if stored != d.PointCount {
		return Result{}, fmt.Errorf("%w: dataset %q stored %d points, meta lists %d", ErrCountMismatch, d.DatasetID, stored, d.PointCount)
	}
	if err := tx.Commit(ctx); err != nil {
		return Result{}, fmt.Errorf("commit dataset %q: %w", d.DatasetID, err)
	}
	return Result{Status: StatusInserted, Points: stored}, nil
}
