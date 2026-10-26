package seedexperiment

import (
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"regexp"
	"strings"
)

var digestPattern = regexp.MustCompile(`^[0-9a-f]{64}$`)

// Validate checks the fixed generated dataset contract before any database access.
func Validate(bundle *Bundle) error {
	if bundle == nil {
		return errors.New("experimental direction bundle is nil")
	}
	m := bundle.Manifest
	if m.SchemaVersion != 1 || m.DatasetID != DatasetID || m.SourceMonth != "202602" ||
		m.DataPurpose != PurposeTest || m.ProductionApproved || m.ConfidenceSemantics != "not_provided" ||
		m.SourceSemantics != SourceSemantics {
		return errors.New("manifest identity, purpose, approval, or semantics mismatch")
	}
	if !digestPattern.MatchString(m.DataSHA256) || bundle.ContentSHA256 != m.DataSHA256 {
		return errors.New("manifest or loaded content digest is invalid")
	}
	if m.Rows != 2333 || len(bundle.Rows) != 2333 {
		return fmt.Errorf("row count mismatch: manifest %d, loaded %d, want 2333", m.Rows, len(bundle.Rows))
	}
	if err := validateProvenance(m.Provenance); err != nil {
		return fmt.Errorf("provenance: %w", err)
	}

	seenIDs := make(map[string]struct{}, len(bundle.Rows))
	seenUIDs := make(map[string]struct{}, len(bundle.Rows))
	counts := map[string]SubsetCounts{"teacher": {}, "target": {}}
	lastSubset := ""
	for index, row := range bundle.Rows {
		if row.EntityID == "" || row.RawUID == "" || len(row.RawUID) > 32 {
			return fmt.Errorf("row %d has an invalid entity id or raw uid", index+1)
		}
		if _, ok := seenIDs[row.EntityID]; ok {
			return fmt.Errorf("duplicate entity_id %q", row.EntityID)
		}
		if _, ok := seenUIDs[row.RawUID]; ok {
			return fmt.Errorf("duplicate raw_uid %q", row.RawUID)
		}
		seenIDs[row.EntityID] = struct{}{}
		seenUIDs[row.RawUID] = struct{}{}
		if row.RawUID != row.EntityID[strings.LastIndex(row.EntityID, "/")+1:] {
			return fmt.Errorf("row %q raw_uid does not match its entity id", row.EntityID)
		}
		if row.Subset != "teacher" && row.Subset != "target" {
			return fmt.Errorf("row %q has invalid subset %q", row.EntityID, row.Subset)
		}
		if lastSubset == "target" && row.Subset == "teacher" {
			return errors.New("rows are not ordered teacher before target")
		}
		lastSubset = row.Subset
		if row.Source != SourceExternal {
			return fmt.Errorf("row %q has an unsupported source", row.EntityID)
		}
		if math.IsNaN(row.Lat) || math.IsInf(row.Lat, 0) || row.Lat < -90 || row.Lat > 90 ||
			math.IsNaN(row.Lon) || math.IsInf(row.Lon, 0) || row.Lon < -180 || row.Lon > 180 {
			return fmt.Errorf("row %q has invalid coordinates", row.EntityID)
		}
		if row.Confidence != nil {
			return fmt.Errorf("row %q confidence must remain null", row.EntityID)
		}
		c := counts[row.Subset]
		c.Rows++
		if row.DirectionDegrees == nil {
			if row.Error == nil || strings.TrimSpace(*row.Error) == "" {
				return fmt.Errorf("unknown row %q must retain a non-empty error", row.EntityID)
			}
			c.Unknown++
		} else {
			direction := *row.DirectionDegrees
			if math.IsNaN(direction) || math.IsInf(direction, 0) || direction < 0 || direction >= 360 {
				return fmt.Errorf("row %q direction is outside [0, 360)", row.EntityID)
			}
			if row.Error != nil {
				return fmt.Errorf("inferred row %q must have null error", row.EntityID)
			}
			c.Inferred++
		}
		counts[row.Subset] = c
	}
	if counts["teacher"] != (SubsetCounts{Rows: 394, Inferred: 388, Unknown: 6}) ||
		counts["target"] != (SubsetCounts{Rows: 1939, Inferred: 1937, Unknown: 2}) ||
		m.Counts.Teacher != counts["teacher"] || m.Counts.Target != counts["target"] {
		return fmt.Errorf("subset counts mismatch: teacher=%+v target=%+v", counts["teacher"], counts["target"])
	}
	return nil
}

