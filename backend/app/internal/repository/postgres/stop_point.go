package postgres

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/NKC-CV2026/Graduation-research/backend/app/internal/domain"
	"github.com/NKC-CV2026/Graduation-research/backend/app/internal/repository"
	"github.com/NKC-CV2026/Graduation-research/backend/app/internal/repository/sqlc"
)

const (
	minRangeLimit = 1
	maxRangeLimit = 500
)

var _ repository.StopPointRepository = (*StopPointRepository)(nil)

// StopPointRepository reads regulation points of one configured dataset
// version. Every query is bound to that dataset, so rows of other versions are
// never returned.
type StopPointRepository struct {
	queries   *sqlc.Queries
	datasetID string
}

func NewStopPointRepository(pool *pgxpool.Pool, datasetID string) *StopPointRepository {
	return &StopPointRepository{queries: sqlc.New(pool), datasetID: datasetID}
}

// FindByID looks a point up by raw_uid. found is false, without an error, when
// the dataset has no such point. Timestamps are the stored row values.
func (r *StopPointRepository) FindByID(ctx context.Context, id string) (domain.StopPointDetail, bool, error) {
	// PostgreSQL text cannot hold NUL bytes or invalid UTF-8, so such an
	// identifier cannot exist in the dataset.
	if !utf8.ValidString(id) || strings.IndexByte(id, 0) >= 0 {
		return domain.StopPointDetail{}, false, nil
	}

	row, err := r.queries.FindRegulationPointByRawUID(ctx, sqlc.FindRegulationPointByRawUIDParams{
		DatasetID: r.datasetID,
		RawUid:    id,
	})
	if errors.Is(err, pgx.ErrNoRows) {
		return domain.StopPointDetail{}, false, nil
	}
	if err != nil {
		return domain.StopPointDetail{}, false, classifyError(err)
	}

	return domain.StopPointDetail{
		StopPoint: toStopPoint(row.RawUid, row.Latitude, row.Longitude, row.DirectionDegrees.Valid, row.DirectionDegrees.Float64,
			row.DirectionStatus, row.ValidityStatus, row.DatasetID, row.SourceMonth, row.Synthetic,
			row.ImputationMethod, row.ImputationReason, row.DirectionSource, row.DirectionEvidence,
			row.ValidityReason, row.ValidityEvidence, row.DataPurpose),
		CreatedAt: formatTimestamp(row.CreatedAt.Time),
		UpdatedAt: formatTimestamp(row.UpdatedAt.Time),
	}, true, nil
}

// FindByRange returns the points within the radius, nearest first. Points at
// the same distance are ordered by entity_id, so the order and the cut-off at
// the limit are stable.
func (r *StopPointRepository) FindByRange(ctx context.Context, input domain.GetStopPointsByRangeInput) ([]domain.StopPoint, error) {
	if input.Limit < minRangeLimit || input.Limit > maxRangeLimit {
		return nil, fmt.Errorf("limit %d is outside %d..%d", input.Limit, minRangeLimit, maxRangeLimit)
	}

	rows, err := r.queries.ListRegulationPointsInRange(ctx, sqlc.ListRegulationPointsInRangeParams{
		DatasetID:    r.datasetID,
		Longitude:    input.Longitude,
		Latitude:     input.Latitude,
		RadiusMeters: float64(input.RadiusMeters),
		MaxRows:      int32(input.Limit),
	})
	if err != nil {
		return nil, classifyError(err)
	}

	stopPoints := make([]domain.StopPoint, 0, len(rows))
	for _, row := range rows {
		stopPoints = append(stopPoints, toStopPoint(row.RawUid, row.Latitude, row.Longitude, row.DirectionDegrees.Valid, row.DirectionDegrees.Float64,
			row.DirectionStatus, row.ValidityStatus, row.DatasetID, row.SourceMonth, row.Synthetic,
			row.ImputationMethod, row.ImputationReason, row.DirectionSource, row.DirectionEvidence,
			row.ValidityReason, row.ValidityEvidence, row.DataPurpose))
	}

	return stopPoints, nil
}

func toStopPoint(rawUID string, latitude, longitude float64, hasDirection bool, direction float64,
	directionStatus, validityStatus, datasetID, sourceMonth string, synthetic bool,
	imputationMethod, imputationReason, directionSource string, directionEvidence []byte,
	validityReason string, validityEvidence []byte, dataPurpose string,
) domain.StopPoint {
	var az *float64
	if hasDirection {
		az = &direction
	}

	return domain.StopPoint{
		UniqueKey: rawUID, AZ: az, Latitude: latitude, Longitude: longitude,
		DirectionStatus: directionStatus, ValidityStatus: validityStatus, Synthetic: synthetic,
		DatasetID: datasetID, SourceMonth: sourceMonth, ImputationMethod: imputationMethod,
		ImputationReason: imputationReason, DirectionSource: directionSource,
		DirectionEvidence: append([]byte(nil), directionEvidence...), ValidityReason: validityReason,
		ValidityEvidence: append([]byte(nil), validityEvidence...), DataPurpose: dataPurpose,
	}
}

func formatTimestamp(t time.Time) string {
	return t.UTC().Format(time.RFC3339Nano)
}
