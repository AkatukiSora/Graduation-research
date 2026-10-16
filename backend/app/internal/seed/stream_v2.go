package seed

import (
	"bufio"
	"bytes"
	"compress/gzip"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"hash"
	"io"
	"io/fs"
	"math"
	"sort"
	"time"
	"unicode/utf8"
)

type v2Source struct {
	fsys     fs.FS
	root     string
	manifest v2Manifest
}

type v2PointIndex struct {
	EntityID          string
	RawUID            string
	Lon               float64
	Lat               float64
	DirectionDegrees  float64
	HasDirection      bool
	DirectionStatus   string
	ImputationMethod  string
	ImputationReason  string
	DirectionSource   string
	ValidityStatus    string
	ValidityReason    string
	NearestNeighborID string
}

type v2CandidateIndex struct {
	SourceEntityID   string
	NeighborEntityID string
	Rank             int
	DistanceM        float64
	CandidateBearing float64
}

type v2EvidenceRecord struct {
	SchemaVersion      int    `json:"schema_version"`
	Kind               string `json:"kind"`
	RecordRef          string `json:"record_ref"`
	EntityID           string `json:"entity_id"`
	SourceMonth        string `json:"source_month"`
	SourceCSVSHA256    string `json:"source_csv_sha256"`
	MethodVersion      string `json:"method_version"`
	DirectionSemantics string `json:"direction_semantics,omitempty"`
	ObservationRef     string `json:"observation_ref,omitempty"`
	ObservedAt         string `json:"observed_at,omitempty"`
	ObservationSHA256  string `json:"observation_sha256,omitempty"`
	EffectiveAt        string `json:"effective_at,omitempty"`
	ValidityReviewSHA  string `json:"validity_review_sha256,omitempty"`
}

type v2Index struct {
	points            map[string]v2PointIndex
	uids              map[string]struct{}
	neighbors         map[string]struct{}
	firstCandidate    map[string]v2CandidateIndex
	evidenceRecords   map[string]v2EvidenceRecord
	evidenceUses      map[string]struct{}
	stateCounts       v2Counts
	indexBytes        int64
	lastPointID       string
	lastCandidateID   string
	lastCandidateRank int
}

var (
	v2PointKeys = []string{
		"entity_id", "raw_uid", "lon", "lat", "direction_degrees", "direction_status",
		"imputation_method", "imputation_reason", "direction_source", "direction_evidence",
		"validity_status", "validity_reason", "validity_evidence", "normalization_status",
	}
	v2CandidateKeys = []string{
		"source_entity_id", "neighbor_entity_id", "rank", "distance_m", "candidate_bearing", "features", "bearing_semantics",
	}
	v2UnknownDirectionReasons = map[string]bool{
		"missing_entrance_coordinates": true, "unsupported_entrance_geometry": true,
		"topology_only_unverified": true, "source_identity_unverified": true,
		"legacy_unrecorded": true, "no_neighbor_within_radius": true,
		"zero_length_geometry": true, "ambiguous_nearest_neighbor": true,
		"multiple_entrance_directions": true,
	}
	v2ValidityReasons = map[string]bool{
		"source_validity_unverified": true, "synthetic_fixture": true, "legacy_unrecorded": true,
	}
)

func (b *Bundle) validateV2(ctx context.Context) error {
	if b.v2 == nil {
		return invalid(CodeSchemaVersion, "schema version 2 requires a streaming artifact source")
	}
	m := &b.v2.manifest
	if b.Manifest.SchemaVersion != v2SchemaVersion || b.Manifest.DatasetID != m.DatasetID || b.Manifest.SourceMonth != m.SourceMonth ||
		b.Manifest.RegionID != m.RegionID || b.Manifest.Synthetic != m.Synthetic || b.Manifest.ContentSHA256 != m.ContentSHA256 ||
		b.ContentSHA256 != m.ContentSHA256 {
		return invalid(CodeHash, "loaded v2 bundle metadata was modified")
	}
	index, err := scanV2(ctx, b.v2)
	if err != nil {
		return err
	}
	return compareV2Counts(m.Counts, index.stateCounts)
}

