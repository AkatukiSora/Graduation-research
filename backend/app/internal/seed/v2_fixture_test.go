package seed

import (
	"bufio"
	"bytes"
	"compress/gzip"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"io/fs"
	"math"
	"sort"
	"strings"
	"testing"
	"testing/fstest"
	"time"

	"github.com/NKC-CV2026/Graduation-research/backend/app/internal/seeddata"
)

type stagedCandidateFS struct {
	validFS     fs.FS
	lateFS      fs.FS
	target      string
	targetOpens int
	switchOn    int
	onLateOpen  func()
}

func (s *stagedCandidateFS) Open(name string) (fs.File, error) {
	if name == s.target {
		s.targetOpens++
		switchOn := s.switchOn
		if switchOn == 0 {
			switchOn = 3
		}
		if s.targetOpens >= switchOn {
			if s.onLateOpen != nil {
				s.onLateOpen()
			}
			return s.lateFS.Open(name)
		}
	}
	return s.validFS.Open(name)
}

func lateCandidateFailureFS(t *testing.T, valid fstest.MapFS, datasetID string) *stagedCandidateFS {
	t.Helper()
	target := datasetID + "/candidates-00001.jsonl.gz"
	reader, err := gzip.NewReader(bytes.NewReader(valid[target].Data))
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
	old, replacement := []byte(`"rank":1`), []byte(`"rank":0`)
	if !bytes.Contains(uncompressed, old) {
		t.Fatal("fixture has no rank-1 candidate")
	}
	late := fstest.MapFS{}
	for name, file := range valid {
		late[name] = &fstest.MapFile{Data: append([]byte(nil), file.Data...)}
	}
	late[target].Data = gzipTestBytes(t, bytes.Replace(uncompressed, old, replacement, 1))
	return &stagedCandidateFS{validFS: valid, lateFS: late, target: target}
}

func lateCandidateCRCFailureFS(t *testing.T, valid fstest.MapFS, datasetID string) *stagedCandidateFS {
	t.Helper()
	target := datasetID + "/candidates-00001.jsonl.gz"
	late := fstest.MapFS{}
	for name, file := range valid {
		late[name] = &fstest.MapFile{Data: append([]byte(nil), file.Data...)}
	}
	compressed := late[target].Data
	if len(compressed) < 8 {
		t.Fatal("candidate shard is too short for a gzip trailer")
	}
	compressed[len(compressed)-8] ^= 0x01
	return &stagedCandidateFS{validFS: valid, lateFS: late, target: target}
}

func lateCandidateDBFailureFS(t *testing.T, valid fstest.MapFS, datasetID string) *stagedCandidateFS {
	t.Helper()
	target := datasetID + "/candidates-00001.jsonl.gz"
	reader, err := gzip.NewReader(bytes.NewReader(valid[target].Data))
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
	lastLineEnd := bytes.LastIndexByte(uncompressed, '\n')
	lastLineStart := bytes.LastIndexByte(uncompressed[:lastLineEnd], '\n') + 1
	lastLine := uncompressed[lastLineStart:lastLineEnd]
	old, replacement := []byte(`"neighbor_entity_id":"e00005"`), []byte(`"neighbor_entity_id":"missing-point"`)
	changed := bytes.Replace(lastLine, old, replacement, 1)
	if bytes.Equal(changed, lastLine) {
		t.Fatal("last candidate row does not contain the expected neighbor")
	}
	updated := append([]byte(nil), uncompressed[:lastLineStart]...)
	updated = append(updated, changed...)
	updated = append(updated, uncompressed[lastLineEnd:]...)
	late := fstest.MapFS{}
	for name, file := range valid {
		late[name] = &fstest.MapFile{Data: append([]byte(nil), file.Data...)}
	}
	late[target].Data = gzipTestBytes(t, updated)
	return &stagedCandidateFS{validFS: valid, lateFS: late, target: target, switchOn: 2}
}

type mixedFixtureIDs struct {
	sourceGeometry string
	nearest        string
	confirmed      string
	valid          string
	invalid        string
}