func validateProvenance(raw json.RawMessage) error {
	var p map[string]json.RawMessage
	if len(raw) == 0 || json.Unmarshal(raw, &p) != nil || p == nil {
		return errors.New("must be a JSON object")
	}
	candidate, err := objectAt(p, "candidate_reference")
	if err != nil {
		return err
	}
	if stringAt(candidate, "commit") != "638e6008501e3055927f3f4be8365968aecdb31b" ||
		stringAt(candidate, "candidate_dataset_id") != "c1r1-kansai-chubu-v1" {
		return errors.New("candidate reference does not identify the frozen dataset")
	}
	var fields []string
	if err := json.Unmarshal(candidate["inference_fields_only"], &fields); err != nil ||
		!equalStrings(fields, []string{"uniqueKey", "lat", "lon"}) {
		return errors.New("candidate inference fields must be uniqueKey, lat, lon only")
	}
	files, err := objectAt(candidate, "files")
	if err != nil {
		return err
	}
	for subset, expected := range map[string]struct {
		hash string
		rows int
	}{
		"teacher": {"3ecd92c3c10349cfc750d7523732132b466636e2ca6b8b34dd4665229d78681b", 394},
		"target":  {"7829d7733625edd3e9f6b7fafc578529697443db286ae9a6aada275ffaacd320", 1939},
	} {
		entry, err := objectAt(files, subset)
		if err != nil {
			return err
		}
		if stringAt(entry, "sha256") != expected.hash || intAt(entry, "rows") != expected.rows {
			return fmt.Errorf("candidate %s file evidence mismatch", subset)
		}
	}
	external, err := objectAt(p, "external_source")
	if err != nil {
		return err
	}
	if _, ok := external["redistributed"]; !ok {
		return errors.New("external source redistribution declaration is missing")
	}
	if stringAt(external, "archive_sha1") != "9c2a7d06cb2ffeb0fe8424065236e157be1908e6" ||
		boolAt(external, "redistributed") || !strings.Contains(strings.ToLower(stringAt(external, "license_status")), "unconfirmed") ||
		!strings.Contains(strings.ToLower(stringAt(external, "online_code")), "not called") {
		return errors.New("external source hash, rights, or offline-use statement mismatch")
	}
	osm, err := objectAt(p, "osm_inputs")
	if err != nil {
		return err
	}
	for subset, expected := range map[string]string{
		"teacher": "c09d28306826347c3b9b15cf4c0c7a5a44c3f5c041c5d1bd71056a6fd3ce0dcb",
		"target":  "9f05616dc818465ae585978b21a7160b9734305c0bfea903da4c3f2c9bde2650",
	} {
		entry, err := objectAt(osm, subset)
		if err != nil {
			return err
		}
		if stringAt(entry, "sha256") != expected {
			return fmt.Errorf("%s OSM input hash mismatch", subset)
		}
	}
	if intAt(p, "radius_m") != 80 {
		return errors.New("inference radius must remain 80 meters")
	}
	attribution, err := objectAt(p, "attribution")
	if err != nil {
		return err
	}
	if !strings.Contains(strings.ToUpper(stringAt(attribution, "reference")), "JARTIC") ||
		!strings.Contains(stringAt(attribution, "osm"), "OpenStreetMap contributors") ||
		!strings.Contains(stringAt(attribution, "osm"), "ODbL 1.0") {
		return errors.New("JARTIC and OpenStreetMap attribution is incomplete")
	}
	scope, err := objectAt(p, "evaluation_scope")
	if err != nil {
		return err
	}
	if boolAt(scope, "candidate_comparison_is_ground_truth") || boolAt(scope, "fresh_target_accuracy_measured") {
		return errors.New("provenance must not claim measured accuracy")
	}
	return nil
}

func objectAt(parent map[string]json.RawMessage, key string) (map[string]json.RawMessage, error) {
	var value map[string]json.RawMessage
	if err := json.Unmarshal(parent[key], &value); err != nil || value == nil {
		return nil, fmt.Errorf("%s must be a JSON object", key)
	}
	return value, nil
}

func stringAt(parent map[string]json.RawMessage, key string) string {
	var value string
	_ = json.Unmarshal(parent[key], &value)
	return value
}

func boolAt(parent map[string]json.RawMessage, key string) bool {
	var value bool
	_ = json.Unmarshal(parent[key], &value)
	return value
}

func intAt(parent map[string]json.RawMessage, key string) int {
	var value int
	_ = json.Unmarshal(parent[key], &value)
	return value
}

func equalStrings(left, right []string) bool {
	if len(left) != len(right) {
		return false
	}
	for i := range left {
		if left[i] != right[i] {
			return false
		}
	}
	return true
}