func scanV2(ctx context.Context, source *v2Source) (*v2Index, error) {
	evidenceRecords, evidenceBytes, err := loadV2EvidenceArtifacts(source)
	if err != nil {
		return nil, err
	}
	idx := &v2Index{
		points:          make(map[string]v2PointIndex, source.manifest.Counts.Points),
		uids:            make(map[string]struct{}, source.manifest.Counts.Points),
		neighbors:       make(map[string]struct{}, source.manifest.Counts.Candidates),
		firstCandidate:  make(map[string]v2CandidateIndex, source.manifest.Counts.Points),
		evidenceRecords: evidenceRecords,
		evidenceUses:    make(map[string]struct{}, len(evidenceRecords)),
		indexBytes:      evidenceBytes,
	}
	idx.stateCounts.DirectionStatusCounts = zeroCountMap([]string{"unknown", "inferred", "confirmed"})
	idx.stateCounts.ValidityStatusCounts = zeroCountMap([]string{"unknown", "valid", "invalid"})
	idx.stateCounts.ImputationMethodCounts = zeroCountMap([]string{"none", "source_entrance_geometry", "nearest_stop_reference", "osm_fusion_cautious", "field_observation"})
	idx.stateCounts.ImputationReasonCounts = zeroCountMap([]string{"none", "missing_entrance_coordinates", "unsupported_entrance_geometry", "topology_only_unverified", "source_identity_unverified", "legacy_unrecorded", "source_geometry_derived", "model_direction_inference", "topology_proxy_unverified", "no_neighbor_within_radius", "zero_length_geometry", "ambiguous_nearest_neighbor", "multiple_entrance_directions"})
	idx.stateCounts.ValidityReasonCounts = zeroCountMap([]string{"source_validity_unverified", "active_regulation_evidence", "withdrawn_regulation_evidence", "synthetic_fixture", "legacy_unrecorded"})

	if err := scanV2Kind(ctx, source, "points", func(line []byte, rowNumber int) error {
		if err := requireV2NumberFields(line, "lon", "lat"); err != nil {
			return invalid(CodeCoordinate, "point row %d has invalid required numeric field: %v", rowNumber, err)
		}
		var p Point
		if err := decodeV2Strict(line, &p, v2PointKeys); err != nil {
			return invalid(CodeSchemaVersion, "point row %d has invalid strict JSON: %v", rowNumber, err)
		}
		if err := validateV2Point(&p, &source.manifest, idx.evidenceRecords, idx.evidenceUses); err != nil {
			return fmt.Errorf("point row %d: %w", rowNumber, err)
		}
		nearestNeighbor := ""
		if p.ImputationMethod == "nearest_stop_reference" {
			var evidence struct {
				Neighbor string `json:"neighbor_entity_id"`
			}
			if err := json.Unmarshal(p.DirectionEvidence, &evidence); err != nil {
				return invalid(CodeDirection, "nearest evidence is invalid: %v", err)
			}
			nearestNeighbor = evidence.Neighbor
		}
		if idx.lastPointID != "" && p.EntityID <= idx.lastPointID {
			return invalid(CodeEntityID, "point rows must be strictly ordered by entity_id")
		}
		if _, exists := idx.uids[p.RawUID]; exists {
			return invalid(CodeDuplicate, "raw uid %q appears more than once", p.RawUID)
		}
		idx.lastPointID = p.EntityID
		idx.uids[p.RawUID] = struct{}{}
		pointIndex := v2PointIndex{
			EntityID: p.EntityID, RawUID: p.RawUID, Lon: p.Lon, Lat: p.Lat,
			DirectionStatus: p.DirectionStatus, ImputationMethod: p.ImputationMethod,
			ImputationReason: p.ImputationReason, DirectionSource: p.DirectionSource,
			ValidityStatus: p.ValidityStatus, ValidityReason: p.ValidityReason,
			NearestNeighborID: nearestNeighbor,
		}
		if p.DirectionDegrees != nil {
			pointIndex.DirectionDegrees, pointIndex.HasDirection = *p.DirectionDegrees, true
		}
		idx.points[p.EntityID] = pointIndex
		idx.indexBytes += int64(len(p.EntityID)+len(p.RawUID)) + 192
		idx.indexBytes += int64(len(p.RawUID) + 48) // UID map key and entry
		idx.stateCounts.DirectionStatusCounts[p.DirectionStatus]++
		idx.stateCounts.ValidityStatusCounts[p.ValidityStatus]++
		idx.stateCounts.ImputationMethodCounts[p.ImputationMethod]++
		idx.stateCounts.ImputationReasonCounts[p.ImputationReason]++
		idx.stateCounts.ValidityReasonCounts[p.ValidityReason]++
		if idx.indexBytes > maxV2IndexBytes {
			return invalid(CodeCount, "v2 validation index exceeds %d bytes", maxV2IndexBytes)
		}
		return nil
	}); err != nil {
		return nil, err
	}
	if len(idx.points) != source.manifest.Counts.Points {
		return nil, invalid(CodeCount, "point streams contain %d rows, manifest lists %d", len(idx.points), source.manifest.Counts.Points)
	}
	if len(idx.evidenceUses) != len(idx.evidenceRecords) {
		return nil, invalid(CodeDirection, "manifest contains unreferenced sanitized evidence artifacts")
	}

	if err := scanV2Kind(ctx, source, "candidates", func(line []byte, rowNumber int) error {
		if err := requireV2IntegerFields(line, "rank"); err != nil {
			return invalid(CodeRank, "candidate row %d has invalid required integer: %v", rowNumber, err)
		}
		if err := requireV2NumberFields(line, "distance_m", "candidate_bearing"); err != nil {
			return invalid(CodeDistance, "candidate row %d has invalid required numeric field: %v", rowNumber, err)
		}
		var candidate Candidate
		if err := decodeV2Strict(line, &candidate, v2CandidateKeys); err != nil {
			return invalid(CodeSchemaVersion, "candidate row %d has invalid strict JSON: %v", rowNumber, err)
		}
		if err := validateV2Candidate(candidate, idx.points); err != nil {
			return fmt.Errorf("candidate row %d: %w", rowNumber, err)
		}
		orderID := candidate.SourceEntityID
		if idx.lastCandidateID != "" && (orderID < idx.lastCandidateID || orderID == idx.lastCandidateID && candidate.Rank != idx.lastCandidateRank+1) {
			return invalid(CodeRank, "candidate rows must be strictly ordered by source_entity_id and contiguous rank")
		}
		if orderID != idx.lastCandidateID && candidate.Rank != 1 {
			return invalid(CodeRank, "candidate ranks for %q must start at 1", orderID)
		}
		pair := candidate.SourceEntityID + string([]byte{0}) + candidate.NeighborEntityID
		if _, exists := idx.neighbors[pair]; exists {
			return invalid(CodeDuplicate, "candidate repeats neighbor %q for %q", candidate.NeighborEntityID, candidate.SourceEntityID)
		}
		idx.neighbors[pair] = struct{}{}
		idx.indexBytes += int64(len(pair)) + 256
		if idx.indexBytes > maxV2IndexBytes {
			return invalid(CodeCount, "v2 validation index exceeds %d bytes", maxV2IndexBytes)
		}
		if candidate.Rank == 1 {
			idx.firstCandidate[candidate.SourceEntityID] = v2CandidateIndex{
				SourceEntityID: candidate.SourceEntityID, NeighborEntityID: candidate.NeighborEntityID,
				Rank: candidate.Rank, DistanceM: candidate.DistanceM, CandidateBearing: candidate.CandidateBearing,
			}
		}
		idx.lastCandidateID, idx.lastCandidateRank = orderID, candidate.Rank
		return nil
	}); err != nil {
		return nil, err
	}
	lat0 := medianLatitudeIndex(idx.points)
	for id, point := range idx.points {
		if point.ImputationMethod == "nearest_stop_reference" {
			if err := validateNearestEvidence(point, idx.points, idx.firstCandidate[id], source.manifest.Inputs.SourceCSVSHA256, lat0); err != nil {
				return nil, fmt.Errorf("point %q: %w", id, err)
			}
		}
	}
	idx.stateCounts.Points = len(idx.points)
	idx.stateCounts.Candidates = len(idx.neighbors)
	idx.stateCounts.Rejected = source.manifest.Counts.Rejected
	idx.stateCounts.OutsideRegion = source.manifest.Counts.OutsideRegion
	idx.stateCounts.RowsTotal = source.manifest.Counts.RowsTotal
	idx.stateCounts.ExcludedNonStop = source.manifest.Counts.ExcludedNonStop
	return idx, nil
}

