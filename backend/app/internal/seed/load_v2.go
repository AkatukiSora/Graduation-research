package seed

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io/fs"
	"path"
	"strconv"
)

var (
	v2CountsKeys     = []string{"points", "candidates", "rejected", "outside_region", "rows_total", "excluded_non_stop", "direction_status_counts", "validity_status_counts", "imputation_method_counts", "imputation_reason_counts", "validity_reason_counts"}
	v2GeometryKeys   = []string{"region_id", "center_lat", "center_lon", "center_source", "radius_m", "sphere_radius_m", "vertices", "coordinate_decimal_places", "algorithm", "boundary_inclusion", "boundary_sha256"}
	v2InputsKeys     = []string{"source_zip_sha256", "source_csv_sha256", "source_csv_bytes", "source_header_sha256", "boundary_sha256", "input_manifest_sha256", "enrichment_sha256"}
	v2GeneratorKeys  = []string{"code_ref", "code_sha256", "method_implementation_sha256", "method_version", "source_selection_order", "commit", "uv_lock_sha256", "python_version", "dependency_versions", "zlib_version", "kernel", "max_candidates", "candidate_radius_m", "thread_policy"}
	v2ProvenanceKeys = []string{"dataset", "publisher", "edition", "source_url", "terms_reference", "terms_sha256", "rights_status", "attribution", "modification_notice", "identity_scope", "source_identity_note"}
)

func loadV2(fsys fs.FS, datasetID string, manifestPath string, manifestBytes []byte) (*Bundle, error) {
	var manifest v2Manifest
	if err := decodeV2Strict(manifestBytes, &manifest, v2ManifestKeys); err != nil {
		return nil, fmt.Errorf("decode v2 manifest of %q: %w", datasetID, err)
	}
	if err := validateV2ManifestKeys(manifestBytes); err != nil {
		return nil, fmt.Errorf("decode v2 manifest of %q: %w", datasetID, err)
	}
	canonicalInput, err := removeManifestDigest(manifestBytes)
	if err != nil {
		return nil, fmt.Errorf("canonicalize manifest of %q: %w", datasetID, err)
	}
	canonical, err := canonicalJSON(canonicalInput)
	if err != nil {
		return nil, fmt.Errorf("canonicalize manifest of %q: %w", datasetID, err)
	}
	digest := sha256.Sum256(canonical)
	canonicalDigest := hex.EncodeToString(digest[:])
	if err := validateV2Manifest(&manifest, canonicalDigest); err != nil {
		return nil, fmt.Errorf("validate v2 manifest of %q: %w", datasetID, err)
	}
	if manifest.DatasetID != datasetID {
		return nil, invalid(CodeDatasetID, "directory id %q differs from manifest id %q", datasetID, manifest.DatasetID)
	}
	root := path.Dir(manifestPath)
	source := &v2Source{fsys: fsys, root: root, manifest: manifest}
	bundle := &Bundle{
		Manifest: Manifest{
			SchemaVersion: manifest.SchemaVersion, DatasetID: manifest.DatasetID, SourceMonth: manifest.SourceMonth,
			RegionID: manifest.RegionID, Synthetic: manifest.Synthetic, ContentSHA256: manifest.ContentSHA256,
			Counts:     Counts{Points: manifest.Counts.Points, Candidates: manifest.Counts.Candidates, Rejected: manifest.Counts.Rejected, OutsideRegion: manifest.Counts.OutsideRegion},
			Provenance: mustJSONRaw(manifest.Provenance),
		},
		Dataset:       Dataset{SchemaVersion: manifest.SchemaVersion, DatasetID: manifest.DatasetID},
		ContentSHA256: manifest.ContentSHA256,
		v2:            source,
	}
	index, err := scanV2(backgroundContext(), source)
	if err != nil {
		return nil, fmt.Errorf("preflight v2 dataset %q: %w", datasetID, err)
	}
	if err := compareV2Counts(manifest.Counts, index.stateCounts); err != nil {
		return nil, fmt.Errorf("preflight v2 dataset %q: %w", datasetID, err)
	}
	return bundle, nil
}

func validateV2ManifestKeys(raw []byte) error {
	var top map[string]json.RawMessage
	if err := json.Unmarshal(raw, &top); err != nil {
		return err
	}
	checks := []struct {
		key  string
		keys []string
	}{
		{"counts", v2CountsKeys}, {"geometry", v2GeometryKeys}, {"inputs", v2InputsKeys},
		{"generator", v2GeneratorKeys}, {"provenance", v2ProvenanceKeys},
	}
	for _, item := range checks {
		if err := requireExactJSONKeys(top[item.key], item.keys); err != nil {
			return fmt.Errorf("%s: %w", item.key, err)
		}
	}
	var files []json.RawMessage
	if err := json.Unmarshal(top["files"], &files); err != nil {
		return fmt.Errorf("files: %w", err)
	}
	for i, file := range files {
		var descriptor struct {
			Kind string `json:"kind"`
		}
		if err := json.Unmarshal(file, &descriptor); err != nil {
			return fmt.Errorf("files[%d]: %w", i, err)
		}
		keys := v2ShardFileKeys
		if descriptor.Kind == "evidence" {
			keys = v2EvidenceFileKeys
		}
		if err := requireExactJSONKeys(file, keys); err != nil {
			return fmt.Errorf("files[%d]: %w", i, err)
		}
	}
	return validateV2ManifestNumberTypes(raw)
}

