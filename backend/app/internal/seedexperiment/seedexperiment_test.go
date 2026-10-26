package seedexperiment

import (
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"math"
	"strings"
	"testing"
	"testing/fstest"
)

func TestLoadEmbeddedExperimentalDataset(t *testing.T) {
	bundle, err := Load(FS(), DatasetID)
	if err != nil {
		t.Fatal(err)
	}
	if bundle.Manifest.Rows != 2333 || len(bundle.Rows) != 2333 || bundle.ContentSHA256 != "02eeb6998b5e926844448b4260d29c95f5b9a8a1b4216397789cbf1973493ebe" {
		t.Fatalf("unexpected loaded dataset: manifest rows=%d actual=%d digest=%s", bundle.Manifest.Rows, len(bundle.Rows), bundle.ContentSHA256)
	}
	if bundle.Manifest.Counts.Teacher != (SubsetCounts{Rows: 394, Inferred: 388, Unknown: 6}) ||
		bundle.Manifest.Counts.Target != (SubsetCounts{Rows: 1939, Inferred: 1937, Unknown: 2}) {
		t.Fatalf("unexpected counts: %+v", bundle.Manifest.Counts)
	}
}

func TestLoadRejectsUnknownDatasetID(t *testing.T) {
	if _, err := Load(FS(), "synthetic-v1"); err == nil || !strings.Contains(err.Error(), "unknown experimental direction dataset") {
		t.Fatalf("error = %v", err)
	}
}

func TestLoadRejectsTamperedDigestAndUnknownFields(t *testing.T) {
	base := FS()
	manifest, err := readBounded(base, DatasetID+"/manifest.json", maxManifestBytes)
	if err != nil {
		t.Fatal(err)
	}
	data, err := readBounded(base, DatasetID+"/experimental-directions.jsonl", maxDataBytes)
	if err != nil {
		t.Fatal(err)
	}
	badDigest := append([]byte(nil), data...)
	badDigest[0] ^= 1
	fsys := fstest.MapFS{
		DatasetID + "/manifest.json":                 &fstest.MapFile{Data: manifest},
		DatasetID + "/experimental-directions.jsonl": &fstest.MapFile{Data: badDigest},
	}
	if _, err := Load(fsys, DatasetID); err == nil || !strings.Contains(err.Error(), "content_hash") {
		t.Fatalf("tampered digest error = %v", err)
	}

	badRow := strings.Replace(string(data), `"source":"external_local_stop_approach"`, `"source":"external_local_stop_approach","unexpected":true`, 1)
	// Keep the manifest digest consistent so strict decoding is the rejecting check.
	var m map[string]any
	if err := json.Unmarshal(manifest, &m); err != nil {
		t.Fatal(err)
	}
	m["data_sha256"] = sha256Hex([]byte(badRow))
	updatedManifest, err := json.Marshal(m)
	if err != nil {
		t.Fatal(err)
	}
	fsys[DatasetID+"/manifest.json"] = &fstest.MapFile{Data: updatedManifest}
	fsys[DatasetID+"/experimental-directions.jsonl"] = &fstest.MapFile{Data: []byte(badRow)}
	if _, err := Load(fsys, DatasetID); err == nil || !strings.Contains(err.Error(), "unknown field") {
		t.Fatalf("unknown JSON field error = %v", err)
	}
}

func TestLoadRejectsMissingFalseApprovalField(t *testing.T) {
	base := FS()
	manifest, err := readBounded(base, DatasetID+"/manifest.json", maxManifestBytes)
	if err != nil {
		t.Fatal(err)
	}
	data, err := readBounded(base, DatasetID+"/experimental-directions.jsonl", maxDataBytes)
	if err != nil {
		t.Fatal(err)
	}
	var m map[string]json.RawMessage
	if err := json.Unmarshal(manifest, &m); err != nil {
		t.Fatal(err)
	}
	delete(m, "production_approved")
	updated, err := json.Marshal(m)
	if err != nil {
		t.Fatal(err)
	}
	fsys := fstest.MapFS{
		DatasetID + "/manifest.json":                 &fstest.MapFile{Data: updated},
		DatasetID + "/experimental-directions.jsonl": &fstest.MapFile{Data: data},
	}
	if _, err := Load(fsys, DatasetID); err == nil || !strings.Contains(err.Error(), `missing required field "production_approved"`) {
		t.Fatalf("missing safety field error = %v", err)
	}
}

func TestValidateRejectsInvalidRows(t *testing.T) {
	base, err := Load(FS(), DatasetID)
	if err != nil {
		t.Fatal(err)
	}
	one := func(t *testing.T) *Bundle {
		t.Helper()
		copyBundle := *base
		copyBundle.Rows = append([]Row(nil), base.Rows...)
		return &copyBundle
	}
	tests := []struct {
		name   string
		mutate func(*Bundle)
	}{
		{"duplicate entity", func(b *Bundle) { b.Rows[1].EntityID = b.Rows[0].EntityID }},
		{"duplicate uid", func(b *Bundle) { b.Rows[1].RawUID = b.Rows[0].RawUID }},
		{"invalid latitude", func(b *Bundle) { b.Rows[0].Lat = 91 }},
		{"infinite longitude", func(b *Bundle) { b.Rows[0].Lon = math.Inf(1) }},
		{"direction outside range", func(b *Bundle) { value := 360.0; b.Rows[0].DirectionDegrees = &value }},
		{"direction NaN", func(b *Bundle) { value := math.NaN(); b.Rows[0].DirectionDegrees = &value }},
		{"confidence provided", func(b *Bundle) { value := 0.5; b.Rows[0].Confidence = &value }},
		{"wrong source", func(b *Bundle) { b.Rows[0].Source = "osm_fusion" }},
		{"unknown reason absent", func(b *Bundle) {
			for i := range b.Rows {
				if b.Rows[i].DirectionDegrees == nil {
					b.Rows[i].Error = nil
					return
				}
			}
		}},
		{"inferred error set", func(b *Bundle) { message := "unexpected"; b.Rows[0].Error = &message }},
		{"target before teacher", func(b *Bundle) { b.Rows[0], b.Rows[394] = b.Rows[394], b.Rows[0] }},
		{"approval enabled", func(b *Bundle) { b.Manifest.ProductionApproved = true }},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			bundle := one(t)
			test.mutate(bundle)
			if err := Validate(bundle); err == nil {
				t.Fatal("Validate succeeded for an invalid bundle")
			}
		})
	}
}

func TestValidateRejectsNonObjectProvenance(t *testing.T) {
	bundle, err := Load(FS(), DatasetID)
	if err != nil {
		t.Fatal(err)
	}
	bundle.Manifest.Provenance = json.RawMessage(`[]`)
	if err := Validate(bundle); err == nil || !strings.Contains(err.Error(), "provenance") {
		t.Fatalf("error = %v", err)
	}
}

func sha256Hex(data []byte) string {
	return fmt.Sprintf("%x", sha256.Sum256(data))
}
