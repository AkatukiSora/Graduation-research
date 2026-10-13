// Package seed loads, validates and applies versioned regulation datasets.
//
// A dataset consists of a manifest and a dataset file. Loading never touches a
// database; Apply is the only code path that writes, and it runs in a single
// transaction guarded by an advisory lock.
package seed

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"path"
	"regexp"
	"sort"
)

const (
	// SchemaVersion is the only manifest and dataset format version understood.
	SchemaVersion = 1
	// DatasetFileName is the dataset file inside a dataset directory.
	DatasetFileName = "dataset.json"
	// ManifestFileName is the manifest file inside a dataset directory.
	ManifestFileName = "manifest.json"
	// BearingSemantics is the only allowed meaning of a candidate bearing: the
	// direction from one point to a neighbouring point, never a sign direction.
	BearingSemantics = "candidate_point_neighbor_only"
	// SyntheticRightsStatus is the rights status every embedded dataset carries.
	SyntheticRightsStatus = "synthetic-no-third-party-data"

	maxFileBytes = 64 << 20
)

var datasetIDPattern = regexp.MustCompile(`^[a-z0-9][a-z0-9._-]{0,63}$`)

// Counts are the row counts a manifest promises.
type Counts struct {
	Points        int `json:"points"`
	Candidates    int `json:"candidates"`
	Rejected      int `json:"rejected"`
	OutsideRegion int `json:"outside_region"`
}

// Manifest describes a dataset and the digest of its dataset file.
type Manifest struct {
	SchemaVersion      int             `json:"schema_version"`
	DatasetID          string          `json:"dataset_id"`
	DatasetFile        string          `json:"dataset_file"`
	SourceMonth        string          `json:"source_month"`
	RegionID           string          `json:"region_id"`
	Synthetic          bool            `json:"synthetic"`
	ProductionApproved bool            `json:"production_approved"`
	ContentSHA256      string          `json:"content_sha256"`
	Counts             Counts          `json:"counts"`
	Provenance         json.RawMessage `json:"provenance"`
	Generator          json.RawMessage `json:"generator"`
}

// Point is one regulation point. Direction fields stay unknown for synthetic data.
type Point struct {
	EntityID            string   `json:"entity_id"`
	RawUID              string   `json:"raw_uid"`
	Lon                 float64  `json:"lon"`
	Lat                 float64  `json:"lat"`
	DirectionDegrees    *float64 `json:"direction_degrees"`
	DirectionStatus     string   `json:"direction_status"`
	ValidityStatus      string   `json:"validity_status"`
	NormalizationStatus string   `json:"normalization_status"`
}

// Candidate links a point to one of its nearest neighbours.
type Candidate struct {
	SourceEntityID   string          `json:"source_entity_id"`
	NeighborEntityID string          `json:"neighbor_entity_id"`
	Rank             int             `json:"rank"`
	DistanceM        float64         `json:"distance_m"`
	CandidateBearing float64         `json:"candidate_bearing"`
	Features         json.RawMessage `json:"features"`
	BearingSemantics string          `json:"bearing_semantics"`
}

// Dataset is the content of a dataset file.
type Dataset struct {
	SchemaVersion int         `json:"schema_version"`
	DatasetID     string      `json:"dataset_id"`
	Points        []Point     `json:"points"`
	Candidates    []Candidate `json:"candidates"`
}

// Bundle is a loaded dataset together with the digest of its dataset file bytes.
type Bundle struct {
	Manifest      Manifest
	Dataset       Dataset
	ContentSHA256 string
}

// ValidDatasetID reports whether id is a safe dataset identifier.
func ValidDatasetID(id string) bool { return datasetIDPattern.MatchString(id) }

// Load reads <datasetID>/manifest.json and <datasetID>/dataset.json from fsys.
// It decodes strictly (unknown fields are errors) and computes the digest of
// the dataset file bytes; Validate compares that digest with the manifest.
func Load(fsys fs.FS, datasetID string) (*Bundle, error) {
	if !ValidDatasetID(datasetID) {
		return nil, invalid(CodeDatasetID, "dataset id %q is not a safe identifier", datasetID)
	}
	manifestBytes, err := readAll(fsys, path.Join(datasetID, ManifestFileName))
	if err != nil {
		return nil, fmt.Errorf("dataset %q is not available: %w", datasetID, err)
	}
	datasetBytes, err := readAll(fsys, path.Join(datasetID, DatasetFileName))
	if err != nil {
		return nil, fmt.Errorf("dataset %q is not available: %w", datasetID, err)
	}
	b := &Bundle{}
	if err := decodeStrict(manifestBytes, &b.Manifest); err != nil {
		return nil, fmt.Errorf("decode manifest of %q: %w", datasetID, err)
	}
	if err := decodeStrict(datasetBytes, &b.Dataset); err != nil {
		return nil, fmt.Errorf("decode dataset of %q: %w", datasetID, err)
	}
	if b.Manifest.DatasetID != datasetID || b.Dataset.DatasetID != datasetID {
		return nil, invalid(CodeDatasetID, "dataset %q declares manifest id %q and dataset id %q", datasetID, b.Manifest.DatasetID, b.Dataset.DatasetID)
	}
	sum := sha256.Sum256(datasetBytes)
	b.ContentSHA256 = hex.EncodeToString(sum[:])
	return b, nil
}

// DatasetIDs lists the dataset directories available in fsys in sorted order.
func DatasetIDs(fsys fs.FS) ([]string, error) {
	entries, err := fs.ReadDir(fsys, ".")
	if err != nil {
		return nil, fmt.Errorf("list datasets: %w", err)
	}
	var ids []string
	for _, entry := range entries {
		if entry.IsDir() && ValidDatasetID(entry.Name()) {
			ids = append(ids, entry.Name())
		}
	}
	sort.Strings(ids)
	return ids, nil
}

func readAll(fsys fs.FS, name string) ([]byte, error) {
	data, err := fs.ReadFile(fsys, name)
	if err != nil {
		return nil, err
	}
	if len(data) > maxFileBytes {
		return nil, fmt.Errorf("%s exceeds %d bytes", name, maxFileBytes)
	}
	return data, nil
}

func decodeStrict(data []byte, into any) error {
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(into); err != nil {
		return err
	}
	if _, err := decoder.Token(); !errors.Is(err, io.EOF) {
		return errors.New("unexpected data after the JSON document")
	}
	return nil
}
