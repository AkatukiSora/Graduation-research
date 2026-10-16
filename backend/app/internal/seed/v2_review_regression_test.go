package seed

import (
	"bufio"
	"bytes"
	"compress/gzip"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"math"
	"reflect"
	"strings"
	"testing"
	"testing/fstest"
)

func cloneV2MapFS(source fstest.MapFS) fstest.MapFS {
	clone := make(fstest.MapFS, len(source))
	for name, file := range source {
		clone[name] = &fstest.MapFile{Data: append([]byte(nil), file.Data...), Mode: file.Mode, ModTime: file.ModTime}
	}
	return clone
}

func rewriteV2ShardRows(t *testing.T, source fstest.MapFS, datasetID, kind string, mutate func(map[string]json.RawMessage) bool) fstest.MapFS {
	t.Helper()
	updated := cloneV2MapFS(source)
	manifestPath := datasetID + "/manifest.json"
	var manifest v2Manifest
	if err := json.Unmarshal(updated[manifestPath].Data, &manifest); err != nil {
		t.Fatal(err)
	}
	foundArtifact, changed := false, false
	for i := range manifest.Files {
		artifact := &manifest.Files[i]
		if artifact.Kind != kind || kind == "evidence" {
			continue
		}
		foundArtifact = true
		name := datasetID + "/" + artifact.Basename
		reader, err := gzip.NewReader(bytes.NewReader(updated[name].Data))
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
		var rewritten bytes.Buffer
		scanner := bufio.NewScanner(bytes.NewReader(uncompressed))
		scanner.Buffer(make([]byte, 0, 64<<10), maxJSONLLineBytes)
		for scanner.Scan() {
			line := append([]byte(nil), scanner.Bytes()...)
			var row map[string]json.RawMessage
			if err := json.Unmarshal(line, &row); err != nil {
				t.Fatal(err)
			}
			if mutate(row) {
				changed = true
				line, err = json.Marshal(row)
				if err != nil {
					t.Fatal(err)
				}
			}
			rewritten.Write(line)
			rewritten.WriteByte('\n')
		}
		if err := scanner.Err(); err != nil {
			t.Fatal(err)
		}
		compressed := gzipTestBytes(t, rewritten.Bytes())
		updated[name].Data = compressed
		artifact.CompressedBytes = int64(len(compressed))
		artifact.CompressedSHA256 = sha256Hex(compressed)
		artifact.UncompressedBytes = int64(rewritten.Len())
		artifact.UncompressedSHA256 = sha256Hex(rewritten.Bytes())
	}
	if !foundArtifact || !changed {
		t.Fatalf("did not modify a %q shard row", kind)
	}
	updated[manifestPath].Data = mustManifestBytes(t, &manifest)
	return updated
}

func rewriteV2EvidenceArtifact(t *testing.T, source fstest.MapFS, datasetID, basename string, mutate func(map[string]json.RawMessage)) fstest.MapFS {
	t.Helper()
	updated := cloneV2MapFS(source)
	manifestPath := datasetID + "/manifest.json"
	var manifest v2Manifest
	if err := json.Unmarshal(updated[manifestPath].Data, &manifest); err != nil {
		t.Fatal(err)
	}
	var artifactSHA string
	for i := range manifest.Files {
		artifact := &manifest.Files[i]
		if artifact.Basename != basename || artifact.Kind != "evidence" {
			continue
		}
		path := datasetID + "/" + basename
		var record map[string]json.RawMessage
		if err := json.Unmarshal(updated[path].Data, &record); err != nil {
			t.Fatal(err)
		}
		mutate(record)
		payload, err := json.Marshal(record)
		if err != nil {
			t.Fatal(err)
		}
		artifactSHA = artifact.SHA256
		artifact.Bytes = int64(len(payload))
		artifact.SHA256 = sha256Hex(payload)
		updated[path].Data = payload
		break
	}
	if artifactSHA == "" {
		t.Fatalf("evidence artifact %q not found", basename)
	}
	newManifest := mustManifestBytes(t, &manifest)
	updated[manifestPath].Data = newManifest
	var newSHA string
	for _, artifact := range manifest.Files {
		if artifact.Basename == basename {
			newSHA = artifact.SHA256
			break
		}
	}
	return rewriteV2ShardRows(t, updated, datasetID, "points", func(row map[string]json.RawMessage) bool {
		changed := false
		for _, field := range []string{"direction_evidence", "validity_evidence"} {
			var root map[string]json.RawMessage
			if err := json.Unmarshal(row[field], &root); err != nil || len(root) == 0 {
				continue
			}
			var referencedSHA string
			if err := json.Unmarshal(root["artifact_sha256"], &referencedSHA); err != nil || referencedSHA != artifactSHA {
				continue
			}
			root["artifact_sha256"], _ = json.Marshal(newSHA)
			row[field], _ = json.Marshal(root)
			changed = true
		}
		return changed
	})
}

