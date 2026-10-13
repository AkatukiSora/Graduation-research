package seed

import (
	"encoding/json"
	"errors"
	"math"
	"regexp"
	"strings"
	"testing"
	"testing/fstest"

	"github.com/NKC-CV2026/Graduation-research/backend/app/internal/seeddata"
)

const embeddedDatasetID = "synthetic-v1"

func loadEmbedded(t *testing.T) *Bundle {
	t.Helper()
	b, err := Load(seeddata.FS(), embeddedDatasetID)
	if err != nil {
		t.Fatalf("Load(%q): %v", embeddedDatasetID, err)
	}
	return b
}

// cloneBundle returns an independent copy whose digest fields are consistent,
// so a test can corrupt exactly one field and observe the matching rejection.
func cloneBundle(t *testing.T, b *Bundle) *Bundle {
	t.Helper()
	out := &Bundle{}
	for _, pair := range []struct{ from, to any }{{&b.Manifest, &out.Manifest}, {&b.Dataset, &out.Dataset}} {
		raw, err := json.Marshal(pair.from)
		if err != nil {
			t.Fatalf("marshal: %v", err)
		}
		if err := json.Unmarshal(raw, pair.to); err != nil {
			t.Fatalf("unmarshal: %v", err)
		}
	}
	digest := strings.Repeat("a", 64)
	out.ContentSHA256 = digest
	out.Manifest.ContentSHA256 = digest
	return out
}

func requireCode(t *testing.T, b *Bundle, want string) {
	t.Helper()
	err := Validate(b)
	if err == nil {
		t.Fatalf("Validate succeeded, want code %q", want)
	}
	var verr *ValidationError
	if !errors.As(err, &verr) {
		t.Fatalf("error %v is not a *ValidationError", err)
	}
	if verr.Code != want {
		t.Fatalf("code = %q (%v), want %q", verr.Code, err, want)
	}
}

func TestEmbeddedSyntheticDatasetIsValid(t *testing.T) {
	b := loadEmbedded(t)
	if err := Validate(b); err != nil {
		t.Fatalf("Validate: %v", err)
	}
	if !b.Manifest.Synthetic || b.Manifest.ProductionApproved {
		t.Fatalf("manifest must be synthetic and not production approved: %+v", b.Manifest)
	}
	if got, want := len(b.Dataset.Points), b.Manifest.Counts.Points; got != want || got != 12 {
		t.Fatalf("points = %d, manifest %d, want 12", got, want)
	}
	if got, want := len(b.Dataset.Candidates), b.Manifest.Counts.Candidates; got != want || got == 0 {
		t.Fatalf("candidates = %d, manifest %d, want > 0", got, want)
	}
	for _, p := range b.Dataset.Points {
		if p.DirectionDegrees != nil || p.DirectionStatus != "unknown" || p.ValidityStatus != "unknown" {
			t.Fatalf("point %s must have unknown direction/validity: %+v", p.EntityID, p)
		}
	}
	if b.ContentSHA256 != b.Manifest.ContentSHA256 {
		t.Fatalf("digest %s != manifest %s", b.ContentSHA256, b.Manifest.ContentSHA256)
	}
}

func TestValidateRejectsTamperedEmbeddedBytes(t *testing.T) {
	fsys := seeddata.FS()
	dataset, err := readAll(fsys, embeddedDatasetID+"/dataset.json")
	if err != nil {
		t.Fatal(err)
	}
	manifest, err := readAll(fsys, embeddedDatasetID+"/manifest.json")
	if err != nil {
		t.Fatal(err)
	}
	tampered := strings.Replace(string(dataset), `"lat":1.0004,`, `"lat":1.0005,`, 1)
	if tampered == string(dataset) {
		t.Fatal("fixture marker not found; update the test")
	}
	b, err := Load(fstest.MapFS{
		embeddedDatasetID + "/dataset.json":  {Data: []byte(tampered)},
		embeddedDatasetID + "/manifest.json": {Data: manifest},
	}, embeddedDatasetID)
	if err != nil {
		t.Fatalf("Load tampered: %v", err)
	}
	requireCode(t, b, CodeHash)
}

func TestLoadRejectsUnknownFieldsAndUnsafeIDs(t *testing.T) {
	fsys := seeddata.FS()
	dataset, _ := readAll(fsys, embeddedDatasetID+"/dataset.json")
	manifest, _ := readAll(fsys, embeddedDatasetID+"/manifest.json")
	extra := strings.Replace(string(dataset), `{"schema_version":1,`, `{"schema_version":1,"surprise":true,`, 1)
	if _, err := Load(fstest.MapFS{
		embeddedDatasetID + "/dataset.json":  {Data: []byte(extra)},
		embeddedDatasetID + "/manifest.json": {Data: manifest},
	}, embeddedDatasetID); err == nil {
		t.Fatal("Load accepted an unknown field")
	}
	for _, id := range []string{"", "../synthetic-v1", "a/b", "UPPER", ".hidden", strings.Repeat("a", 65), "missing-dataset"} {
		if _, err := Load(fsys, id); err == nil {
			t.Fatalf("Load(%q) succeeded", id)
		}
	}
}

