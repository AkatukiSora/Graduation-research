package domain

import "errors"

// ErrDatabaseUnavailable reports that the database could not serve a request
// (connection failure, timeout, shutdown, closed pool). Callers match it with
// errors.Is and answer with a retryable status.
var ErrDatabaseUnavailable = errors.New("database unavailable")

const (
	DirectionStatusUnknown   = "unknown"
	DirectionStatusInferred  = "inferred"
	DirectionStatusConfirmed = "confirmed"

	ValidityStatusUnknown = "unknown"
	ValidityStatusValid   = "valid"
	ValidityStatusInvalid = "invalid"
)

// StopPoint is one regulation point of the configured dataset. AZ is nil when
// the direction is unknown; candidate neighbour bearings are never used here.
type StopPoint struct {
	UniqueKey          string
	AZ                 *float64
	Latitude           float64
	Longitude          float64
	DirectionStatus    string
	ValidityStatus     string
	Synthetic          bool
	ProductionApproved bool
	DatasetID          string
	SourceMonth        string
}

// NotificationEligible is derived from stored state and never stored itself:
// a point may drive a notification only when its dataset is production
// approved and not synthetic, and the point has a confirmed direction and is
// valid.
func (s StopPoint) NotificationEligible() bool {
	return s.ProductionApproved &&
		!s.Synthetic &&
		s.DirectionStatus == DirectionStatusConfirmed &&
		s.ValidityStatus == ValidityStatusValid
}

type StopPointDetail struct {
	StopPoint
	CreatedAt string
	UpdatedAt string
}

type GetStopPointByIDInput struct {
	ID string
}

type GetStopPointsByRangeInput struct {
	Latitude     float64
	Longitude    float64
	RadiusMeters int
	Limit        int
}