func zeroCountMap(keys []string) map[string]int {
	out := make(map[string]int, len(keys))
	for _, key := range keys {
		out[key] = 0
	}
	return out
}

func compareV2Counts(want, got v2Counts) error {
	if want.Points != got.Points || want.Candidates != got.Candidates || want.Rejected != got.Rejected ||
		want.OutsideRegion != got.OutsideRegion || want.RowsTotal != got.RowsTotal || want.ExcludedNonStop != got.ExcludedNonStop ||
		!sameCountMap(want.DirectionStatusCounts, got.DirectionStatusCounts) || !sameCountMap(want.ValidityStatusCounts, got.ValidityStatusCounts) ||
		!sameCountMap(want.ImputationMethodCounts, got.ImputationMethodCounts) || !sameCountMap(want.ImputationReasonCounts, got.ImputationReasonCounts) ||
		!sameCountMap(want.ValidityReasonCounts, got.ValidityReasonCounts) {
		return invalid(CodeCount, "streamed row counts or mixed-state counts differ from manifest")
	}
	return nil
}

func sameCountMap(left, right map[string]int) bool {
	if len(left) != len(right) {
		return false
	}
	for key, value := range left {
		if right[key] != value {
			return false
		}
	}
	return true
}

func scanV2Kind(ctx context.Context, source *v2Source, kind string, consume func([]byte, int) error) error {
	var totalCompressed, totalUncompressed int64
	for _, artifact := range v2FileNames(&source.manifest, kind) {
		if err := ctx.Err(); err != nil {
			return fmt.Errorf("stream %s: %w", kind, err)
		}
		name := source.root + "/" + artifact.Basename
		if source.root == "." {
			name = artifact.Basename
		}
		actual, err := scanV2File(ctx, source.fsys, name, artifact, consume)
		if err != nil {
			return fmt.Errorf("artifact %q: %w", artifact.Basename, err)
		}
		totalCompressed += actual.compressedBytes
		totalUncompressed += actual.uncompressedBytes
		if totalCompressed > maxBundleCompressed || totalUncompressed > maxBundleUncompressed {
			return invalid(CodeCount, "v2 bundle exceeds total compressed or expanded size limit")
		}
	}
	return nil
}

type v2FileActual struct {
	rows              int
	compressedBytes   int64
	uncompressedBytes int64
}

func scanV2File(ctx context.Context, fsys fs.FS, name string, artifact v2File, consume func([]byte, int) error) (v2FileActual, error) {
	var actual v2FileActual
	file, err := fsys.Open(name)
	if err != nil {
		return actual, fmt.Errorf("open artifact: %w", err)
	}
	defer file.Close()
	compressedHash := sha256.New()
	compressedLimit := &io.LimitedReader{R: file, N: artifact.CompressedBytes + 1}
	buffered := bufio.NewReaderSize(io.TeeReader(compressedLimit, compressedHash), 32<<10)
	gz, err := gzip.NewReader(buffered)
	if err != nil {
		return actual, fmt.Errorf("open gzip: %w", err)
	}
	gz.Multistream(false)
	uncompressedHash := sha256.New()
	counted := &hashingReader{reader: gz, hash: uncompressedHash}
	reader := bufio.NewReaderSize(counted, 4<<10)
	for {
		if err := ctx.Err(); err != nil {
			_ = gz.Close()
			return actual, err
		}
		line, bytesRead, readErr := readBoundedJSONLLine(reader)
		if readErr == io.EOF {
			break
		}
		if readErr != nil {
			_ = gz.Close()
			return actual, readErr
		}
		actual.uncompressedBytes += int64(bytesRead)
		if actual.uncompressedBytes > maxShardUncompressedSize || actual.uncompressedBytes > maxBundleUncompressed {
			_ = gz.Close()
			return actual, fmt.Errorf("expanded shard exceeds %d bytes", maxShardUncompressedSize)
		}
		actual.rows++
		if err := consume(line, actual.rows); err != nil {
			_ = gz.Close()
			return actual, err
		}
	}
	if err := gz.Close(); err != nil {
		return actual, fmt.Errorf("close gzip: %w", err)
	}
	if actual.rows != artifact.Rows || actual.uncompressedBytes != artifact.UncompressedBytes {
		return actual, fmt.Errorf("expanded rows/bytes %d/%d differ from manifest %d/%d", actual.rows, actual.uncompressedBytes, artifact.Rows, artifact.UncompressedBytes)
	}
	if _, err := buffered.ReadByte(); !errors.Is(err, io.EOF) {
		if err == nil {
			return actual, errors.New("gzip artifact contains a second member or trailing bytes")
		}
		return actual, fmt.Errorf("read gzip trailer: %w", err)
	}
	actual.compressedBytes = artifact.CompressedBytes + 1 - compressedLimit.N
	if actual.compressedBytes > maxBundleCompressed || actual.compressedBytes != artifact.CompressedBytes {
		return actual, fmt.Errorf("compressed bytes %d differ from manifest %d or limit", actual.compressedBytes, artifact.CompressedBytes)
	}
	if got := hex.EncodeToString(compressedHash.Sum(nil)); got != artifact.CompressedSHA256 {
		return actual, fmt.Errorf("compressed SHA-256 %s differs from manifest", got)
	}
	if got := hex.EncodeToString(uncompressedHash.Sum(nil)); got != artifact.UncompressedSHA256 {
		return actual, fmt.Errorf("expanded SHA-256 %s differs from manifest", got)
	}
	return actual, nil
}

