package seed

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestV2RejectsUnregisteredOSMInference(t *testing.T) {
	const datasetID = "test-unregistered-osm"
	fixture, _ := mixedV2Fixture(t, datasetID)
	var manifest v2Manifest
	if err := json.Unmarshal(fixture[datasetID+"/manifest.json"].Data, &manifest); err != nil {
		t.Fatal(err)
	}
	rootEvidence, err := json.Marshal(map[string]any{
		"schema_version":        2,
		"kind":                  "model_inference",
		"source_csv_sha256":     manifest.Inputs.SourceCSVSHA256,
		"method_version":        "osm-fusion-reconstructed-rf-v1",
		"direction_semantics":   "vehicle_approach_to_stop_estimate",
		"artifact_sha256":       strings.Repeat("a", 64),
		"record_ref":            "unregistered-record-1",
		"model_version":         "unregistered-model-v1",
		"feature_parent_sha256": strings.Repeat("b", 64),
	})
	if err != nil {
		t.Fatal(err)
	}
	broken := rewriteV2ShardRows(t, fixture, datasetID, "points", func(row map[string]json.RawMessage) bool {
		var status string
		if err := json.Unmarshal(row["direction_status"], &status); err != nil || status != "unknown" {
			return false
		}
		row["direction_degrees"] = json.RawMessage("123.0")
		row["direction_status"], _ = json.Marshal("inferred")
		row["imputation_method"], _ = json.Marshal("osm_fusion_cautious")
		row["imputation_reason"], _ = json.Marshal("model_direction_inference")
		row["direction_source"], _ = json.Marshal("osm_fusion")
		row["direction_evidence"] = rootEvidence
		return true
	})
	if _, err := Load(broken, datasetID); err == nil || !strings.Contains(err.Error(), "OSM model registry is empty") {
		t.Fatalf("Load did not reject an unregistered OSM inference through the empty registry: %v", err)
	}
}
