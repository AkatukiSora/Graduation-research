package seed

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"math"
	"regexp"
	"sort"
	"unicode/utf8"
)

// Validation failure codes. Each identifies the first rule a bundle violates.
const (
	CodeSchemaVersion = "schema_version"
	CodeDatasetID     = "dataset_id"
	CodeMonth         = "source_month"
	CodeRegion        = "region_id"
	CodeHash          = "content_hash"
	CodeApproval      = "production_approved"
	CodeSynthetic     = "synthetic"
	CodeProvenance    = "provenance"
	CodeCount         = "count"
	CodeEntityID      = "entity_id"
	CodeUID           = "raw_uid"
	CodeDuplicate     = "duplicate"
	CodeCoordinate    = "coordinate"
	CodeDirection     = "direction"
	CodeValidity      = "validity"
	CodeNormalization = "normalization_status"
	CodeCandidateRef  = "candidate_reference"
	CodeSelfCandidate = "self_candidate"
	CodeRank          = "candidate_rank"
	CodeDistance      = "candidate_distance"
	CodeBearing       = "candidate_bearing"
	CodeSemantics     = "bearing_semantics"
	CodeFeatures      = "candidate_features"
)

const maxRawUIDLength = 32

var (
	monthPattern  = regexp.MustCompile(`^[0-9]{4}(0[1-9]|1[0-2])$`)
	digestPattern = regexp.MustCompile(`^[0-9a-f]{64}$`)
)

// ValidationError reports the rule a dataset violates.
type ValidationError struct {
	Code    string
	Message string
}

func (e *ValidationError) Error() string { return e.Code + ": " + e.Message }

func invalid(code, format string, args ...any) error {
	return &ValidationError{Code: code, Message: fmt.Sprintf(format, args...)}
}

// Validate checks a loaded bundle without touching a database: manifest and
// dataset format, digest, counts, identifiers, coordinates, uncertainty states
// and candidate references. Synthetic datasets must stay unknown in direction
// and validity and must never be production approved.
func Validate(b *Bundle) error {
	if b == nil {
		return invalid(CodeSchemaVersion, "bundle is nil")
	}
	if b.Manifest.SchemaVersion == v2SchemaVersion {
		return b.validateV2(context.Background())
	}
	m, d := &b.Manifest, &b.Dataset
	if m.SchemaVersion != SchemaVersion || d.SchemaVersion != SchemaVersion {
		return invalid(CodeSchemaVersion, "manifest version %d and dataset version %d must both be %d", m.SchemaVersion, d.SchemaVersion, SchemaVersion)
	}
	if !ValidDatasetID(m.DatasetID) {
		return invalid(CodeDatasetID, "dataset id %q is not a safe identifier", m.DatasetID)
	}
	if d.DatasetID != m.DatasetID {
		return invalid(CodeDatasetID, "dataset file id %q differs from manifest id %q", d.DatasetID, m.DatasetID)
	}
	if m.DatasetFile != DatasetFileName {
		return invalid(CodeDatasetID, "manifest dataset_file must be %q", DatasetFileName)
	}
	if !digestPattern.MatchString(m.ContentSHA256) || !digestPattern.MatchString(b.ContentSHA256) {
		return invalid(CodeHash, "content digest must be 64 lowercase hex characters")
	}
	if b.ContentSHA256 != m.ContentSHA256 {
		return invalid(CodeHash, "dataset file digest %s differs from manifest digest %s", b.ContentSHA256, m.ContentSHA256)
	}
	if !monthPattern.MatchString(m.SourceMonth) {
		return invalid(CodeMonth, "source month %q is not YYYYMM", m.SourceMonth)
	}
	if m.RegionID == "" {
		return invalid(CodeRegion, "region id is empty")
	}
	if m.ProductionApproved {
		return invalid(CodeApproval, "embedded datasets must not be production approved")
	}
	if !m.Synthetic {
		return invalid(CodeSynthetic, "only synthetic datasets may be embedded")
	}
	if err := validateProvenance(m.Provenance); err != nil {
		return err
	}
	if m.Counts.Rejected != 0 || m.Counts.OutsideRegion != 0 {
		return invalid(CodeCount, "rejected (%d) and outside-region (%d) rows must be zero", m.Counts.Rejected, m.Counts.OutsideRegion)
	}
	if m.Counts.Points != len(d.Points) {
		return invalid(CodeCount, "manifest lists %d points but the dataset holds %d", m.Counts.Points, len(d.Points))
	}
	if m.Counts.Candidates != len(d.Candidates) {
		return invalid(CodeCount, "manifest lists %d candidates but the dataset holds %d", m.Counts.Candidates, len(d.Candidates))
	}
	entities, err := validatePoints(d.Points)
	if err != nil {
		return err
	}
	return validateCandidates(d.Candidates, entities)
}

func validateProvenance(raw json.RawMessage) error {
	var provenance struct {
		RightsStatus string `json:"rights_status"`
	}
	trimmed := bytes.TrimSpace(raw)
	if len(trimmed) == 0 || trimmed[0] != '{' || json.Unmarshal(trimmed, &provenance) != nil {
		return invalid(CodeProvenance, "provenance must be a JSON object")
	}
	if provenance.RightsStatus != SyntheticRightsStatus {
		return invalid(CodeProvenance, "rights_status must be %q", SyntheticRightsStatus)
	}
	return nil
}