type hashingReader struct {
	reader io.Reader
	hash   hash.Hash
}

func (r *hashingReader) Read(p []byte) (int, error) {
	n, err := r.reader.Read(p)
	if n > 0 {
		_, _ = r.hash.Write(p[:n])
	}
	return n, err
}

func readBoundedJSONLLine(reader *bufio.Reader) ([]byte, int, error) {
	var line []byte
	for {
		part, err := reader.ReadSlice('\n')
		if len(line)+len(part) > maxJSONLLineBytes {
			return nil, 0, fmt.Errorf("JSONL line exceeds %d bytes", maxJSONLLineBytes)
		}
		line = append(line, part...)
		if err == nil {
			line = line[:len(line)-1]
			if len(line) == 0 || bytes.IndexByte(line, '\r') >= 0 || !utf8Valid(line) {
				return nil, 0, errors.New("JSONL line is empty, contains CR, or is not UTF-8")
			}
			return line, len(line) + 1, nil
		}
		if !errors.Is(err, bufio.ErrBufferFull) {
			if errors.Is(err, io.EOF) && len(line) == 0 {
				return nil, 0, io.EOF
			}
			return nil, 0, errors.New("JSONL artifact must end every row with LF")
		}
	}
}

func utf8Valid(value []byte) bool { return utf8.Valid(value) }
func validateV2Point(p *Point, m *v2Manifest, evidenceRecords map[string]v2EvidenceRecord, evidenceUses map[string]struct{}) error {
	if len(p.DirectionEvidence) > maxV2EvidenceBytes || len(p.ValidityEvidence) > maxV2EvidenceBytes {
		return invalid(CodeCount, "point evidence exceeds %d bytes", maxV2EvidenceBytes)
	}
	if p.EntityID == "" || !asciiUIDPattern.MatchString(p.RawUID) {
		return invalid(CodeUID, "entity_id must be non-empty and raw_uid exactly 32 ASCII alphanumeric characters")
	}
	if !finite(p.Lon) || !finite(p.Lat) || p.Lon < -180 || p.Lon > 180 || p.Lat < -90 || p.Lat > 90 {
		return invalid(CodeCoordinate, "coordinates must be finite WGS84 lon/lat")
	}
	if p.NormalizationStatus != "valid_candidate_only" || p.ImputationReason == "" || p.ValidityReason == "" {
		return invalid(CodeNormalization, "normalization and state reason fields are required")
	}
	if p.DirectionDegrees != nil && (!finite(*p.DirectionDegrees) || *p.DirectionDegrees < 0 || *p.DirectionDegrees >= 360) {
		return invalid(CodeDirection, "direction_degrees must be null or finite in [0,360)")
	}
	if err := validateV2Direction(p, m, evidenceRecords, evidenceUses); err != nil {
		return err
	}
	if err := validateV2Validity(p, m, evidenceRecords, evidenceUses); err != nil {
		return err
	}
	return nil
}

func validateV2Direction(p *Point, m *v2Manifest, evidenceRecords map[string]v2EvidenceRecord, evidenceUses map[string]struct{}) error {
	switch p.DirectionStatus {
	case "unknown":
		if p.DirectionDegrees != nil || p.ImputationMethod != "none" || p.DirectionSource != "unknown" ||
			!v2UnknownDirectionReasons[p.ImputationReason] || !isEmptyJSONObject(p.DirectionEvidence) {
			return invalid(CodeDirection, "unknown state must have null degree, no method/source and empty evidence")
		}
	case "inferred":
		if p.DirectionDegrees == nil || p.ImputationReason == "none" || p.DirectionEvidence == nil {
			return invalid(CodeDirection, "inferred state requires degree, reason and evidence")
		}
		switch p.ImputationMethod {
		case "source_entrance_geometry":
			if p.DirectionSource != "jartic_entrance" || p.ImputationReason != "source_geometry_derived" {
				return invalid(CodeDirection, "source geometry state combination is invalid")
			}
			if err := validateSourceGeometryEvidence(*p, m); err != nil {
				return err
			}
		case "nearest_stop_reference":
			if p.DirectionSource != "nearest_stop" || p.ImputationReason != "topology_proxy_unverified" {
				return invalid(CodeDirection, "nearest state combination is invalid")
			}
			if err := validateNearestEvidenceShape(*p, m); err != nil {
				return err
			}
		case "osm_fusion_cautious":
			return invalid(CodeDirection, "OSM model registry is empty; model inference is not registered")
		default:
			return invalid(CodeDirection, "inferred method %q is not supported", p.ImputationMethod)
		}
	case "confirmed":
		if p.DirectionDegrees == nil || p.ImputationMethod != "field_observation" || p.DirectionSource != "field_survey" || p.ImputationReason != "none" {
			return invalid(CodeDirection, "confirmed state requires field_observation/field_survey/none")
		}
		if err := validateFieldEvidence(*p, m, evidenceRecords, evidenceUses); err != nil {
			return err
		}
	default:
		return invalid(CodeDirection, "unknown direction status %q", p.DirectionStatus)
	}
	if m.Synthetic && p.DirectionStatus != "unknown" {
		return invalid(CodeDirection, "synthetic-v2 datasets must remain unknown")
	}
	return nil
}

func validateV2Validity(p *Point, m *v2Manifest, evidenceRecords map[string]v2EvidenceRecord, evidenceUses map[string]struct{}) error {
	switch p.ValidityStatus {
	case "unknown":
		if !v2ValidityReasons[p.ValidityReason] || !isEmptyJSONObject(p.ValidityEvidence) {
			return invalid(CodeValidity, "unknown validity requires an allowed reason and empty evidence")
		}
		if m.Synthetic && p.ValidityReason != "synthetic_fixture" {
			return invalid(CodeValidity, "synthetic-v2 validity must be synthetic_fixture")
		}
	case "valid", "invalid":
		wantReason := "active_regulation_evidence"
		if p.ValidityStatus == "invalid" {
			wantReason = "withdrawn_regulation_evidence"
		}
		if m.Synthetic || p.ValidityReason != wantReason {
			return invalid(CodeValidity, "known validity is only allowed in non-synthetic test/real-shaped bundles with matching reason")
		}
		if err := validateValidityEvidence(*p, m, evidenceRecords, evidenceUses); err != nil {
			return err
		}
	default:
		return invalid(CodeValidity, "unknown validity status %q", p.ValidityStatus)
	}
	return nil
}