func TestDatasetIDsListsEmbeddedDatasets(t *testing.T) {
	ids, err := DatasetIDs(seeddata.FS())
	if err != nil {
		t.Fatal(err)
	}
	if len(ids) != 1 || ids[0] != embeddedDatasetID {
		t.Fatalf("ids = %v", ids)
	}
}

func TestValidateRejectsOneCorruptedFieldAtATime(t *testing.T) {
	base := loadEmbedded(t)
	if err := Validate(cloneBundle(t, base)); err != nil {
		t.Fatalf("sealed clone must be valid: %v", err)
	}
	// Index of a candidate with a known source/neighbour pair and rank 1.
	cases := []struct {
		name   string
		code   string
		mutate func(b *Bundle)
	}{
		{"hash mismatch", CodeHash, func(b *Bundle) { b.Manifest.ContentSHA256 = strings.Repeat("b", 64) }},
		{"malformed digest", CodeHash, func(b *Bundle) { b.Manifest.ContentSHA256 = "xyz"; b.ContentSHA256 = "xyz" }},
		{"schema version", CodeSchemaVersion, func(b *Bundle) { b.Manifest.SchemaVersion = 2 }},
		{"dataset schema version", CodeSchemaVersion, func(b *Bundle) { b.Dataset.SchemaVersion = 2 }},
		{"manifest dataset id differs", CodeDatasetID, func(b *Bundle) { b.Manifest.DatasetID = "other-dataset" }},
		{"unsafe dataset id", CodeDatasetID, func(b *Bundle) { b.Manifest.DatasetID = "Bad/ID"; b.Dataset.DatasetID = "Bad/ID" }},
		{"unknown dataset file", CodeDatasetID, func(b *Bundle) { b.Manifest.DatasetFile = "other.json" }},
		{"bad source month", CodeMonth, func(b *Bundle) { b.Manifest.SourceMonth = "202613" }},
		{"empty region", CodeRegion, func(b *Bundle) { b.Manifest.RegionID = "" }},
		{"point count", CodeCount, func(b *Bundle) { b.Manifest.Counts.Points++ }},
		{"candidate count", CodeCount, func(b *Bundle) { b.Manifest.Counts.Candidates-- }},
		{"rejected rows present", CodeCount, func(b *Bundle) { b.Manifest.Counts.Rejected = 1 }},
		{"outside region rows present", CodeCount, func(b *Bundle) { b.Manifest.Counts.OutsideRegion = 1 }},
		{"production approved", CodeApproval, func(b *Bundle) { b.Manifest.ProductionApproved = true }},
		{"not synthetic", CodeSynthetic, func(b *Bundle) { b.Manifest.Synthetic = false }},
		{"rights status", CodeProvenance, func(b *Bundle) {
			b.Manifest.Provenance = json.RawMessage(`{"rights_status":"unknown"}`)
		}},
		{"missing provenance", CodeProvenance, func(b *Bundle) { b.Manifest.Provenance = nil }},
		{"longitude above range", CodeCoordinate, func(b *Bundle) { b.Dataset.Points[0].Lon = 180.5 }},
		{"latitude below range", CodeCoordinate, func(b *Bundle) { b.Dataset.Points[0].Lat = -90.5 }},
		{"NaN longitude", CodeCoordinate, func(b *Bundle) { b.Dataset.Points[0].Lon = math.NaN() }},
		{"infinite latitude", CodeCoordinate, func(b *Bundle) { b.Dataset.Points[0].Lat = math.Inf(1) }},
		{"direction degrees set", CodeDirection, func(b *Bundle) { v := 90.0; b.Dataset.Points[0].DirectionDegrees = &v }},
		{"direction confirmed", CodeDirection, func(b *Bundle) { b.Dataset.Points[0].DirectionStatus = "confirmed" }},
		{"direction inferred", CodeDirection, func(b *Bundle) { b.Dataset.Points[0].DirectionStatus = "inferred" }},
		{"validity valid", CodeValidity, func(b *Bundle) { b.Dataset.Points[0].ValidityStatus = "valid" }},
		{"empty normalization status", CodeNormalization, func(b *Bundle) { b.Dataset.Points[0].NormalizationStatus = "" }},
		{"over-long uid", CodeUID, func(b *Bundle) { b.Dataset.Points[0].RawUID = strings.Repeat("U", 33) }},
		{"empty uid", CodeUID, func(b *Bundle) { b.Dataset.Points[0].RawUID = "" }},
		{"duplicate entity id", CodeDuplicate, func(b *Bundle) { b.Dataset.Points[1].EntityID = b.Dataset.Points[0].EntityID }},
		{"duplicate uid", CodeDuplicate, func(b *Bundle) { b.Dataset.Points[1].RawUID = b.Dataset.Points[0].RawUID }},
		{"dangling neighbour", CodeCandidateRef, func(b *Bundle) { b.Dataset.Candidates[0].NeighborEntityID = "missing-point" }},
		{"dangling source", CodeCandidateRef, func(b *Bundle) { b.Dataset.Candidates[0].SourceEntityID = "missing-point" }},
		{"self candidate", CodeSelfCandidate, func(b *Bundle) {
			b.Dataset.Candidates[0].NeighborEntityID = b.Dataset.Candidates[0].SourceEntityID
		}},
		{"rank zero", CodeRank, func(b *Bundle) { b.Dataset.Candidates[0].Rank = 0 }},
		{"rank gap", CodeRank, func(b *Bundle) { b.Dataset.Candidates[0].Rank = 99 }},
		{"duplicate neighbour for source", CodeDuplicate, func(b *Bundle) {
			b.Dataset.Candidates[1].NeighborEntityID = b.Dataset.Candidates[0].NeighborEntityID
		}},
		{"NaN distance", CodeDistance, func(b *Bundle) { b.Dataset.Candidates[0].DistanceM = math.NaN() }},
		{"negative distance", CodeDistance, func(b *Bundle) { b.Dataset.Candidates[0].DistanceM = -1 }},
		{"bearing 360", CodeBearing, func(b *Bundle) { b.Dataset.Candidates[0].CandidateBearing = 360 }},
		{"negative bearing", CodeBearing, func(b *Bundle) { b.Dataset.Candidates[0].CandidateBearing = -0.5 }},
		{"NaN bearing", CodeBearing, func(b *Bundle) { b.Dataset.Candidates[0].CandidateBearing = math.NaN() }},
		{"bearing semantics", CodeSemantics, func(b *Bundle) { b.Dataset.Candidates[0].BearingSemantics = "official_direction" }},
		{"features not an object", CodeFeatures, func(b *Bundle) { b.Dataset.Candidates[0].Features = json.RawMessage(`[1]`) }},
		{"features missing", CodeFeatures, func(b *Bundle) { b.Dataset.Candidates[0].Features = nil }},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			b := cloneBundle(t, base)
			tc.mutate(b)
			requireCode(t, b, tc.code)
		})
	}
}