func validateV2ManifestNumberTypes(raw []byte) error {
	var top map[string]json.RawMessage
	if err := json.Unmarshal(raw, &top); err != nil {
		return err
	}
	checks := []struct {
		parent string
		fields []string
	}{
		{"", []string{"schema_version"}},
		{"counts", []string{"points", "candidates", "rejected", "outside_region", "rows_total", "excluded_non_stop"}},
		{"geometry", []string{"radius_m", "vertices", "coordinate_decimal_places"}},
		{"inputs", []string{"source_csv_bytes"}},
		{"generator", []string{"max_candidates", "candidate_radius_m"}},
	}
	for _, check := range checks {
		object := top
		if check.parent != "" {
			if err := json.Unmarshal(top[check.parent], &object); err != nil {
				return fmt.Errorf("%s: %w", check.parent, err)
			}
		}
		for _, field := range check.fields {
			if err := requireV2Integer(object[field]); err != nil {
				return fmt.Errorf("%s.%s: %w", check.parent, field, err)
			}
		}
	}
	var counts map[string]json.RawMessage
	if err := json.Unmarshal(top["counts"], &counts); err != nil {
		return fmt.Errorf("counts: %w", err)
	}
	for _, key := range []string{"direction_status_counts", "validity_status_counts", "imputation_method_counts", "imputation_reason_counts", "validity_reason_counts"} {
		var values map[string]json.RawMessage
		if err := json.Unmarshal(counts[key], &values); err != nil {
			return fmt.Errorf("counts.%s: %w", key, err)
		}
		for name, value := range values {
			if err := requireV2Integer(value); err != nil {
				return fmt.Errorf("counts.%s.%s: %w", key, name, err)
			}
		}
	}
	var files []map[string]json.RawMessage
	if err := json.Unmarshal(top["files"], &files); err != nil {
		return fmt.Errorf("files: %w", err)
	}
	for i, file := range files {
		var descriptor struct {
			Kind string `json:"kind"`
		}
		if err := json.Unmarshal(file["kind"], &descriptor.Kind); err != nil {
			return fmt.Errorf("files[%d].kind: %w", i, err)
		}
		fields := []string{"rows"}
		if descriptor.Kind == "evidence" {
			fields = append(fields, "bytes")
		} else {
			fields = append(fields, "compressed_bytes", "uncompressed_bytes")
		}
		for _, field := range fields {
			if err := requireV2Integer(file[field]); err != nil {
				return fmt.Errorf("files[%d].%s: %w", i, field, err)
			}
		}
	}
	return nil
}

func requireV2Integer(raw json.RawMessage) error {
	value := string(bytes.TrimSpace(raw))
	if !integerPattern.MatchString(value) || value == "-0" {
		return fmt.Errorf("must be a non-null JSON integer")
	}
	if _, err := strconv.ParseInt(value, 10, 64); err != nil {
		return fmt.Errorf("must be a non-null JSON integer: %w", err)
	}
	return nil
}

var (
	v2ShardFileKeys    = []string{"kind", "basename", "rows", "compressed_bytes", "compressed_sha256", "uncompressed_bytes", "uncompressed_sha256"}
	v2EvidenceFileKeys = []string{"kind", "basename", "rows", "bytes", "sha256"}
)

func requireExactJSONKeys(raw json.RawMessage, expected []string) error {
	var object map[string]json.RawMessage
	if err := json.Unmarshal(raw, &object); err != nil {
		return err
	}
	if len(object) != len(expected) {
		return fmt.Errorf("object has %d keys, want %d", len(object), len(expected))
	}
	for _, key := range expected {
		if _, ok := object[key]; !ok {
			return fmt.Errorf("required key %q is missing", key)
		}
	}
	return nil
}

func removeManifestDigest(raw []byte) ([]byte, error) {
	var object map[string]json.RawMessage
	if err := json.Unmarshal(raw, &object); err != nil {
		return nil, err
	}
	delete(object, "content_sha256")
	return json.Marshal(object)
}

func mustJSONRaw(value any) json.RawMessage {
	raw, _ := json.Marshal(value)
	return raw
}

// backgroundContext avoids importing context into the schema-only loader file.
func backgroundContext() context.Context { return context.Background() }