func validateV2Candidate(c Candidate, points map[string]v2PointIndex) error {
	if _, ok := points[c.SourceEntityID]; !ok {
		return invalid(CodeCandidateRef, "source %q is not a point", c.SourceEntityID)
	}
	if _, ok := points[c.NeighborEntityID]; !ok {
		return invalid(CodeCandidateRef, "neighbor %q is not a point", c.NeighborEntityID)
	}
	if c.SourceEntityID == c.NeighborEntityID {
		return invalid(CodeSelfCandidate, "candidate cannot reference itself")
	}
	if c.Rank < 1 || c.Rank > 6 {
		return invalid(CodeRank, "candidate rank must be in 1..6")
	}
	if !finite(c.DistanceM) || c.DistanceM < 0 || c.DistanceM > 80 {
		return invalid(CodeDistance, "candidate distance must be finite and in [0,80]")
	}
	if !finite(c.CandidateBearing) || c.CandidateBearing < 0 || c.CandidateBearing >= 360 {
		return invalid(CodeBearing, "candidate bearing must be finite and in [0,360)")
	}
	if c.BearingSemantics != BearingSemantics {
		return invalid(CodeSemantics, "candidate bearing semantics must be %q", BearingSemantics)
	}
	if err := validateV2Features(c.Features); err != nil {
		return invalid(CodeFeatures, "candidate features: %v", err)
	}
	return nil
}

func validateV2Features(raw json.RawMessage) error {
	if len(raw) == 0 || raw[0] != '{' {
		return errors.New("features must be a JSON object")
	}
	if err := rejectDuplicateJSONKeys(raw); err != nil {
		return err
	}
	var features map[string]json.RawMessage
	if err := json.Unmarshal(raw, &features); err != nil || features == nil {
		return errors.New("features must be a JSON object")
	}
	allowed := v2FeatureKeySet
	for key, value := range features {
		if _, ok := allowed[key]; !ok {
			return fmt.Errorf("unknown feature key %q", key)
		}
		decoder := json.NewDecoder(bytes.NewReader(value))
		decoder.UseNumber()
		var decoded any
		if err := decoder.Decode(&decoded); err != nil {
			return fmt.Errorf("feature %q must be a non-null JSON number", key)
		}
		number, ok := decoded.(json.Number)
		if !ok || number == "" {
			return fmt.Errorf("feature %q must be a non-null JSON number", key)
		}
		var trailing any
		if err := decoder.Decode(&trailing); err != io.EOF {
			return fmt.Errorf("feature %q must contain exactly one JSON number", key)
		}
		parsed, err := number.Float64()
		if err != nil || !finite(parsed) {
			return fmt.Errorf("feature %q must be finite", key)
		}
	}
	return nil
}

var v2FeatureKeySet = makeV2FeatureKeys()

func makeV2FeatureKeys() map[string]struct{} {
	keys := map[string]struct{}{}
	for _, key := range []string{
		"candidate_rank", "candidate_distance_m", "candidate_bearing", "p_d1", "p_d2", "p_d3", "p_gap12", "p_ratio12",
		"q_d1", "q_d2", "reciprocal_rank", "mutual_nearest", "min_axis_angle_other", "min_directed_angle_other",
		"third_midpoint_distance_m", "bearing_mod_45", "bearing_mod_90",
	} {
		keys[key] = struct{}{}
	}
	for _, radius := range []int{12, 15, 18, 20, 25, 30, 40, 60, 100} {
		keys[fmt.Sprintf("p_n%d", radius)] = struct{}{}
		keys[fmt.Sprintf("q_n%d", radius)] = struct{}{}
	}
	return keys
}

func isEmptyJSONObject(raw json.RawMessage) bool {
	var object map[string]json.RawMessage
	return json.Unmarshal(raw, &object) == nil && object != nil && len(object) == 0
}

func validateSourceGeometryEvidence(p Point, m *v2Manifest) error {
	keys := []string{"schema_version", "kind", "source_csv_sha256", "method_version", "direction_semantics", "entrance"}
	var evidence struct {
		SchemaVersion int    `json:"schema_version"`
		Kind          string `json:"kind"`
		SourceCSV     string `json:"source_csv_sha256"`
		MethodVersion string `json:"method_version"`
		Semantics     string `json:"direction_semantics"`
		Entrance      struct {
			Lon float64 `json:"lon"`
			Lat float64 `json:"lat"`
		} `json:"entrance"`
	}
	if err := decodeV2Strict(p.DirectionEvidence, &evidence, keys); err != nil {
		return invalid(CodeDirection, "source geometry evidence: %v", err)
	}
	if evidence.SchemaVersion != 2 || evidence.Kind != "source_geometry" || evidence.SourceCSV != m.Inputs.SourceCSVSHA256 ||
		evidence.MethodVersion != "entrance-to-stop-sphere-v1" || evidence.Semantics != "vehicle_approach_to_stop_estimate" {
		return invalid(CodeDirection, "source geometry evidence values do not match the manifest contract")
	}
	var entranceKeys map[string]json.RawMessage
	if err := json.Unmarshal(p.DirectionEvidence, &entranceKeys); err != nil {
		return invalid(CodeDirection, "invalid source evidence object")
	}
	var entrance struct {
		Lon float64 `json:"lon"`
		Lat float64 `json:"lat"`
	}
	if err := requireV2NumberFields(entranceKeys["entrance"], "lon", "lat"); err != nil {
		return invalid(CodeDirection, "source geometry entrance must contain non-null numeric lon and lat: %v", err)
	}
	if err := json.Unmarshal(entranceKeys["entrance"], &entrance); err != nil {
		return invalid(CodeDirection, "source geometry entrance must contain numeric lon and lat")
	}
	var entranceObject map[string]json.RawMessage
	if err := json.Unmarshal(entranceKeys["entrance"], &entranceObject); err != nil || len(entranceObject) != 2 || entranceObject["lon"] == nil || entranceObject["lat"] == nil {
		return invalid(CodeDirection, "source geometry entrance must contain exactly lon and lat")
	}
	if !finite(entrance.Lon) || !finite(entrance.Lat) || entrance.Lon < -180 || entrance.Lon > 180 || entrance.Lat < -90 || entrance.Lat > 90 {
		return invalid(CodeDirection, "source geometry entrance coordinates are out of range")
	}
	bearing := sphericalBearing(entrance.Lon, entrance.Lat, p.Lon, p.Lat)
	if circularDifference(bearing, *p.DirectionDegrees) > 1e-6 || sphericalDistance(entrance.Lon, entrance.Lat, p.Lon, p.Lat) <= 1e-6 {
		return invalid(CodeDirection, "source geometry bearing does not match point coordinates or is zero length")
	}
	return nil
}

