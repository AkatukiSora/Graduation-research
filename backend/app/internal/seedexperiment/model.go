// Package seedexperiment handles the explicitly opt-in experimental direction dataset.
// It is intentionally separate from the production regulation seed and API reader.
package seedexperiment

import (
	"encoding/json"
	"io/fs"
)

const (
	DatasetID       = "external-stop-approach-test-202602-v1"
	SourceExternal  = "external_local_stop_approach"
	PurposeTest     = "direction_test"
	SourceSemantics = "vehicle_approach_to_stop_estimate"
)

// Manifest describes the immutable generated JSONL and its provenance.
type Manifest struct {
	SchemaVersion       int             `json:"schema_version"`
	DatasetID           string          `json:"dataset_id"`
	SourceMonth         string          `json:"source_month"`
	DataPurpose         string          `json:"data_purpose"`
	ProductionApproved  bool            `json:"production_approved"`
	ConfidenceSemantics string          `json:"confidence_semantics"`
	Rows                int             `json:"rows"`
	Counts              Counts          `json:"counts"`
	SourceSemantics     string          `json:"source_semantics"`
	DataSHA256          string          `json:"data_sha256"`
	Provenance          json.RawMessage `json:"provenance"`
}

type Counts struct {
	Teacher SubsetCounts `json:"teacher"`
	Target  SubsetCounts `json:"target"`
}

type SubsetCounts struct {
	Rows     int `json:"rows"`
	Inferred int `json:"inferred"`
	Unknown  int `json:"unknown"`
}

// Row has the exact fields emitted by the generation task. A nil direction is
// an intentional abstention; confidence remains nil because the source does not
// provide a calibrated confidence value.
type Row struct {
	EntityID         string   `json:"entity_id"`
	RawUID           string   `json:"raw_uid"`
	Subset           string   `json:"subset"`
	Lat              float64  `json:"lat"`
	Lon              float64  `json:"lon"`
	DirectionDegrees *float64 `json:"direction_degrees"`
	Source           string   `json:"source"`
	Confidence       *float64 `json:"confidence"`
	Error            *string  `json:"error"`
}

// Bundle is the validated source manifest and ordered estimate rows.
type Bundle struct {
	Manifest      Manifest
	Rows          []Row
	ContentSHA256 string
}

type Result struct {
	Status   string
	Rows     int
	Inferred int
	Unknown  int
}

//go:generate gofmt -w model.go load.go validate.go apply.go

// FS returns the embedded, test-only dataset files.
func FS() fs.FS {
	return embeddedFS()
}
