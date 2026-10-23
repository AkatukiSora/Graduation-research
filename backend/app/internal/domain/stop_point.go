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
)

// StopPoint is one search result. AZ is nil when the direction is unknown;
// candidate neighbour bearings are never used here. Provenance, evidence and
// dataset metadata stay in the database and are not part of this model.
type StopPoint struct {
	UniqueKey       string
	AZ              *float64
	Latitude        float64
	Longitude       float64
	DirectionStatus string
}

// StopPointDetail is the single-point view. It carries the same fields as
// StopPoint today but is a separate type so the two views can evolve apart.
type StopPointDetail struct {
	UniqueKey       string
	AZ              *float64
	Latitude        float64
	Longitude       float64
	DirectionStatus string
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