// mixedV2Fixture creates a test-only, non-synthetic bundle in memory. It is
// never embedded or distributed; it exercises state/evidence persistence
// without treating synthetic data as real evidence.
func mixedV2Fixture(t *testing.T, datasetID string) (fstest.MapFS, mixedFixtureIDs) {
	t.Helper()
	base := seeddata.FS()
	manifestBytes, err := fs.ReadFile(base, "synthetic-v2/manifest.json")
	if err != nil {
		t.Fatal(err)
	}
	var manifest v2Manifest
	if err := json.Unmarshal(manifestBytes, &manifest); err != nil {
		t.Fatal(err)
	}
	manifest.DatasetID = datasetID
	manifest.Synthetic = false
	zipDigest, termsDigest := strings.Repeat("a", 64), strings.Repeat("b", 64)
	manifest.Inputs.SourceZIPSHA256 = &zipDigest
	manifest.Provenance.SourceURL = "test-only://not-distributed"
	manifest.Provenance.TermsReference = "test-only"
	manifest.Provenance.TermsSHA256 = termsDigest
	manifest.Provenance.RightsStatus = "test-only"

	var points []Point
	var candidates []Candidate
	for _, artifact := range manifest.Files {
		compressed, err := fs.ReadFile(base, "synthetic-v2/"+artifact.Basename)
		if err != nil {
			t.Fatal(err)
		}
		reader, err := gzip.NewReader(bytes.NewReader(compressed))
		if err != nil {
			t.Fatal(err)
		}
		scanner := bufio.NewScanner(reader)
		scanner.Buffer(make([]byte, 0, 64<<10), maxJSONLLineBytes)
		for scanner.Scan() {
			if artifact.Kind == "points" {
				var point Point
				if err := json.Unmarshal(scanner.Bytes(), &point); err != nil {
					t.Fatal(err)
				}
				points = append(points, point)
			} else {
				var candidate Candidate
				if err := json.Unmarshal(scanner.Bytes(), &candidate); err != nil {
					t.Fatal(err)
				}
				candidates = append(candidates, candidate)
			}
		}
		if err := scanner.Err(); err != nil {
			t.Fatal(err)
		}
		if err := reader.Close(); err != nil {
			t.Fatal(err)
		}
	}
	sort.Slice(points, func(i, j int) bool { return points[i].EntityID < points[j].EntityID })
	sort.Slice(candidates, func(i, j int) bool {
		if candidates[i].SourceEntityID != candidates[j].SourceEntityID {
			return candidates[i].SourceEntityID < candidates[j].SourceEntityID
		}
		return candidates[i].Rank < candidates[j].Rank
	})
	if len(points) < 5 {
		t.Fatalf("fixture has only %d points", len(points))
	}
	pointByID := make(map[string]Point, len(points))
	for _, point := range points {
		pointByID[point.EntityID] = point
	}

	ids := mixedFixtureIDs{sourceGeometry: points[0].EntityID}
	entranceLon, entranceLat := points[0].Lon-0.0001, points[0].Lat
	degree := sphericalBearing(entranceLon, entranceLat, points[0].Lon, points[0].Lat)
	points[0].DirectionDegrees = &degree
	points[0].DirectionStatus = "inferred"
	points[0].ImputationMethod = "source_entrance_geometry"
	points[0].ImputationReason = "source_geometry_derived"
	points[0].DirectionSource = "jartic_entrance"
	points[0].DirectionEvidence = evidenceJSON(t, struct {
		SchemaVersion   int    `json:"schema_version"`
		Kind            string `json:"kind"`
		SourceCSVSHA256 string `json:"source_csv_sha256"`
		MethodVersion   string `json:"method_version"`
		Semantics       string `json:"direction_semantics"`
		Entrance        struct {
			Lon float64 `json:"lon"`
			Lat float64 `json:"lat"`
		} `json:"entrance"`
	}{2, "source_geometry", manifest.Inputs.SourceCSVSHA256, "entrance-to-stop-sphere-v1", "vehicle_approach_to_stop_estimate", struct {
		Lon float64 `json:"lon"`
		Lat float64 `json:"lat"`
	}{entranceLon, entranceLat}})
	pointByID[points[0].EntityID] = points[0]

	lat0 := medianLatitude(pointByID)
	cosLat := math.Cos(lat0 * math.Pi / 180)
	for i := range candidates {
		candidate := &candidates[i]
		if candidate.Rank != 1 || candidate.SourceEntityID == points[0].EntityID {
			continue
		}
		source, ok := pointByID[candidate.SourceEntityID]
		if !ok {
			continue
		}
		sourceXY := project(source.Lon, source.Lat, cosLat)
		nearest, second := math.Inf(1), math.Inf(1)
		nearestID := ""
		for entityID, other := range pointByID {
			if entityID == source.EntityID {
				continue
			}
			otherXY := project(other.Lon, other.Lat, cosLat)
			distance := math.Hypot(otherXY[0]-sourceXY[0], otherXY[1]-sourceXY[1])
			if distance < nearest {
				second, nearest, nearestID = nearest, distance, entityID
			} else if distance < second {
				second = distance
			}
		}
		if nearestID != candidate.NeighborEntityID || nearest <= 1e-6 || second-nearest <= 1e-6 || nearest > 80 {
			continue
		}
		neighbor := pointByID[nearestID]
		neighborXY := project(neighbor.Lon, neighbor.Lat, cosLat)
		bearing := projectionBearing(sourceXY[0], sourceXY[1], neighborXY)
		candidate.DistanceM, candidate.CandidateBearing = nearest, bearing
		source.DirectionDegrees = &bearing
		source.DirectionStatus = "inferred"
		source.ImputationMethod = "nearest_stop_reference"
		source.ImputationReason = "topology_proxy_unverified"
		source.DirectionSource = "nearest_stop"
		source.DirectionEvidence = evidenceJSON(t, struct {
			SchemaVersion   int    `json:"schema_version"`
			Kind            string `json:"kind"`
			SourceCSVSHA256 string `json:"source_csv_sha256"`
			MethodVersion   string `json:"method_version"`
			Semantics       string `json:"direction_semantics"`
			Neighbor        string `json:"neighbor_entity_id"`
			Rank            int    `json:"candidate_rank"`
		}{2, "nearest_stop_reference", manifest.Inputs.SourceCSVSHA256, "nearest-stop-proxy-v1", "stop_to_neighbor_proxy", nearestID, 1})
		pointByID[source.EntityID] = source
		for pointIndex := range points {
			if points[pointIndex].EntityID == source.EntityID {
				points[pointIndex] = source
				break
			}
		}
		ids.nearest = source.EntityID
		break
	}
	if ids.nearest == "" {
		t.Fatal("could not construct a valid unique nearest-neighbor fixture")
	}

	confirmedIndex := -1
	for i := range points {
		if points[i].EntityID != points[0].EntityID && points[i].EntityID != ids.nearest {
			confirmedIndex = i
			break
		}
	}
	if confirmedIndex < 0 {
		t.Fatal("no point available for confirmed-state fixture")
	}
	confirmed := &points[confirmedIndex]
	ids.confirmed = confirmed.EntityID
	evidenceFiles := make([]v2File, 0, 3)
	evidencePayloads := make(map[string][]byte, 3)
	confirmedDegree := 123.456
	confirmed.DirectionDegrees = &confirmedDegree
	confirmed.DirectionStatus = "confirmed"
	confirmed.ImputationMethod = "field_observation"
	confirmed.ImputationReason = "none"
	confirmed.DirectionSource = "field_survey"
	confirmed.DirectionEvidence = evidenceJSON(t, struct {
		SchemaVersion     int    `json:"schema_version"`
		Kind              string `json:"kind"`
		EntityID          string `json:"entity_id"`
		SourceMonth       string `json:"source_month"`
		SourceCSVSHA256   string `json:"source_csv_sha256"`
		ArtifactSHA256    string `json:"artifact_sha256"`
		RecordRef         string `json:"record_ref"`
		MethodVersion     string `json:"method_version"`
		Semantics         string `json:"direction_semantics"`
		ObservationRef    string `json:"observation_ref"`
		ObservedAt        string `json:"observed_at"`
		ObservationSHA256 string `json:"observation_sha256"`
	}{1, "field_observation", confirmed.EntityID, manifest.SourceMonth, manifest.Inputs.SourceCSVSHA256, strings.Repeat("c", 64), "test-only:field-observation", "field-observation-test-v1", "vehicle_approach_to_stop", "test-only:observation", "2026-10-14T00:00:00Z", strings.Repeat("d", 64)})
	confirmed.ValidityStatus = "valid"
	confirmed.ValidityReason = "active_regulation_evidence"
	confirmed.ValidityEvidence = validityEvidence(t, manifest, confirmed.EntityID, "active")
	file, payload, rootEvidence := attachEvidenceArtifact(t, confirmed.DirectionEvidence, "evidence-00001.json")
	confirmed.DirectionEvidence = rootEvidence
	evidenceFiles, evidencePayloads[file.Basename] = append(evidenceFiles, file), payload
	file, payload, rootEvidence = attachEvidenceArtifact(t, confirmed.ValidityEvidence, "evidence-00002.json")
	confirmed.ValidityEvidence = rootEvidence
	evidenceFiles, evidencePayloads[file.Basename] = append(evidenceFiles, file), payload
	ids.valid = confirmed.EntityID

	invalidIndex := -1
	for i := range points {
		if points[i].EntityID != ids.sourceGeometry && points[i].EntityID != ids.nearest && points[i].EntityID != ids.confirmed {
			invalidIndex = i
			break
		}
	}
	if invalidIndex < 0 {
		t.Fatal("no point available for invalid-state fixture")
	}
	invalidPoint := &points[invalidIndex]
	ids.invalid = invalidPoint.EntityID
	invalidPoint.ValidityStatus = "invalid"
	invalidPoint.ValidityReason = "withdrawn_regulation_evidence"
	invalidPoint.ValidityEvidence = validityEvidence(t, manifest, invalidPoint.EntityID, "withdrawn")
	file, payload, rootEvidence = attachEvidenceArtifact(t, invalidPoint.ValidityEvidence, "evidence-00003.json")
	invalidPoint.ValidityEvidence = rootEvidence
	evidenceFiles, evidencePayloads[file.Basename] = append(evidenceFiles, file), payload

	for i := range points {
		if points[i].ValidityStatus == "unknown" {
			points[i].ValidityReason = "source_validity_unverified"
		}
		pointByID[points[i].EntityID] = points[i]
	}
	manifest.Counts.DirectionStatusCounts = zeroCountMap([]string{"unknown", "inferred", "confirmed"})
	manifest.Counts.ValidityStatusCounts = zeroCountMap([]string{"unknown", "valid", "invalid"})
	manifest.Counts.ImputationMethodCounts = zeroCountMap([]string{"none", "source_entrance_geometry", "nearest_stop_reference", "osm_fusion_cautious", "field_observation"})
	manifest.Counts.ImputationReasonCounts = zeroCountMap([]string{"none", "missing_entrance_coordinates", "unsupported_entrance_geometry", "topology_only_unverified", "source_identity_unverified", "legacy_unrecorded", "source_geometry_derived", "model_direction_inference", "topology_proxy_unverified", "no_neighbor_within_radius", "zero_length_geometry", "ambiguous_nearest_neighbor", "multiple_entrance_directions"})
	manifest.Counts.ValidityReasonCounts = zeroCountMap([]string{"source_validity_unverified", "active_regulation_evidence", "withdrawn_regulation_evidence", "synthetic_fixture", "legacy_unrecorded"})
	for _, point := range points {
		manifest.Counts.DirectionStatusCounts[point.DirectionStatus]++
		manifest.Counts.ValidityStatusCounts[point.ValidityStatus]++
		manifest.Counts.ImputationMethodCounts[point.ImputationMethod]++
		manifest.Counts.ImputationReasonCounts[point.ImputationReason]++
		manifest.Counts.ValidityReasonCounts[point.ValidityReason]++
	}

	files := make([]v2File, 0, 2+len(evidenceFiles))
	payloads := make(map[string][]byte, 2+len(evidenceFiles))
	for _, item := range []struct {
		kind  string
		rows  any
		count int
	}{{"candidates", candidates, len(candidates)}, {"points", points, len(points)}} {
		uncompressed := jsonlTestRows(t, item.rows)
		compressed := gzipTestBytes(t, uncompressed)
		name := item.kind + "-00001.jsonl.gz"
		files = append(files, v2File{Kind: item.kind, Basename: name, Rows: item.count,
			CompressedBytes: int64(len(compressed)), CompressedSHA256: sha256Hex(compressed),
			UncompressedBytes: int64(len(uncompressed)), UncompressedSHA256: sha256Hex(uncompressed)})
		payloads[name] = compressed
	}
	files = append(files, evidenceFiles...)
	for name, data := range evidencePayloads {
		payloads[name] = data
	}
	manifest.Files = files
	sort.Slice(manifest.Files, func(i, j int) bool {
		if manifest.Files[i].Kind != manifest.Files[j].Kind {
			return manifest.Files[i].Kind < manifest.Files[j].Kind
		}
		return manifest.Files[i].Basename < manifest.Files[j].Basename
	})
	manifestBytes = mustManifestBytes(t, &manifest)
	mapFS := fstest.MapFS{datasetID + "/manifest.json": &fstest.MapFile{Data: manifestBytes}}
	for name, data := range payloads {
		mapFS[datasetID+"/"+name] = &fstest.MapFile{Data: data}
	}
	return mapFS, ids
}