func validateNearestEvidenceShape(p Point, m *v2Manifest) error {
	keys := []string{"schema_version", "kind", "source_csv_sha256", "method_version", "direction_semantics", "neighbor_entity_id", "candidate_rank"}
	var evidence struct {
		SchemaVersion int    `json:"schema_version"`
		Kind          string `json:"kind"`
		SourceCSV     string `json:"source_csv_sha256"`
		MethodVersion string `json:"method_version"`
		Semantics     string `json:"direction_semantics"`
		Neighbor      string `json:"neighbor_entity_id"`
		Rank          int    `json:"candidate_rank"`
	}
	if err := decodeV2Strict(p.DirectionEvidence, &evidence, keys); err != nil {
		return invalid(CodeDirection, "nearest evidence: %v", err)
	}
	if evidence.SchemaVersion != 2 || evidence.Kind != "nearest_stop_reference" || evidence.SourceCSV != m.Inputs.SourceCSVSHA256 ||
		evidence.MethodVersion != "nearest-stop-proxy-v1" || evidence.Semantics != "stop_to_neighbor_proxy" || evidence.Neighbor == "" || evidence.Rank != 1 {
		return invalid(CodeDirection, "nearest evidence values do not match schema2 contract")
	}
	return nil
}

func evidenceArtifactKey(artifactSHA, recordRef string) string {
	return artifactSHA + string([]byte{0}) + recordRef
}

func loadV2EvidenceArtifacts(source *v2Source) (map[string]v2EvidenceRecord, int64, error) {
	records := make(map[string]v2EvidenceRecord)
	var retainedBytes int64
	for _, artifact := range source.manifest.Files {
		if artifact.Kind != "evidence" {
			continue
		}
		name := source.root + "/" + artifact.Basename
		if source.root == "." {
			name = artifact.Basename
		}
		file, err := source.fsys.Open(name)
		if err != nil {
			return nil, 0, fmt.Errorf("open evidence artifact %q: %w", artifact.Basename, err)
		}
		raw, readErr := io.ReadAll(io.LimitReader(file, maxV2EvidenceArtifactBytes+1))
		closeErr := file.Close()
		if readErr != nil {
			return nil, 0, fmt.Errorf("read evidence artifact %q: %w", artifact.Basename, readErr)
		}
		if closeErr != nil {
			return nil, 0, fmt.Errorf("close evidence artifact %q: %w", artifact.Basename, closeErr)
		}
		if int64(len(raw)) != artifact.Bytes || len(raw) > maxV2EvidenceArtifactBytes {
			return nil, 0, invalid(CodeCount, "evidence artifact %q size differs from manifest or exceeds %d bytes", artifact.Basename, maxV2EvidenceArtifactBytes)
		}
		digest := sha256.Sum256(raw)
		artifactSHA := hex.EncodeToString(digest[:])
		if artifactSHA != artifact.SHA256 {
			return nil, 0, invalid(CodeHash, "evidence artifact %q SHA-256 differs from manifest", artifact.Basename)
		}
		var header struct {
			Kind string `json:"kind"`
		}
		if err := json.Unmarshal(raw, &header); err != nil {
			return nil, 0, fmt.Errorf("decode evidence artifact %q: %w", artifact.Basename, err)
		}
		keys, err := v2EvidenceArtifactKeys(header.Kind)
		if err != nil {
			return nil, 0, fmt.Errorf("evidence artifact %q: %w", artifact.Basename, err)
		}
		if err := requireV2IntegerFields(raw, "schema_version"); err != nil {
			return nil, 0, fmt.Errorf("evidence artifact %q: %w", artifact.Basename, err)
		}
		var record v2EvidenceRecord
		if err := decodeV2Strict(raw, &record, keys); err != nil {
			return nil, 0, fmt.Errorf("decode evidence artifact %q: %w", artifact.Basename, err)
		}
		if err := validateV2EvidenceRecord(record, source.manifest); err != nil {
			return nil, 0, fmt.Errorf("evidence artifact %q: %w", artifact.Basename, err)
		}
		key := evidenceArtifactKey(artifactSHA, record.RecordRef)
		if _, exists := records[key]; exists {
			return nil, 0, invalid(CodeDuplicate, "evidence artifact repeats artifact/ref %q", record.RecordRef)
		}
		records[key] = record
		retainedBytes += int64(len(raw) + len(key) + 256)
		if retainedBytes > maxV2IndexBytes {
			return nil, 0, invalid(CodeCount, "evidence validation index exceeds %d bytes", maxV2IndexBytes)
		}
	}
	return records, retainedBytes, nil
}

func v2EvidenceArtifactKeys(kind string) ([]string, error) {
	common := []string{"schema_version", "kind", "record_ref", "entity_id", "source_month", "source_csv_sha256", "method_version"}
	switch kind {
	case "field_observation":
		return append(common, "direction_semantics", "observation_ref", "observed_at", "observation_sha256"), nil
	case "regulation_validity":
		return append(common, "effective_at", "validity_review_sha256"), nil
	default:
		return nil, fmt.Errorf("unsupported evidence artifact kind %q", kind)
	}
}