func rewriteV2ManifestRaw(t *testing.T, source fstest.MapFS, datasetID string, mutate func(map[string]json.RawMessage)) fstest.MapFS {
	t.Helper()
	updated := cloneV2MapFS(source)
	manifestPath := datasetID + "/manifest.json"
	var object map[string]json.RawMessage
	if err := json.Unmarshal(updated[manifestPath].Data, &object); err != nil {
		t.Fatal(err)
	}
	mutate(object)
	withoutDigest, err := json.Marshal(object)
	if err != nil {
		t.Fatal(err)
	}
	canonicalInput, err := removeManifestDigest(withoutDigest)
	if err != nil {
		t.Fatal(err)
	}
	canonical, err := canonicalJSON(canonicalInput)
	if err != nil {
		t.Fatal(err)
	}
	digest := sha256.Sum256(canonical)
	object["content_sha256"], err = json.Marshal(hex.EncodeToString(digest[:]))
	if err != nil {
		t.Fatal(err)
	}
	updated[manifestPath].Data, err = json.MarshalIndent(object, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	updated[manifestPath].Data = append(updated[manifestPath].Data, '\n')
	return updated
}

func TestV2EvidenceArtifactsAreRequiredAndRootReferencesAreChecked(t *testing.T) {
	const datasetID = "test-evidence-boundary"
	fixture, _ := mixedV2Fixture(t, datasetID)
	if _, err := Load(fixture, datasetID); err != nil {
		t.Fatalf("valid evidence fixture rejected: %v", err)
	}

	t.Run("missing artifact", func(t *testing.T) {
		broken := cloneV2MapFS(fixture)
		delete(broken, datasetID+"/evidence-00001.json")
		if _, err := Load(broken, datasetID); err == nil {
			t.Fatal("Load accepted a missing referenced evidence artifact")
		}
	})

	t.Run("artifact digest mismatch", func(t *testing.T) {
		broken := cloneV2MapFS(fixture)
		path := datasetID + "/evidence-00001.json"
		broken[path].Data = append(broken[path].Data, ' ')
		if _, err := Load(broken, datasetID); err == nil {
			t.Fatal("Load accepted an evidence artifact with a mismatched digest")
		}
	})

	t.Run("unknown root record reference", func(t *testing.T) {
		broken := rewriteV2ShardRows(t, fixture, datasetID, "points", func(row map[string]json.RawMessage) bool {
			var evidence map[string]json.RawMessage
			if err := json.Unmarshal(row["direction_evidence"], &evidence); err != nil || len(evidence) == 0 {
				return false
			}
			evidence["record_ref"], _ = json.Marshal("missing-record-ref")
			row["direction_evidence"], _ = json.Marshal(evidence)
			return true
		})
		if _, err := Load(broken, datasetID); err == nil {
			t.Fatal("Load accepted a root evidence reference absent from its artifact")
		}
	})
}

func TestV2RejectsSourceGeometryEvidenceForDifferentSourceCSV(t *testing.T) {
	const datasetID = "test-source-geometry-source-csv"
	fixture, _ := mixedV2Fixture(t, datasetID)
	broken := rewriteV2ShardRows(t, fixture, datasetID, "points", func(row map[string]json.RawMessage) bool {
		var evidence map[string]json.RawMessage
		if err := json.Unmarshal(row["direction_evidence"], &evidence); err != nil || len(evidence) == 0 {
			return false
		}
		var kind string
		if err := json.Unmarshal(evidence["kind"], &kind); err != nil || kind != "source_geometry" {
			return false
		}
		evidence["source_csv_sha256"], _ = json.Marshal(strings.Repeat("c", 64))
		row["direction_evidence"], _ = json.Marshal(evidence)
		return true
	})
	if _, err := Load(broken, datasetID); err == nil {
		t.Fatal("Load accepted source-geometry evidence bound to a different source CSV SHA")
	}
}

func TestV2RejectsTamperedEvidenceContentWithConsistentDigests(t *testing.T) {
	const datasetID = "test-evidence-content"
	cases := []struct {
		name     string
		basename string
		field    string
		value    string
	}{
		{"entity-scope", "evidence-00001.json", "entity_id", "other-entity"},
		{"month-scope", "evidence-00001.json", "source_month", "202501"},
		{"source-csv-scope", "evidence-00001.json", "source_csv_sha256", strings.Repeat("c", 64)},
		{"observation-time", "evidence-00001.json", "observed_at", "not-a-timestamp"},
		{"validity-review-digest", "evidence-00002.json", "validity_review_sha256", "not-a-sha256"},
		{"validity-effective-time", "evidence-00002.json", "effective_at", "not-a-date"},
	}
	for _, test := range cases {
		t.Run(test.name, func(t *testing.T) {
			fixture, _ := mixedV2Fixture(t, datasetID)
			broken := rewriteV2EvidenceArtifact(t, fixture, datasetID, test.basename, func(record map[string]json.RawMessage) {
				record[test.field], _ = json.Marshal(test.value)
			})
			if _, err := Load(broken, datasetID); err == nil {
				t.Fatalf("Load accepted tampered evidence field %q with refreshed artifact and root hashes", test.field)
			}
		})
	}
}

func TestV2RejectsNullRequiredNumbersAndInvalidCandidateFeatures(t *testing.T) {
	const datasetID = "test-v2-strict-values"
	pointFields := []string{"lon", "lat"}
	for _, field := range pointFields {
		t.Run("point-"+field, func(t *testing.T) {
			fixture, _ := mixedV2Fixture(t, datasetID)
			broken := rewriteV2ShardRows(t, fixture, datasetID, "points", func(row map[string]json.RawMessage) bool {
				if _, ok := row[field]; !ok {
					return false
				}
				row[field] = json.RawMessage("null")
				return true
			})
			if _, err := Load(broken, datasetID); err == nil {
				t.Fatalf("Load accepted null required point field %q", field)
			}
		})
	}
	for _, field := range []string{"lon", "lat"} {
		t.Run("source-geometry-entrance-"+field, func(t *testing.T) {
			fixture, _ := mixedV2Fixture(t, datasetID)
			broken := rewriteV2ShardRows(t, fixture, datasetID, "points", func(row map[string]json.RawMessage) bool {
				var evidence map[string]json.RawMessage
				if err := json.Unmarshal(row["direction_evidence"], &evidence); err != nil || len(evidence) == 0 {
					return false
				}
				var entrance map[string]json.RawMessage
				if err := json.Unmarshal(evidence["entrance"], &entrance); err != nil || len(entrance) == 0 {
					return false
				}
				entrance[field] = json.RawMessage("null")
				evidence["entrance"], _ = json.Marshal(entrance)
				row["direction_evidence"], _ = json.Marshal(evidence)
				return true
			})
			if _, err := Load(broken, datasetID); err == nil {
				t.Fatalf("Load accepted null source-geometry entrance %s", field)
			}
		})
	}
	t.Run("inferred-direction-degrees", func(t *testing.T) {
		fixture, _ := mixedV2Fixture(t, datasetID)
		broken := rewriteV2ShardRows(t, fixture, datasetID, "points", func(row map[string]json.RawMessage) bool {
			var status string
			if err := json.Unmarshal(row["direction_status"], &status); err != nil || status != "inferred" {
				return false
			}
			row["direction_degrees"] = json.RawMessage("null")
			return true
		})
		if _, err := Load(broken, datasetID); err == nil {
			t.Fatal("Load accepted null direction_degrees for an inferred point")
		}
	})

	for _, field := range []string{"rank", "distance_m", "candidate_bearing"} {
		t.Run("candidate-"+field, func(t *testing.T) {
			fixture, _ := mixedV2Fixture(t, datasetID)
			broken := rewriteV2ShardRows(t, fixture, datasetID, "candidates", func(row map[string]json.RawMessage) bool {
				if _, ok := row[field]; !ok {
					return false
				}
				row[field] = json.RawMessage("null")
				return true
			})
			if _, err := Load(broken, datasetID); err == nil {
				t.Fatalf("Load accepted null required candidate field %q", field)
			}
		})
	}

	candidateCases := []struct {
		name     string
		features string
	}{
		{"unknown-feature", `{"private_path":"/tmp/internal"}`},
		{"free-text-value", `{"p_d1":"operator note"}`},
		{"quoted-zero", `{"p_d1":"0"}`},
		{"quoted-decimal", `{"p_d1":"1.25"}`},
		{"null-value", `{"p_d1":null}`},
		{"non-finite-number", `{"p_d1":1e999}`},
	}
	for _, test := range candidateCases {
		t.Run(test.name, func(t *testing.T) {
			fixture, _ := mixedV2Fixture(t, datasetID)
			broken := rewriteV2ShardRows(t, fixture, datasetID, "candidates", func(row map[string]json.RawMessage) bool {
				row["features"] = json.RawMessage(test.features)
				return true
			})
			if _, err := Load(broken, datasetID); err == nil {
				t.Fatalf("Load accepted invalid candidate features %s", test.features)
			}
		})
	}
}

func TestV2ManifestRejectsNullOptionalCountDespiteCanonicalDigest(t *testing.T) {
	const datasetID = "test-v2-null-count"
	fixture, _ := mixedV2Fixture(t, datasetID)
	broken := rewriteV2ManifestRaw(t, fixture, datasetID, func(object map[string]json.RawMessage) {
		var counts map[string]json.RawMessage
		if err := json.Unmarshal(object["counts"], &counts); err != nil {
			t.Fatal(err)
		}
		counts["rejected"] = json.RawMessage("null")
		object["counts"], _ = json.Marshal(counts)
	})
	if _, err := Load(broken, datasetID); err == nil || !strings.Contains(err.Error(), "integer") {
		t.Fatalf("Load did not reject a null manifest number before semantic checks: %v", err)
	}
}

func TestV2OptionalDataPurposeIsStrict(t *testing.T) {
	for _, test := range []struct {
		name string
		raw  string
		bad  bool
	}{
		{"omitted legacy marker", `{}`, false},
		{"rule test marker", `{"data_purpose":"rule_test"}`, false},
		{"null marker", `{"data_purpose":null}`, true},
		{"empty marker", `{"data_purpose":""}`, true},
		{"unknown marker", `{"data_purpose":"other"}`, true},
	} {
		t.Run(test.name, func(t *testing.T) {
			if err := validateOptionalV2DataPurpose([]byte(test.raw)); (err != nil) != test.bad {
				t.Fatalf("validateOptionalV2DataPurpose(%s) error = %v, bad = %t", test.raw, err, test.bad)
			}
		})
	}
	if err := requireOptionalJSONKeys([]byte(`{"required":"ok","unexpected":true}`), []string{"required"}, []string{"data_purpose"}); err == nil {
		t.Fatal("optional-key validation accepted an unknown provenance key")
	}
	if err := rejectDuplicateJSONKeys([]byte(`{"data_purpose":"rule_test","data_purpose":"rule_test"}`)); err == nil {
		t.Fatal("strict JSON validation accepted a duplicate data_purpose key")
	}
}

func TestV2RuleTestModeKeepsSyntheticAndValidityRestrictions(t *testing.T) {
	sourceCSV := strings.Repeat("a", 64)
	purpose := ruleTestDataPurpose
	manifest := &v2Manifest{
		Synthetic:  true,
		Inputs:     v2Inputs{SourceCSVSHA256: sourceCSV},
		Provenance: v2Provenance{DataPurpose: &purpose},
	}
	degree := 90.0
	point := Point{
		DirectionDegrees: &degree, DirectionStatus: "inferred", ImputationMethod: "nearest_stop_reference",
		ImputationReason: "topology_proxy_unverified", DirectionSource: "nearest_stop",
		DirectionEvidence: json.RawMessage(`{"schema_version":2,"kind":"nearest_stop_reference","source_csv_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","method_version":"nearest-stop-proxy-v1","direction_semantics":"stop_to_neighbor_proxy","neighbor_entity_id":"neighbor","candidate_rank":1}`),
		ValidityStatus:    "unknown", ValidityReason: "source_validity_unverified", ValidityEvidence: json.RawMessage(`{}`),
	}
	if err := validateV2Direction(&point, manifest, nil, nil); err != nil {
		t.Fatalf("rule_test nearest inference was rejected: %v", err)
	}
	if err := validateV2Validity(&point, manifest, nil, nil); err != nil {
		t.Fatalf("rule_test unknown validity was rejected: %v", err)
	}

	legacy := *manifest
	legacy.Provenance.DataPurpose = nil
	if err := validateV2Direction(&point, &legacy, nil, nil); err == nil {
		t.Fatal("legacy synthetic data accepted an inferred direction")
	}
	invalidValidity := point
	invalidValidity.ValidityStatus = "valid"
	if err := validateV2Validity(&invalidValidity, manifest, nil, nil); err == nil {
		t.Fatal("rule_test accepted known validity")
	}
}

func TestV2RuleTestNearestStatesVerifyRank1AgainstAllPoints(t *testing.T) {
	const sourceID, neighborID, fartherID = "stop-a", "stop-b", "stop-c"
	const lat0 = 35.0
	cosLat := math.Cos(lat0 * math.Pi / 180)
	points := map[string]v2PointIndex{
		sourceID:   {EntityID: sourceID, Lon: 139, Lat: lat0, DirectionStatus: "inferred", ImputationMethod: "nearest_stop_reference", ImputationReason: "topology_proxy_unverified", DirectionSource: "nearest_stop", HasDirection: true, NearestNeighborID: neighborID},
		neighborID: {EntityID: neighborID, Lon: 139.0001, Lat: lat0, DirectionStatus: "unknown", ImputationReason: "unsupported_entrance_geometry"},
		fartherID:  {EntityID: fartherID, Lon: 139.0003, Lat: lat0, DirectionStatus: "unknown", ImputationReason: "unsupported_entrance_geometry"},
	}
	sourceXY := project(points[sourceID].Lon, points[sourceID].Lat, cosLat)
	neighborXY := project(points[neighborID].Lon, points[neighborID].Lat, cosLat)
	degree := projectionBearing(sourceXY[0], sourceXY[1], neighborXY)
	source := points[sourceID]
	source.DirectionDegrees = degree
	points[sourceID] = source
	candidate := v2CandidateIndex{
		SourceEntityID: sourceID, NeighborEntityID: neighborID, Rank: 1,
		DistanceM: math.Hypot(neighborXY[0]-sourceXY[0], neighborXY[1]-sourceXY[1]), CandidateBearing: degree,
	}
	if err := validateRuleTestNearestStates(points, map[string]v2CandidateIndex{sourceID: candidate}, strings.Repeat("a", 64), lat0); err != nil {
		t.Fatalf("self-consistent rank-1 nearest evidence was rejected: %v", err)
	}

	fartherXY := project(points[fartherID].Lon, points[fartherID].Lat, cosLat)
	candidate.NeighborEntityID = fartherID
	candidate.DistanceM = math.Hypot(fartherXY[0]-sourceXY[0], fartherXY[1]-sourceXY[1])
	candidate.CandidateBearing = projectionBearing(sourceXY[0], sourceXY[1], fartherXY)
	source.NearestNeighborID = fartherID
	points[sourceID] = source
	if err := validateRuleTestNearestStates(points, map[string]v2CandidateIndex{sourceID: candidate}, strings.Repeat("a", 64), lat0); err == nil {
		t.Fatal("rule_test accepted a rank-1 candidate that is not the nearest point")
	}
}

func TestV2RejectsDirectionEvidenceSourceCSVHashMismatch(t *testing.T) {
	const datasetID = "test-direction-source-csv-scope"
	fixture, _ := mixedV2Fixture(t, datasetID)
	var manifest v2Manifest
	if err := json.Unmarshal(fixture[datasetID+"/manifest.json"].Data, &manifest); err != nil {
		t.Fatal(err)
	}
	wrongDigest := strings.Repeat("0", 64)
	if wrongDigest == manifest.Inputs.SourceCSVSHA256 {
		wrongDigest = strings.Repeat("1", 64)
	}
	for _, test := range []struct {
		name string
		kind string
	}{
		{"source-geometry", "source_geometry"},
		{"nearest-stop", "nearest_stop_reference"},
	} {
		t.Run(test.name, func(t *testing.T) {
			broken := rewriteV2ShardRows(t, fixture, datasetID, "points", func(row map[string]json.RawMessage) bool {
				var evidence map[string]json.RawMessage
				if err := json.Unmarshal(row["direction_evidence"], &evidence); err != nil || len(evidence) == 0 {
					return false
				}
				var kind string
				if err := json.Unmarshal(evidence["kind"], &kind); err != nil || kind != test.kind {
					return false
				}
				evidence["source_csv_sha256"], _ = json.Marshal(wrongDigest)
				row["direction_evidence"], _ = json.Marshal(evidence)
				return true
			})
			if _, err := Load(broken, datasetID); err == nil {
				t.Fatalf("Load accepted %s evidence with a source CSV digest that differs from the manifest", test.kind)
			}
		})
	}
}

func TestV2RejectsNearestProjectionZeroDistanceWithConsistentHashes(t *testing.T) {
	const datasetID = "test-nearest-zero-projection"
	fixture, ids := mixedV2Fixture(t, datasetID)
	points := readV2PointsForReview(t, fixture, datasetID)
	source := points[ids.nearest]
	var evidence struct {
		Neighbor string `json:"neighbor_entity_id"`
	}
	if err := json.Unmarshal(source.DirectionEvidence, &evidence); err != nil {
		t.Fatal(err)
	}
	neighbor := points[evidence.Neighbor]

	broken := rewriteV2ShardRows(t, fixture, datasetID, "points", func(row map[string]json.RawMessage) bool {
		var id string
		_ = json.Unmarshal(row["entity_id"], &id)
		if id != ids.nearest {
			return false
		}
		row["lon"], _ = json.Marshal(neighbor.Lon)
		row["lat"], _ = json.Marshal(neighbor.Lat)
		row["direction_degrees"] = json.RawMessage("0")
		return true
	})
	broken = rewriteV2ShardRows(t, broken, datasetID, "candidates", func(row map[string]json.RawMessage) bool {
		var sourceID string
		var rank int
		_ = json.Unmarshal(row["source_entity_id"], &sourceID)
		_ = json.Unmarshal(row["rank"], &rank)
		if sourceID != ids.nearest || rank != 1 {
			return false
		}
		row["distance_m"] = json.RawMessage("0")
		row["candidate_bearing"] = json.RawMessage("0")
		return true
	})
	_, err := Load(broken, datasetID)
	if err == nil || !strings.Contains(err.Error(), "nearest projection is zero length") {
		t.Fatalf("Load did not reject the self-consistent zero-distance nearest evidence specifically: %v", err)
	}
}

func TestV2RejectsNearestRankOneTieWithConsistentHashes(t *testing.T) {
	const datasetID = "test-nearest-rank-one-tie"
	fixture, ids := mixedV2Fixture(t, datasetID)
	points := readV2PointsForReview(t, fixture, datasetID)
	source := points[ids.nearest]
	var evidence struct {
		Neighbor string `json:"neighbor_entity_id"`
	}
	if err := json.Unmarshal(source.DirectionEvidence, &evidence); err != nil {
		t.Fatal(err)
	}
	neighbor := points[evidence.Neighbor]
	tieID := ""
	for id, point := range points {
		if id != ids.nearest && id != ids.sourceGeometry && id != ids.confirmed && id != ids.valid && id != ids.invalid &&
			point.DirectionStatus == "unknown" {
			tieID = id
			break
		}
	}
	if tieID == "" {
		t.Fatal("fixture has no unrelated unknown point for a nearest tie")
	}
	broken := rewriteV2ShardRows(t, fixture, datasetID, "points", func(row map[string]json.RawMessage) bool {
		var id string
		_ = json.Unmarshal(row["entity_id"], &id)
		if id != tieID {
			return false
		}
		row["lon"], _ = json.Marshal(2*source.Lon - neighbor.Lon)
		row["lat"], _ = json.Marshal(2*source.Lat - neighbor.Lat)
		return true
	})
	_, err := Load(broken, datasetID)
	if err == nil || !strings.Contains(err.Error(), "nearest projection has a tied rank-1 neighbor") {
		t.Fatalf("Load did not reject the self-consistent rank-1 tie specifically: %v", err)
	}
}

func readV2PointsForReview(t *testing.T, fixture fstest.MapFS, datasetID string) map[string]Point {
	t.Helper()
	var manifest v2Manifest
	if err := json.Unmarshal(fixture[datasetID+"/manifest.json"].Data, &manifest); err != nil {
		t.Fatal(err)
	}
	var pointsFile string
	for _, file := range manifest.Files {
		if file.Kind == "points" {
			pointsFile = datasetID + "/" + file.Basename
			break
		}
	}
	if pointsFile == "" {
		t.Fatal("fixture has no point shard")
	}
	reader, err := gzip.NewReader(bytes.NewReader(fixture[pointsFile].Data))
	if err != nil {
		t.Fatal(err)
	}
	defer reader.Close()
	points := map[string]Point{}
	scanner := bufio.NewScanner(reader)
	scanner.Buffer(make([]byte, 0, 64<<10), maxJSONLLineBytes)
	for scanner.Scan() {
		var point Point
		if err := json.Unmarshal(scanner.Bytes(), &point); err != nil {
			t.Fatal(err)
		}
		points[point.EntityID] = point
	}
	if err := scanner.Err(); err != nil {
		t.Fatal(err)
	}
	return points
}

func TestV2AcceptsFiniteJSONNumberFeatures(t *testing.T) {
	const datasetID = "test-v2-json-number-features"
	fixture, _ := mixedV2Fixture(t, datasetID)
	valid := rewriteV2ShardRows(t, fixture, datasetID, "candidates", func(row map[string]json.RawMessage) bool {
		row["features"] = json.RawMessage(`{"p_d1":0,"p_d2":1.25,"p_d3":-2,"p_n12":1e2}`)
		return true
	})
	if _, err := Load(valid, datasetID); err != nil {
		t.Fatalf("Load rejected finite JSON-number features: %v", err)
	}
}

func TestV2ValidationIndexTypesExcludeFullRowsAndEvidence(t *testing.T) {
	pointType := reflect.TypeOf(v2PointIndex{})
	candidateType := reflect.TypeOf(v2CandidateIndex{})
	for _, name := range []string{"DirectionEvidence", "ValidityEvidence", "Raw", "Features"} {
		if _, ok := pointType.FieldByName(name); ok {
			t.Errorf("v2PointIndex retains full-row field %q", name)
		}
		if _, ok := candidateType.FieldByName(name); ok {
			t.Errorf("v2CandidateIndex retains full-row field %q", name)
		}
	}
	if pointType == reflect.TypeOf(Point{}) || candidateType == reflect.TypeOf(Candidate{}) {
		t.Fatal("v2 index must retain dedicated lightweight tuple types")
	}
}