func nearLimitV2Fixture(t *testing.T, datasetID string) fstest.MapFS {
	return sizedV2Fixture(t, datasetID, 10000)
}

func sizedV2Fixture(t *testing.T, datasetID string, pointCount int) fstest.MapFS {
	t.Helper()
	if pointCount < 7 || pointCount > maxV2Points {
		t.Fatalf("invalid sized fixture point count %d", pointCount)
	}
	baseManifest, err := fs.ReadFile(seeddata.FS(), "synthetic-v2/manifest.json")
	if err != nil {
		t.Fatal(err)
	}
	var manifest v2Manifest
	if err := json.Unmarshal(baseManifest, &manifest); err != nil {
		t.Fatal(err)
	}
	manifest.DatasetID = datasetID
	manifest.Counts.Points, manifest.Counts.Candidates = pointCount, pointCount*6
	manifest.Counts.Rejected, manifest.Counts.OutsideRegion, manifest.Counts.ExcludedNonStop = 0, 0, 0
	manifest.Counts.RowsTotal = pointCount
	manifest.Counts.DirectionStatusCounts = zeroCountMap([]string{"unknown", "inferred", "confirmed"})
	manifest.Counts.DirectionStatusCounts["unknown"] = pointCount
	manifest.Counts.ValidityStatusCounts = zeroCountMap([]string{"unknown", "valid", "invalid"})
	manifest.Counts.ValidityStatusCounts["unknown"] = pointCount
	manifest.Counts.ImputationMethodCounts = zeroCountMap([]string{"none", "source_entrance_geometry", "nearest_stop_reference", "osm_fusion_cautious", "field_observation"})
	manifest.Counts.ImputationMethodCounts["none"] = pointCount
	manifest.Counts.ImputationReasonCounts = zeroCountMap([]string{"none", "missing_entrance_coordinates", "unsupported_entrance_geometry", "topology_only_unverified", "source_identity_unverified", "legacy_unrecorded", "source_geometry_derived", "model_direction_inference", "topology_proxy_unverified", "no_neighbor_within_radius", "zero_length_geometry", "ambiguous_nearest_neighbor", "multiple_entrance_directions"})
	manifest.Counts.ImputationReasonCounts["missing_entrance_coordinates"] = pointCount
	manifest.Counts.ValidityReasonCounts = zeroCountMap([]string{"source_validity_unverified", "active_regulation_evidence", "withdrawn_regulation_evidence", "synthetic_fixture", "legacy_unrecorded"})
	manifest.Counts.ValidityReasonCounts["synthetic_fixture"] = pointCount
	manifest.Provenance.Edition = datasetID

	points := make([]Point, pointCount)
	for i := range points {
		points[i] = Point{
			EntityID: fmt.Sprintf("e%05d", i), RawUID: fmt.Sprintf("u%031d", i),
			Lon: float64(i) * 0.000001, Lat: 35, DirectionStatus: "unknown",
			ImputationMethod: "none", ImputationReason: "missing_entrance_coordinates", DirectionSource: "unknown",
			DirectionEvidence: json.RawMessage(`{}`), ValidityStatus: "unknown", ValidityReason: "synthetic_fixture",
			ValidityEvidence: json.RawMessage(`{}`), NormalizationStatus: "valid_candidate_only",
		}
	}
	files := make([]v2File, 0, 12)
	payloads := make(map[string][]byte, 12)
	addShards := func(kind string, rows any, total, rowsPerShard int) {
		shardIndex := 0
		switch typed := rows.(type) {
		case []Point:
			for start := 0; start < total; start += rowsPerShard {
				end := min(start+rowsPerShard, total)
				addStressShard(t, kind, shardIndex, typed[start:end], &files, payloads)
				shardIndex++
			}
		case []Candidate:
			for start := 0; start < total; start += rowsPerShard {
				end := min(start+rowsPerShard, total)
				addStressShard(t, kind, shardIndex, typed[start:end], &files, payloads)
				shardIndex++
			}
		default:
			t.Fatalf("unsupported stress rows %T", rows)
		}
	}
	addShards("points", points, len(points), 7000)
	candidates := make([]Candidate, 0, pointCount*6)
	for source := 0; source < pointCount; source++ {
		for rank := 1; rank <= 6; rank++ {
			neighbor := (source + rank) % pointCount
			candidates = append(candidates, Candidate{
				SourceEntityID: fmt.Sprintf("e%05d", source), NeighborEntityID: fmt.Sprintf("e%05d", neighbor),
				Rank: rank, DistanceM: float64(rank), CandidateBearing: 90,
				Features: json.RawMessage(`{}`), BearingSemantics: BearingSemantics,
			})
		}
	}
	addShards("candidates", candidates, len(candidates), 10000)
	sort.Slice(files, func(i, j int) bool {
		if files[i].Kind != files[j].Kind {
			return files[i].Kind < files[j].Kind
		}
		return files[i].Basename < files[j].Basename
	})
	manifest.Files = files
	mapFS := fstest.MapFS{datasetID + "/manifest.json": &fstest.MapFile{Data: mustManifestBytes(t, &manifest)}}
	for name, data := range payloads {
		mapFS[datasetID+"/"+name] = &fstest.MapFile{Data: data}
	}
	return mapFS
}