func validateV2EvidenceRecord(record v2EvidenceRecord, manifest v2Manifest) error {
	if record.SchemaVersion != 1 || !safeEvidenceRefPattern.MatchString(record.RecordRef) || record.EntityID == "" ||
		record.SourceMonth != manifest.SourceMonth || record.SourceCSVSHA256 != manifest.Inputs.SourceCSVSHA256 ||
		!safeEvidenceRefPattern.MatchString(record.MethodVersion) {
		return invalid(CodeProvenance, "evidence artifact scope or identifiers do not match manifest")
	}
	switch record.Kind {
	case "field_observation":
		if record.DirectionSemantics != "vehicle_approach_to_stop" || !safeEvidenceRefPattern.MatchString(record.ObservationRef) ||
			!isRFC3339Time(record.ObservedAt) || !v2DigestPattern.MatchString(record.ObservationSHA256) {
			return invalid(CodeDirection, "field evidence artifact lacks valid observation semantics, time or review digest")
		}
	case "regulation_validity":
		if !isISO8601Time(record.EffectiveAt) || !v2DigestPattern.MatchString(record.ValidityReviewSHA) {
			return invalid(CodeValidity, "validity evidence artifact lacks valid effective time or review digest")
		}
	default:
		return invalid(CodeProvenance, "unsupported evidence artifact kind %q", record.Kind)
	}
	return nil
}

func isRFC3339Time(value string) bool {
	_, err := time.Parse(time.RFC3339Nano, value)
	return err == nil
}

func isISO8601Time(value string) bool {
	if isRFC3339Time(value) {
		return true
	}
	_, err := time.Parse("2006-01-02", value)
	return err == nil
}

func claimEvidenceRecord(records map[string]v2EvidenceRecord, uses map[string]struct{}, artifactSHA, recordRef, kind string) (v2EvidenceRecord, error) {
	key := evidenceArtifactKey(artifactSHA, recordRef)
	record, ok := records[key]
	if !ok || record.Kind != kind {
		return v2EvidenceRecord{}, invalid(CodeProvenance, "referenced sanitized evidence artifact/ref is missing or has wrong kind")
	}
	if _, used := uses[key]; used {
		return v2EvidenceRecord{}, invalid(CodeDuplicate, "sanitized evidence artifact/ref is referenced more than once")
	}
	uses[key] = struct{}{}
	return record, nil
}

func validateFieldEvidence(p Point, m *v2Manifest, records map[string]v2EvidenceRecord, uses map[string]struct{}) error {
	keys := []string{"schema_version", "kind", "entity_id", "source_month", "source_csv_sha256", "artifact_sha256", "record_ref", "method_version", "direction_semantics", "observation_ref", "observed_at", "observation_sha256"}
	var evidence struct {
		SchemaVersion  int    `json:"schema_version"`
		Kind           string `json:"kind"`
		EntityID       string `json:"entity_id"`
		SourceMonth    string `json:"source_month"`
		SourceCSV      string `json:"source_csv_sha256"`
		Artifact       string `json:"artifact_sha256"`
		RecordRef      string `json:"record_ref"`
		MethodVersion  string `json:"method_version"`
		Semantics      string `json:"direction_semantics"`
		ObservationRef string `json:"observation_ref"`
		ObservedAt     string `json:"observed_at"`
		ObservationSHA string `json:"observation_sha256"`
	}
	if err := decodeV2Strict(p.DirectionEvidence, &evidence, keys); err != nil {
		return invalid(CodeDirection, "field evidence: %v", err)
	}
	if evidence.SchemaVersion != 1 || evidence.Kind != "field_observation" || evidence.EntityID != p.EntityID || evidence.SourceMonth != m.SourceMonth ||
		evidence.SourceCSV != m.Inputs.SourceCSVSHA256 || !v2DigestPattern.MatchString(evidence.Artifact) || !safeEvidenceRefPattern.MatchString(evidence.RecordRef) ||
		!safeEvidenceRefPattern.MatchString(evidence.MethodVersion) || evidence.Semantics != "vehicle_approach_to_stop" ||
		!safeEvidenceRefPattern.MatchString(evidence.ObservationRef) || !isRFC3339Time(evidence.ObservedAt) || !v2DigestPattern.MatchString(evidence.ObservationSHA) {
		return invalid(CodeDirection, "field evidence values do not match entity/source contract")
	}
	record, err := claimEvidenceRecord(records, uses, evidence.Artifact, evidence.RecordRef, "field_observation")
	if err != nil {
		return err
	}
	if record.EntityID != evidence.EntityID || record.SourceMonth != evidence.SourceMonth || record.SourceCSVSHA256 != evidence.SourceCSV ||
		record.MethodVersion != evidence.MethodVersion || record.DirectionSemantics != evidence.Semantics || record.ObservationRef != evidence.ObservationRef ||
		record.ObservedAt != evidence.ObservedAt || record.ObservationSHA256 != evidence.ObservationSHA {
		return invalid(CodeDirection, "field evidence artifact record does not match point evidence")
	}
	return nil
}