func validatePoints(points []Point) (map[string]struct{}, error) {
	entities := make(map[string]struct{}, len(points))
	uids := make(map[string]struct{}, len(points))
	for i, p := range points {
		if p.EntityID == "" {
			return nil, invalid(CodeEntityID, "point %d has an empty entity id", i)
		}
		if n := utf8.RuneCountInString(p.RawUID); n == 0 || n > maxRawUIDLength {
			return nil, invalid(CodeUID, "point %q raw uid length %d is outside 1..%d", p.EntityID, n, maxRawUIDLength)
		}
		if _, dup := entities[p.EntityID]; dup {
			return nil, invalid(CodeDuplicate, "entity id %q appears twice", p.EntityID)
		}
		if _, dup := uids[p.RawUID]; dup {
			return nil, invalid(CodeDuplicate, "raw uid %q appears twice", p.RawUID)
		}
		entities[p.EntityID] = struct{}{}
		uids[p.RawUID] = struct{}{}
		if !finite(p.Lon) || !finite(p.Lat) || p.Lon < -180 || p.Lon > 180 || p.Lat < -90 || p.Lat > 90 {
			return nil, invalid(CodeCoordinate, "point %q has coordinates outside lon [-180,180] / lat [-90,90] or not finite", p.EntityID)
		}
		if p.DirectionDegrees != nil || p.DirectionStatus != "unknown" {
			return nil, invalid(CodeDirection, "point %q must have no direction and status unknown", p.EntityID)
		}
		if p.ValidityStatus != "unknown" {
			return nil, invalid(CodeValidity, "point %q validity must be unknown", p.EntityID)
		}
		if p.NormalizationStatus == "" {
			return nil, invalid(CodeNormalization, "point %q has an empty normalization status", p.EntityID)
		}
	}
	return entities, nil
}

func validateCandidates(candidates []Candidate, entities map[string]struct{}) error {
	type edge struct{ source, neighbor string }
	type slot struct {
		source string
		rank   int
	}
	edges := make(map[edge]struct{}, len(candidates))
	slots := make(map[slot]struct{}, len(candidates))
	perSource := make(map[string][]int)
	for i, c := range candidates {
		if _, ok := entities[c.SourceEntityID]; !ok {
			return invalid(CodeCandidateRef, "candidate %d source %q is not a point of the dataset", i, c.SourceEntityID)
		}
		if _, ok := entities[c.NeighborEntityID]; !ok {
			return invalid(CodeCandidateRef, "candidate %d neighbour %q is not a point of the dataset", i, c.NeighborEntityID)
		}
		if c.SourceEntityID == c.NeighborEntityID {
			return invalid(CodeSelfCandidate, "candidate %d links %q to itself", i, c.SourceEntityID)
		}
		if c.Rank < 1 {
			return invalid(CodeRank, "candidate %d rank %d must be at least 1", i, c.Rank)
		}
		if !finite(c.DistanceM) || c.DistanceM < 0 {
			return invalid(CodeDistance, "candidate %d distance must be finite and non-negative", i)
		}
		if !finite(c.CandidateBearing) || c.CandidateBearing < 0 || c.CandidateBearing >= 360 {
			return invalid(CodeBearing, "candidate %d bearing must be finite and within [0,360)", i)
		}
		if c.BearingSemantics != BearingSemantics {
			return invalid(CodeSemantics, "candidate %d bearing semantics must be %q", i, BearingSemantics)
		}
		if features := bytes.TrimSpace(c.Features); len(features) == 0 || features[0] != '{' || !json.Valid(features) {
			return invalid(CodeFeatures, "candidate %d features must be a JSON object", i)
		}
		e := edge{c.SourceEntityID, c.NeighborEntityID}
		if _, dup := edges[e]; dup {
			return invalid(CodeDuplicate, "candidate %d repeats neighbour %q of source %q", i, c.NeighborEntityID, c.SourceEntityID)
		}
		edges[e] = struct{}{}
		s := slot{c.SourceEntityID, c.Rank}
		if _, dup := slots[s]; dup {
			return invalid(CodeDuplicate, "candidate %d repeats rank %d of source %q", i, c.Rank, c.SourceEntityID)
		}
		slots[s] = struct{}{}
		perSource[c.SourceEntityID] = append(perSource[c.SourceEntityID], c.Rank)
	}
	sources := make([]string, 0, len(perSource))
	for source := range perSource {
		sources = append(sources, source)
	}
	sort.Strings(sources)
	for _, source := range sources {
		ranks := perSource[source]
		highest := 0
		for _, r := range ranks {
			if r > highest {
				highest = r
			}
		}
		// Ranks are unique and at least 1, so contiguity means max == count.
		if highest != len(ranks) {
			return invalid(CodeRank, "source %q ranks are not contiguous from 1 (%d candidates, highest rank %d)", source, len(ranks), highest)
		}
	}
	return nil
}

func finite(v float64) bool { return !math.IsNaN(v) && !math.IsInf(v, 0) }