func addStressShard(t *testing.T, kind string, fileIndex int, rows any, files *[]v2File, payloads map[string][]byte) {
	t.Helper()
	rowCount := 0
	switch typed := rows.(type) {
	case []Point:
		rowCount = len(typed)
	case []Candidate:
		rowCount = len(typed)
	}
	uncompressed := jsonlTestRows(t, rows)
	if len(uncompressed) > maxShardUncompressedSize {
		t.Fatalf("stress %s shard unexpectedly exceeds 4 MiB: %d", kind, len(uncompressed))
	}
	compressed := gzipTestBytes(t, uncompressed)
	name := fmt.Sprintf("%s-%05d.jsonl.gz", kind, fileIndex+1)
	*files = append(*files, v2File{Kind: kind, Basename: name, Rows: rowCount,
		CompressedBytes: int64(len(compressed)), CompressedSHA256: sha256Hex(compressed),
		UncompressedBytes: int64(len(uncompressed)), UncompressedSHA256: sha256Hex(uncompressed)})
	payloads[name] = compressed
}

func attachEvidenceArtifact(t *testing.T, root json.RawMessage, basename string) (v2File, []byte, json.RawMessage) {
	t.Helper()
	var record v2EvidenceRecord
	if err := json.Unmarshal(root, &record); err != nil {
		t.Fatal(err)
	}
	if record.RecordRef == "" {
		t.Fatal("root evidence has no record_ref")
	}
	payload, err := json.Marshal(record)
	if err != nil {
		t.Fatal(err)
	}
	artifactSHA := sha256Hex(payload)
	var rootObject map[string]json.RawMessage
	if err := json.Unmarshal(root, &rootObject); err != nil {
		t.Fatal(err)
	}
	rootObject["artifact_sha256"], err = json.Marshal(artifactSHA)
	if err != nil {
		t.Fatal(err)
	}
	updated, err := json.Marshal(rootObject)
	if err != nil {
		t.Fatal(err)
	}
	return v2File{Kind: "evidence", Basename: basename, Rows: 1, Bytes: int64(len(payload)), SHA256: artifactSHA}, payload, updated
}