func validateValidityEvidence(p Point, m *v2Manifest, records map[string]v2EvidenceRecord, uses map[string]struct{}) error {
	keys := []string{"schema_version", "kind", "entity_id", "source_month", "source_csv_sha256", "artifact_sha256", "record_ref", "method_version", "effective_at", "validity_review_sha256"}
	var evidence struct {
		SchemaVersion int    `json:"schema_version"`
		Kind          string `json:"kind"`
		EntityID      string `json:"entity_id"`
		SourceMonth   string `json:"source_month"`
		SourceCSV     string `json:"source_csv_sha256"`
		Artifact      string `json:"artifact_sha256"`
		RecordRef     string `json:"record_ref"`
		MethodVersion string `json:"method_version"`
		EffectiveAt   string `json:"effective_at"`
		ReviewSHA     string `json:"validity_review_sha256"`
	}
	if err := decodeV2Strict(p.ValidityEvidence, &evidence, keys); err != nil {
		return invalid(CodeValidity, "validity evidence: %v", err)
	}
	if evidence.SchemaVersion != 1 || evidence.Kind != "regulation_validity" || evidence.EntityID != p.EntityID || evidence.SourceMonth != m.SourceMonth ||
		evidence.SourceCSV != m.Inputs.SourceCSVSHA256 || !v2DigestPattern.MatchString(evidence.Artifact) || !safeEvidenceRefPattern.MatchString(evidence.RecordRef) ||
		!safeEvidenceRefPattern.MatchString(evidence.MethodVersion) || !isISO8601Time(evidence.EffectiveAt) || !v2DigestPattern.MatchString(evidence.ReviewSHA) {
		return invalid(CodeValidity, "validity evidence values do not match entity/source contract")
	}
	record, err := claimEvidenceRecord(records, uses, evidence.Artifact, evidence.RecordRef, "regulation_validity")
	if err != nil {
		return err
	}
	if record.EntityID != evidence.EntityID || record.SourceMonth != evidence.SourceMonth || record.SourceCSVSHA256 != evidence.SourceCSV ||
		record.MethodVersion != evidence.MethodVersion || record.EffectiveAt != evidence.EffectiveAt || record.ValidityReviewSHA != evidence.ReviewSHA {
		return invalid(CodeValidity, "validity evidence artifact record does not match point evidence")
	}
	return nil
}

func medianLatitudeIndex(points map[string]v2PointIndex) float64 {
	latitudes := make([]float64, 0, len(points))
	for _, point := range points {
		latitudes = append(latitudes, point.Lat)
	}
	sort.Float64s(latitudes)
	middle := len(latitudes) / 2
	if len(latitudes)%2 == 0 {
		return (latitudes[middle-1] + latitudes[middle]) / 2
	}
	return latitudes[middle]
}

func medianLatitude(points map[string]Point) float64 {
	latitudes := make([]float64, 0, len(points))
	for _, point := range points {
		latitudes = append(latitudes, point.Lat)
	}
	sort.Float64s(latitudes)
	middle := len(latitudes) / 2
	if len(latitudes)%2 == 0 {
		return (latitudes[middle-1] + latitudes[middle]) / 2
	}
	return latitudes[middle]
}

func validateNearestEvidence(point v2PointIndex, points map[string]v2PointIndex, candidate v2CandidateIndex, sourceCSV string, lat0 float64) error {
	if candidate.SourceEntityID != point.EntityID || candidate.Rank != 1 || candidate.NeighborEntityID != point.NearestNeighborID ||
		point.NearestNeighborID == "" || !point.HasDirection || sourceCSV == "" {
		return invalid(CodeDirection, "nearest evidence does not match embedded rank-1 candidate")
	}
	cosLat := math.Cos(lat0 * math.Pi / 180)
	pointXY := project(point.Lon, point.Lat, cosLat)
	var nearest, second float64
	nearest = math.Inf(1)
	second = math.Inf(1)
	nearestID := ""
	for id, other := range points {
		if id == point.EntityID {
			continue
		}
		otherXY := project(other.Lon, other.Lat, cosLat)
		distance := math.Hypot(otherXY[0]-pointXY[0], otherXY[1]-pointXY[1])
		if distance < nearest {
			second, nearest, nearestID = nearest, distance, id
		} else if distance < second {
			second = distance
		}
	}
	if nearest <= 1e-6 {
		return invalid(CodeDistance, "nearest projection is zero length")
	}
	if second-nearest <= 1e-6 {
		return invalid(CodeRank, "nearest projection has a tied rank-1 neighbor")
	}
	if nearestID != candidate.NeighborEntityID || math.Abs(nearest-candidate.DistanceM) > 1e-6 {
		return invalid(CodeRank, "candidate rank 1 or projected distance differs from all-point nearest calculation")
	}
	neighborXY := project(points[candidate.NeighborEntityID].Lon, points[candidate.NeighborEntityID].Lat, cosLat)
	bearing := projectionBearing(pointXY[0], pointXY[1], neighborXY)
	if point.DirectionDegrees != candidate.CandidateBearing || circularDifference(bearing, point.DirectionDegrees) > 1e-6 {
		return invalid(CodeBearing, "nearest projected bearing differs from the copied direction or candidate")
	}
	return nil
}

func project(lon, lat, cosLat float64) [2]float64 {
	return [2]float64{lon * 111320 * cosLat, lat * 111320}
}

func projectionBearing(fromX, fromY float64, to [2]float64) float64 {
	return math.Mod(math.Atan2(to[0]-fromX, to[1]-fromY)*180/math.Pi+360, 360)
}

func sphericalBearing(fromLon, fromLat, toLon, toLat float64) float64 {
	phi1, phi2 := fromLat*math.Pi/180, toLat*math.Pi/180
	delta := (toLon - fromLon) * math.Pi / 180
	return math.Mod(math.Atan2(math.Sin(delta)*math.Cos(phi2), math.Cos(phi1)*math.Sin(phi2)-math.Sin(phi1)*math.Cos(phi2)*math.Cos(delta))*180/math.Pi+360, 360)
}

func sphericalDistance(fromLon, fromLat, toLon, toLat float64) float64 {
	const sphereRadius = 6371008.8
	phi1, phi2 := fromLat*math.Pi/180, toLat*math.Pi/180
	deltaPhi := (toLat - fromLat) * math.Pi / 180
	deltaLambda := (toLon - fromLon) * math.Pi / 180
	a := math.Sin(deltaPhi/2)*math.Sin(deltaPhi/2) + math.Cos(phi1)*math.Cos(phi2)*math.Sin(deltaLambda/2)*math.Sin(deltaLambda/2)
	return 2 * sphereRadius * math.Asin(math.Sqrt(math.Min(1, a)))
}

func circularDifference(left, right float64) float64 {
	difference := math.Mod(math.Abs(left-right), 360)
	if difference > 180 {
		difference = 360 - difference
	}
	return difference
}
