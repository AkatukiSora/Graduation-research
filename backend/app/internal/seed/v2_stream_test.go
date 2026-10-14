package seed

import (
	"bytes"
	"compress/gzip"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"runtime"
	"strconv"
	"strings"
	"testing"
)

func TestV2NearLimitFixtureStreamsWithoutMaterializingArrays(t *testing.T) {
	bundle, err := Load(nearLimitV2Fixture(t, "stress-v2"), "stress-v2")
	if err != nil {
		t.Fatalf("Load near-limit fixture: %v", err)
	}
	if err := Validate(bundle); err != nil {
		t.Fatalf("Validate near-limit fixture: %v", err)
	}
	if bundle.v2.manifest.Counts.Points != 10000 || bundle.v2.manifest.Counts.Candidates != 60000 {
		t.Fatalf("near-limit counts = %d points / %d candidates", bundle.v2.manifest.Counts.Points, bundle.v2.manifest.Counts.Candidates)
	}
	if len(bundle.Dataset.Points) != 0 || len(bundle.Dataset.Candidates) != 0 {
		t.Fatalf("near-limit v2 bundle materialized arrays: %d points / %d candidates", len(bundle.Dataset.Points), len(bundle.Dataset.Candidates))
	}
	if runtime.GOOS == "linux" {
		highWaterKiB, err := processHighWaterRSSKiB()
		if err != nil {
			t.Fatal(err)
		}
		if highWaterKiB >= 512*1024 {
			t.Fatalf("near-limit fixture process high-water RSS is %d KiB, want <512 MiB", highWaterKiB)
		}
		t.Logf("near-limit fixture: 10,000 points / 60,000 candidates, process high-water RSS=%d KiB", highWaterKiB)
	}
}

func processHighWaterRSSKiB() (uint64, error) {
	status, err := os.ReadFile("/proc/self/status")
	if err != nil {
		return 0, err
	}
	for _, line := range strings.Split(string(status), "\n") {
		fields := strings.Fields(line)
		if len(fields) >= 2 && fields[0] == "VmHWM:" {
			return strconv.ParseUint(fields[1], 10, 64)
		}
	}
	return 0, fmt.Errorf("VmHWM is missing from /proc/self/status")
}

func TestV2MixedStateFixturePreflightsWithoutObjectArrays(t *testing.T) {
	fixture, ids := mixedV2Fixture(t, "test-mixed-v2")
	bundle, err := Load(fixture, "test-mixed-v2")
	if err != nil {
		t.Fatalf("Load mixed-state fixture: %v", err)
	}
	if err := Validate(bundle); err != nil {
		t.Fatalf("Validate mixed-state fixture: %v", err)
	}
	if len(bundle.Dataset.Points) != 0 || len(bundle.Dataset.Candidates) != 0 {
		t.Fatalf("v2 bundle materialized object arrays: points=%d candidates=%d", len(bundle.Dataset.Points), len(bundle.Dataset.Candidates))
	}
	counts := bundle.v2.manifest.Counts
	if counts.DirectionStatusCounts["unknown"] == 0 || counts.DirectionStatusCounts["inferred"] != 2 || counts.DirectionStatusCounts["confirmed"] != 1 {
		t.Fatalf("direction state counts do not cover mixed states: %+v", counts.DirectionStatusCounts)
	}
	if counts.ValidityStatusCounts["unknown"] == 0 || counts.ValidityStatusCounts["valid"] != 1 || counts.ValidityStatusCounts["invalid"] != 1 {
		t.Fatalf("validity state counts do not cover mixed states: %+v", counts.ValidityStatusCounts)
	}
	if ids.sourceGeometry == "" || ids.nearest == "" || ids.confirmed == "" || ids.valid == "" || ids.invalid == "" {
		t.Fatalf("fixture omitted expected state ids: %+v", ids)
	}
}

func TestV2RejectsNewApprovalField(t *testing.T) {
	fixture, _ := mixedV2Fixture(t, "test-approval-field")
	path := "test-approval-field/manifest.json"
	var object map[string]json.RawMessage
	if err := json.Unmarshal(fixture[path].Data, &object); err != nil {
		t.Fatal(err)
	}
	object["production_approved"] = json.RawMessage("false")
	fixture[path].Data, _ = json.Marshal(object)
	if _, err := Load(fixture, "test-approval-field"); err == nil || !strings.Contains(err.Error(), "production_approved") {
		t.Fatalf("Load accepted the removed approval field: %v", err)
	}
}

func TestV2RejectsNearestSemanticsTamper(t *testing.T) {
	fixture, _ := mixedV2Fixture(t, "test-semantics-tamper")
	path := "test-semantics-tamper/points-00001.jsonl.gz"
	compressed := fixture[path].Data
	reader, err := gzip.NewReader(bytes.NewReader(compressed))
	if err != nil {
		t.Fatal(err)
	}
	uncompressed, err := io.ReadAll(reader)
	if err != nil {
		t.Fatal(err)
	}
	if err := reader.Close(); err != nil {
		t.Fatal(err)
	}
	old := []byte(`"direction_semantics":"stop_to_neighbor_proxy"`)
	newValue := []byte(`"direction_semantics":"candidate_point_neighbor_only"`)
	if !bytes.Contains(uncompressed, old) {
		t.Fatal("fixture lacks nearest-stop evidence semantics")
	}
	uncompressed = bytes.Replace(uncompressed, old, newValue, 1)
	fixture[path].Data = gzipTestBytes(t, uncompressed)
	if _, err := Load(fixture, "test-semantics-tamper"); err == nil || !strings.Contains(err.Error(), "semantics") {
		t.Fatalf("Load accepted nearest semantics tampering: %v", err)
	}
}

func TestV2RejectsGzipCRCFailure(t *testing.T) {
	fixture, _ := mixedV2Fixture(t, "test-crc-failure")
	path := "test-crc-failure/points-00001.jsonl.gz"
	data := append([]byte(nil), fixture[path].Data...)
	data[len(data)-8] ^= 0x01
	fixture[path].Data = data
	if _, err := Load(fixture, "test-crc-failure"); err == nil {
		t.Fatal("Load accepted a gzip stream with a corrupted CRC")
	}
}

func TestCanonicalManifestRejectsNonportableValues(t *testing.T) {
	cases := []string{
		`{"n":-0}`,
		`{"n":9007199254740992}`,
		`{"n":1.0}`,
		`{"s":"line\u2028separator"}`,
		`{"s":"\uD800"}`,
	}
	for _, input := range cases {
		if _, err := canonicalJSON([]byte(input)); err == nil {
			t.Errorf("canonicalJSON accepted %s", input)
		}
	}
	if _, err := canonicalJSON([]byte(`{"ok":"日本語<&>"}`)); err != nil {
		t.Fatalf("canonicalJSON rejected a portable Unicode string: %v", err)
	}
}