func validityEvidence(t *testing.T, manifest v2Manifest, entityID, state string) json.RawMessage {
	t.Helper()
	return evidenceJSON(t, struct {
		SchemaVersion        int    `json:"schema_version"`
		Kind                 string `json:"kind"`
		EntityID             string `json:"entity_id"`
		SourceMonth          string `json:"source_month"`
		SourceCSVSHA256      string `json:"source_csv_sha256"`
		ArtifactSHA256       string `json:"artifact_sha256"`
		RecordRef            string `json:"record_ref"`
		MethodVersion        string `json:"method_version"`
		EffectiveAt          string `json:"effective_at"`
		ValidityReviewSHA256 string `json:"validity_review_sha256"`
	}{1, "regulation_validity", entityID, manifest.SourceMonth, manifest.Inputs.SourceCSVSHA256, strings.Repeat("e", 64), "test-only:validity-" + state, "validity-test-v1", "2026-10-14", strings.Repeat("f", 64)})
}

func evidenceJSON(t *testing.T, value any) json.RawMessage {
	t.Helper()
	data, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	return data
}

func jsonlTestRows(t *testing.T, value any) []byte {
	t.Helper()
	rows, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	var objects []json.RawMessage
	if err := json.Unmarshal(rows, &objects); err != nil {
		t.Fatal(err)
	}
	var output bytes.Buffer
	for _, row := range objects {
		canonical, err := canonicalAnyJSON(row)
		if err != nil {
			t.Fatal(err)
		}
		output.Write(canonical)
		output.WriteByte('\n')
	}
	return output.Bytes()
}

func gzipTestBytes(t *testing.T, data []byte) []byte {
	t.Helper()
	var output bytes.Buffer
	writer, err := gzip.NewWriterLevel(&output, 9)
	if err != nil {
		t.Fatal(err)
	}
	writer.Header.Name = ""
	writer.Header.ModTime = time.Time{}
	if _, err := writer.Write(data); err != nil {
		t.Fatal(err)
	}
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}
	return output.Bytes()
}

func mustManifestBytes(t *testing.T, manifest *v2Manifest) []byte {
	t.Helper()
	manifest.ContentSHA256 = ""
	raw, err := json.Marshal(manifest)
	if err != nil {
		t.Fatal(err)
	}
	withoutDigest, err := removeManifestDigest(raw)
	if err != nil {
		t.Fatal(err)
	}
	canonical, err := canonicalJSON(withoutDigest)
	if err != nil {
		t.Fatal(err)
	}
	manifest.ContentSHA256 = sha256Hex(canonical)
	pretty, err := json.MarshalIndent(manifest, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	return append(pretty, '\n')
}

func sha256Hex(data []byte) string {
	digest := sha256.Sum256(data)
	return hex.EncodeToString(digest[:])
}