func TestValidateRequiresContiguousRanksPerSource(t *testing.T) {
	base := loadEmbedded(t)
	b := cloneBundle(t, base)
	// Find a source with at least two candidates and swap their ranks: still contiguous.
	first := b.Dataset.Candidates[0]
	for i := 1; i < len(b.Dataset.Candidates); i++ {
		c := &b.Dataset.Candidates[i]
		if c.SourceEntityID == first.SourceEntityID {
			b.Dataset.Candidates[0].Rank, c.Rank = c.Rank, b.Dataset.Candidates[0].Rank
			if err := Validate(b); err != nil {
				t.Fatalf("a permutation of contiguous ranks must stay valid: %v", err)
			}
			return
		}
	}
	t.Fatal("no source with two candidates in the embedded dataset")
}

func TestLoadRejectsDirectoryIDThatDiffersFromDeclaredID(t *testing.T) {
	fsys := seeddata.FS()
	dataset, _ := readAll(fsys, embeddedDatasetID+"/dataset.json")
	manifest, _ := readAll(fsys, embeddedDatasetID+"/manifest.json")
	renamed := regexp.MustCompile(`"dataset_id"\s*:\s*"`+embeddedDatasetID+`"`).ReplaceAllString(string(manifest), `"dataset_id": "other-id"`)
	if renamed == string(manifest) {
		t.Fatal("test setup: manifest dataset_id was not replaced")
	}
	_, err := Load(fstest.MapFS{
		embeddedDatasetID + "/dataset.json":  {Data: dataset},
		embeddedDatasetID + "/manifest.json": {Data: []byte(renamed)},
	}, embeddedDatasetID)
	var verr *ValidationError
	if !errors.As(err, &verr) || verr.Code != CodeDatasetID {
		t.Fatalf("err = %v, want dataset_id validation error", err)
	}
}
