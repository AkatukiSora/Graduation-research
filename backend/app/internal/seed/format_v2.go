package seed

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"unicode"
	"unicode/utf8"
)

const (
	v2SchemaVersion            = 2
	maxManifestBytes           = 1 << 20
	maxJSONLLineBytes          = 64 << 10
	maxShardUncompressedSize   = 4 << 20
	maxBundleUncompressed      = 64 << 20
	maxBundleCompressed        = 64 << 20
	maxBundleFiles             = 64
	maxV2Points                = 10000
	maxV2Candidates            = 250000
	maxV2IndexBytes            = 128 << 20
	maxV2EvidenceBytes         = 8 << 10
	maxV2EvidenceArtifactBytes = 64 << 10
)

var (
	asciiUIDPattern        = regexp.MustCompile(`^[A-Za-z0-9]{32}$`)
	v2DigestPattern        = regexp.MustCompile(`^[0-9a-f]{64}$`)
	safeEvidenceRefPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$`)
	integerPattern         = regexp.MustCompile(`^-?(0|[1-9][0-9]*)$`)
)

type v2Counts struct {
	Points                 int            `json:"points"`
	Candidates             int            `json:"candidates"`
	Rejected               int            `json:"rejected"`
	OutsideRegion          int            `json:"outside_region"`
	RowsTotal              int            `json:"rows_total"`
	ExcludedNonStop        int            `json:"excluded_non_stop"`
	DirectionStatusCounts  map[string]int `json:"direction_status_counts"`
	ValidityStatusCounts   map[string]int `json:"validity_status_counts"`
	ImputationMethodCounts map[string]int `json:"imputation_method_counts"`
	ImputationReasonCounts map[string]int `json:"imputation_reason_counts"`
	ValidityReasonCounts   map[string]int `json:"validity_reason_counts"`
}

type v2Geometry struct {
	RegionID                string `json:"region_id"`
	CenterLat               string `json:"center_lat"`
	CenterLon               string `json:"center_lon"`
	CenterSource            string `json:"center_source"`
	RadiusM                 int    `json:"radius_m"`
	SphereRadiusM           string `json:"sphere_radius_m"`
	Vertices                int    `json:"vertices"`
	CoordinateDecimalPlaces int    `json:"coordinate_decimal_places"`
	Algorithm               string `json:"algorithm"`
	BoundaryInclusion       string `json:"boundary_inclusion"`
	BoundarySHA256          string `json:"boundary_sha256"`
}

type v2Inputs struct {
	SourceZIPSHA256     *string `json:"source_zip_sha256"`
	SourceCSVSHA256     string  `json:"source_csv_sha256"`
	SourceCSVBytes      int64   `json:"source_csv_bytes"`
	SourceHeaderSHA256  string  `json:"source_header_sha256"`
	BoundarySHA256      string  `json:"boundary_sha256"`
	InputManifestSHA256 string  `json:"input_manifest_sha256"`
	EnrichmentSHA256    *string `json:"enrichment_sha256"`
}

type v2Generator struct {
	CodeRef                    string            `json:"code_ref"`
	CodeSHA256                 string            `json:"code_sha256"`
	MethodImplementationSHA256 string            `json:"method_implementation_sha256"`
	MethodVersion              string            `json:"method_version"`
	SourceSelectionOrder       []string          `json:"source_selection_order"`
	Commit                     string            `json:"commit"`
	UVLockSHA256               string            `json:"uv_lock_sha256"`
	PythonVersion              string            `json:"python_version"`
	DependencyVersions         map[string]string `json:"dependency_versions"`
	ZlibVersion                string            `json:"zlib_version"`
	Kernel                     string            `json:"kernel"`
	MaxCandidates              int               `json:"max_candidates"`
	CandidateRadiusM           int               `json:"candidate_radius_m"`
	ThreadPolicy               map[string]string `json:"thread_policy"`
}

type v2Provenance struct {
	Dataset            string `json:"dataset"`
	Publisher          string `json:"publisher"`
	Edition            string `json:"edition"`
	SourceURL          string `json:"source_url"`
	TermsReference     string `json:"terms_reference"`
	TermsSHA256        string `json:"terms_sha256"`
	RightsStatus       string `json:"rights_status"`
	Attribution        string `json:"attribution"`
	ModificationNotice string `json:"modification_notice"`
	IdentityScope      string `json:"identity_scope"`
	SourceIdentityNote string `json:"source_identity_note"`
}

type v2File struct {
	Kind               string `json:"kind"`
	Basename           string `json:"basename"`
	Rows               int    `json:"rows"`
	CompressedBytes    int64  `json:"compressed_bytes,omitempty"`
	CompressedSHA256   string `json:"compressed_sha256,omitempty"`
	UncompressedBytes  int64  `json:"uncompressed_bytes,omitempty"`
	UncompressedSHA256 string `json:"uncompressed_sha256,omitempty"`
	Bytes              int64  `json:"bytes,omitempty"`
	SHA256             string `json:"sha256,omitempty"`
}

type v2Manifest struct {
	SchemaVersion int          `json:"schema_version"`
	DatasetID     string       `json:"dataset_id"`
	SourceMonth   string       `json:"source_month"`
	RegionID      string       `json:"region_id"`
	Synthetic     bool         `json:"synthetic"`
	Counts        v2Counts     `json:"counts"`
	Geometry      v2Geometry   `json:"geometry"`
	Inputs        v2Inputs     `json:"inputs"`
	Generator     v2Generator  `json:"generator"`
	Provenance    v2Provenance `json:"provenance"`
	Files         []v2File     `json:"files"`
	ContentSHA256 string       `json:"content_sha256"`
}

var v2ManifestKeys = []string{
	"schema_version", "dataset_id", "source_month", "region_id", "synthetic", "counts", "geometry",
	"inputs", "generator", "provenance", "files", "content_sha256",
}

// canonicalJSON encodes JSON objects with recursively sorted keys. The format's
// manifest values use only integer numbers so Python and Go share exact bytes.
func canonicalJSON(raw []byte) ([]byte, error) {
	if !utf8.Valid(raw) {
		return nil, errors.New("canonical JSON must be valid UTF-8")
	}
	if err := rejectUnpairedSurrogateEscapes(raw); err != nil {
		return nil, err
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	var value any
	if err := decoder.Decode(&value); err != nil {
		return nil, err
	}
	if _, err := decoder.Token(); !errors.Is(err, io.EOF) {
		return nil, errors.New("unexpected data after JSON document")
	}
	if err := validateCanonicalValue(value); err != nil {
		return nil, err
	}
	var out bytes.Buffer
	encoder := json.NewEncoder(&out)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(value); err != nil {
		return nil, err
	}
	return bytes.TrimSuffix(out.Bytes(), []byte("\n")), nil
}

func validateCanonicalValue(value any) error {
	switch item := value.(type) {
	case json.Number:
		raw := string(item)
		if !integerPattern.MatchString(raw) || raw == "-0" {
			return fmt.Errorf("canonical manifest number %q is not a canonical integer", item)
		}
		number, err := strconv.ParseInt(raw, 10, 64)
		if err != nil || number > 9007199254740991 || number < -9007199254740991 {
			return fmt.Errorf("canonical manifest number %q exceeds the exact integer range", item)
		}
	case string:
		return validateCanonicalString(item)
	case []any:
		for _, child := range item {
			if err := validateCanonicalValue(child); err != nil {
				return err
			}
		}
	case map[string]any:
		for key, child := range item {
			if err := validateCanonicalString(key); err != nil {
				return err
			}
			if err := validateCanonicalValue(child); err != nil {
				return err
			}
		}
	}
	return nil
}

func validateCanonicalString(value string) error {
	for _, character := range value {
		if unicode.IsControl(character) || character == 0x2028 || character == 0x2029 {
			return fmt.Errorf("canonical manifest contains forbidden character U+%04X", character)
		}
	}
	return nil
}

func rejectUnpairedSurrogateEscapes(raw []byte) error {
	for i := 0; i < len(raw); {
		if raw[i] != '"' {
			i++
			continue
		}
		i++
		for i < len(raw) && raw[i] != '"' {
			if raw[i] != 92 {
				i++
				continue
			}
			if i+1 >= len(raw) {
				return nil // The JSON decoder reports the truncated escape.
			}
			if raw[i+1] != 'u' || i+6 > len(raw) {
				i += 2
				continue
			}
			codepoint, err := strconv.ParseUint(string(raw[i+2:i+6]), 16, 16)
			if err != nil {
				i += 6
				continue
			}
			switch {
			case codepoint >= 0xD800 && codepoint <= 0xDBFF:
				if i+12 > len(raw) || raw[i+6] != 92 || raw[i+7] != 'u' {
					return errors.New("canonical manifest contains an unpaired surrogate escape")
				}
				low, lowErr := strconv.ParseUint(string(raw[i+8:i+12]), 16, 16)
				if lowErr != nil || low < 0xDC00 || low > 0xDFFF {
					return errors.New("canonical manifest contains an unpaired surrogate escape")
				}
				i += 12
			case codepoint >= 0xDC00 && codepoint <= 0xDFFF:
				return errors.New("canonical manifest contains an unpaired surrogate escape")
			default:
				i += 6
			}
		}
		if i < len(raw) {
			i++
		}
	}
	return nil
}

func requireV2NumberFields(raw []byte, fields ...string) error {
	var object map[string]json.RawMessage
	if err := json.Unmarshal(raw, &object); err != nil {
		return err
	}
	for _, field := range fields {
		value, ok := object[field]
		if !ok {
			return fmt.Errorf("required numeric field %q is missing", field)
		}
		var number json.Number
		if err := json.Unmarshal(value, &number); err != nil || number == "" {
			return fmt.Errorf("field %q must be a non-null JSON number", field)
		}
		parsed, err := number.Float64()
		if err != nil || !finite(parsed) {
			return fmt.Errorf("field %q must be finite", field)
		}
	}
	return nil
}

func requireV2IntegerFields(raw []byte, fields ...string) error {
	var object map[string]json.RawMessage
	if err := json.Unmarshal(raw, &object); err != nil {
		return err
	}
	for _, field := range fields {
		value, ok := object[field]
		if !ok {
			return fmt.Errorf("required integer field %q is missing", field)
		}
		if err := requireV2Integer(value); err != nil {
			return fmt.Errorf("field %q %w", field, err)
		}
	}
	return nil
}

func decodeV2Strict(raw []byte, into any, keys []string) error {
	if err := rejectDuplicateJSONKeys(raw); err != nil {
		return err
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(raw, &fields); err != nil {
		return err
	}
	expected := make(map[string]struct{}, len(keys))
	for _, key := range keys {
		expected[key] = struct{}{}
	}
	for key := range fields {
		if _, ok := expected[key]; !ok {
			return fmt.Errorf("unknown field %q", key)
		}
	}
	for _, key := range keys {
		if _, ok := fields[key]; !ok {
			return fmt.Errorf("required key %q is missing", key)
		}
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(into); err != nil {
		return err
	}
	if err := decoder.Decode(new(any)); !errors.Is(err, io.EOF) {
		return errors.New("unexpected data after JSON object")
	}
	return nil
}

func rejectDuplicateJSONKeys(raw []byte) error {
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	if err := consumeJSONValue(decoder); err != nil {
		return err
	}
	if _, err := decoder.Token(); !errors.Is(err, io.EOF) {
		return errors.New("unexpected data after JSON document")
	}
	return nil
}

func consumeJSONValue(decoder *json.Decoder) error {
	token, err := decoder.Token()
	if err != nil {
		return err
	}
	delimiter, ok := token.(json.Delim)
	if !ok {
		return nil
	}
	switch delimiter {
	case '{':
		seen := make(map[string]struct{})
		for decoder.More() {
			keyToken, err := decoder.Token()
			if err != nil {
				return err
			}
			key, ok := keyToken.(string)
			if !ok {
				return errors.New("object key is not a string")
			}
			if _, exists := seen[key]; exists {
				return fmt.Errorf("duplicate JSON key %q", key)
			}
			seen[key] = struct{}{}
			if err := consumeJSONValue(decoder); err != nil {
				return err
			}
		}
		_, err = decoder.Token()
		return err
	case '[':
		for decoder.More() {
			if err := consumeJSONValue(decoder); err != nil {
				return err
			}
		}
		_, err = decoder.Token()
		return err
	default:
		return fmt.Errorf("unexpected JSON delimiter %q", delimiter)
	}
}

func validateV2Manifest(m *v2Manifest, canonicalDigest string) error {
	if m.SchemaVersion != v2SchemaVersion {
		return invalid(CodeSchemaVersion, "v2 manifest schema_version must be %d", v2SchemaVersion)
	}
	if !ValidDatasetID(m.DatasetID) {
		return invalid(CodeDatasetID, "dataset id %q is not a safe identifier", m.DatasetID)
	}
	if !monthPattern.MatchString(m.SourceMonth) {
		return invalid(CodeMonth, "source month %q is not YYYYMM", m.SourceMonth)
	}
	if m.RegionID == "" || m.Geometry.RegionID != m.RegionID {
		return invalid(CodeRegion, "region id must be non-empty and match geometry")
	}
	if !v2DigestPattern.MatchString(m.ContentSHA256) || m.ContentSHA256 != canonicalDigest {
		return invalid(CodeHash, "canonical manifest digest mismatch")
	}
	if m.Counts.Points <= 0 || m.Counts.Points > maxV2Points || m.Counts.Candidates < 0 || m.Counts.Candidates > maxV2Candidates ||
		m.Counts.Rejected < 0 || m.Counts.OutsideRegion < 0 || m.Counts.RowsTotal < 0 || m.Counts.ExcludedNonStop < 0 {
		return invalid(CodeCount, "v2 manifest counts are outside supported limits")
	}
	if m.Counts.RowsTotal != m.Counts.Points+m.Counts.Rejected+m.Counts.OutsideRegion+m.Counts.ExcludedNonStop {
		return invalid(CodeCount, "rows_total does not equal points + rejected + outside_region + excluded_non_stop")
	}
	if err := validateCountMap(m.Counts.DirectionStatusCounts, []string{"unknown", "inferred", "confirmed"}); err != nil {
		return invalid(CodeCount, "direction_status_counts: %v", err)
	}
	if err := validateCountMap(m.Counts.ValidityStatusCounts, []string{"unknown", "valid", "invalid"}); err != nil {
		return invalid(CodeCount, "validity_status_counts: %v", err)
	}
	if err := validateCountMap(m.Counts.ImputationMethodCounts, []string{"none", "source_entrance_geometry", "nearest_stop_reference", "osm_fusion_cautious", "field_observation"}); err != nil {
		return invalid(CodeCount, "imputation_method_counts: %v", err)
	}
	if err := validateCountMap(m.Counts.ImputationReasonCounts, []string{"none", "missing_entrance_coordinates", "unsupported_entrance_geometry", "topology_only_unverified", "source_identity_unverified", "legacy_unrecorded", "source_geometry_derived", "model_direction_inference", "topology_proxy_unverified", "no_neighbor_within_radius", "zero_length_geometry", "ambiguous_nearest_neighbor", "multiple_entrance_directions"}); err != nil {
		return invalid(CodeCount, "imputation_reason_counts: %v", err)
	}
	if err := validateCountMap(m.Counts.ValidityReasonCounts, []string{"source_validity_unverified", "active_regulation_evidence", "withdrawn_regulation_evidence", "synthetic_fixture", "legacy_unrecorded"}); err != nil {
		return invalid(CodeCount, "validity_reason_counts: %v", err)
	}
	if sumCountMap(m.Counts.DirectionStatusCounts) != m.Counts.Points || sumCountMap(m.Counts.ValidityStatusCounts) != m.Counts.Points ||
		sumCountMap(m.Counts.ImputationMethodCounts) != m.Counts.Points || sumCountMap(m.Counts.ImputationReasonCounts) != m.Counts.Points ||
		sumCountMap(m.Counts.ValidityReasonCounts) != m.Counts.Points {
		return invalid(CodeCount, "state count maps must each sum to points")
	}
	if m.Geometry.CenterLat == "" || m.Geometry.CenterLon == "" || m.Geometry.CenterSource == "" || m.Geometry.RadiusM < 0 ||
		m.Geometry.SphereRadiusM != "6371008.8" || m.Geometry.Vertices < 3 || m.Geometry.CoordinateDecimalPlaces < 0 ||
		m.Geometry.Algorithm == "" || m.Geometry.BoundaryInclusion != "covers" || !v2DigestPattern.MatchString(m.Geometry.BoundarySHA256) {
		return invalid(CodeRegion, "geometry metadata is incomplete or invalid")
	}
	if !v2DigestPattern.MatchString(m.Inputs.SourceCSVSHA256) || m.Inputs.SourceCSVBytes < 0 ||
		!v2DigestPattern.MatchString(m.Inputs.SourceHeaderSHA256) || !v2DigestPattern.MatchString(m.Inputs.BoundarySHA256) ||
		!v2DigestPattern.MatchString(m.Inputs.InputManifestSHA256) || m.Inputs.BoundarySHA256 != m.Geometry.BoundarySHA256 {
		return invalid(CodeProvenance, "input digests or byte count are invalid")
	}
	if m.Inputs.SourceZIPSHA256 == nil && !m.Synthetic || m.Inputs.SourceZIPSHA256 != nil && !v2DigestPattern.MatchString(*m.Inputs.SourceZIPSHA256) {
		return invalid(CodeProvenance, "source_zip_sha256 is required for non-synthetic data and must be a digest when present")
	}
	if m.Inputs.EnrichmentSHA256 != nil && !v2DigestPattern.MatchString(*m.Inputs.EnrichmentSHA256) {
		return invalid(CodeProvenance, "enrichment_sha256 must be a digest when present")
	}
	if m.Generator.CodeRef == "" || !v2DigestPattern.MatchString(m.Generator.CodeSHA256) ||
		!v2DigestPattern.MatchString(m.Generator.MethodImplementationSHA256) || m.Generator.MethodVersion == "" ||
		!equalStrings(m.Generator.SourceSelectionOrder, []string{"source_entrance_geometry", "nearest_stop_reference", "unknown"}) ||
		m.Generator.Commit == "" || !v2DigestPattern.MatchString(m.Generator.UVLockSHA256) || m.Generator.PythonVersion == "" || m.Generator.ZlibVersion == "" ||
		m.Generator.Kernel == "" || m.Generator.MaxCandidates != 6 || m.Generator.CandidateRadiusM != 80 ||
		len(m.Generator.DependencyVersions) == 0 || len(m.Generator.ThreadPolicy) == 0 {
		return invalid(CodeProvenance, "generator metadata is incomplete or invalid")
	}
	if m.Provenance.Dataset == "" || m.Provenance.Publisher == "" || m.Provenance.Edition == "" || m.Provenance.RightsStatus == "" ||
		m.Provenance.TermsReference == "" || m.Provenance.Attribution == "" || m.Provenance.ModificationNotice == "" ||
		m.Provenance.IdentityScope != "source_month" || m.Provenance.SourceIdentityNote == "" {
		return invalid(CodeProvenance, "provenance metadata is incomplete")
	}
	if m.Synthetic && m.Provenance.RightsStatus != SyntheticRightsStatus {
		return invalid(CodeProvenance, "synthetic rights_status must be %q", SyntheticRightsStatus)
	}
	if !m.Synthetic && (!v2DigestPattern.MatchString(m.Provenance.TermsSHA256) || m.Provenance.SourceURL == "") {
		return invalid(CodeProvenance, "non-synthetic provenance requires source URL and terms digest")
	}
	return validateV2Files(m)
}

func validateCountMap(values map[string]int, keys []string) error {
	if len(values) != len(keys) {
		return fmt.Errorf("has %d entries, want %d", len(values), len(keys))
	}
	for _, key := range keys {
		value, ok := values[key]
		if !ok || value < 0 {
			return fmt.Errorf("missing %q or has a negative count", key)
		}
	}
	return nil
}

func equalStrings(left, right []string) bool {
	if len(left) != len(right) {
		return false
	}
	for i := range left {
		if left[i] != right[i] {
			return false
		}
	}
	return true
}

func sumCountMap(values map[string]int) int {
	total := 0
	for _, value := range values {
		total += value
	}
	return total
}

func validateV2Files(m *v2Manifest) error {
	if len(m.Files) == 0 || len(m.Files) > maxBundleFiles {
		return invalid(CodeCount, "manifest files must contain 1..%d artifacts", maxBundleFiles)
	}
	var points, candidates, compressed, uncompressed, evidenceBytes int64
	seen := make(map[string]struct{}, len(m.Files))
	lastKind, lastName := "", ""
	kindShard := map[string]int{"points": 0, "candidates": 0}
	for _, file := range m.Files {
		if file.Kind != "points" && file.Kind != "candidates" && file.Kind != "evidence" {
			return invalid(CodeDatasetID, "unknown artifact kind %q", file.Kind)
		}
		wantSuffix := ".jsonl.gz"
		if file.Kind == "evidence" {
			wantSuffix = ".json"
		}
		if file.Basename == "" || file.Basename != pathBase(file.Basename) || strings.ContainsAny(file.Basename, `/\\`) ||
			!strings.HasPrefix(file.Basename, file.Kind+"-") || !strings.HasSuffix(file.Basename, wantSuffix) {
			return invalid(CodeDatasetID, "artifact basename %q is not a safe %s artifact name", file.Basename, file.Kind)
		}
		if _, ok := seen[file.Basename]; ok {
			return invalid(CodeDuplicate, "artifact basename %q is duplicated", file.Basename)
		}
		seen[file.Basename] = struct{}{}
		if file.Kind < lastKind || file.Kind == lastKind && file.Basename <= lastName {
			return invalid(CodeDatasetID, "artifact files are not in canonical kind/basename order")
		}
		lastKind, lastName = file.Kind, file.Basename
		if file.Kind == "evidence" {
			if file.Rows != 1 || file.Bytes <= 0 || file.Bytes > maxV2EvidenceArtifactBytes || !v2DigestPattern.MatchString(file.SHA256) ||
				file.CompressedBytes != 0 || file.CompressedSHA256 != "" || file.UncompressedBytes != 0 || file.UncompressedSHA256 != "" {
				return invalid(CodeCount, "evidence artifact %q has invalid rows, size, digest or shard fields", file.Basename)
			}
			evidenceBytes += file.Bytes
			continue
		}
		kindShard[file.Kind]++
		if file.Rows < 0 || file.CompressedBytes <= 0 || file.CompressedBytes > maxBundleCompressed || file.Bytes != 0 || file.SHA256 != "" ||
			file.UncompressedBytes < 0 || file.UncompressedBytes > maxShardUncompressedSize ||
			!v2DigestPattern.MatchString(file.CompressedSHA256) || !v2DigestPattern.MatchString(file.UncompressedSHA256) {
			return invalid(CodeCount, "artifact %q has invalid sizes, row count or digest", file.Basename)
		}
		compressed += file.CompressedBytes
		uncompressed += file.UncompressedBytes
		if file.Kind == "points" {
			points += int64(file.Rows)
		} else {
			candidates += int64(file.Rows)
		}
	}
	if kindShard["points"] == 0 || points != int64(m.Counts.Points) || candidates != int64(m.Counts.Candidates) ||
		compressed > maxBundleCompressed || uncompressed > maxBundleUncompressed || evidenceBytes > maxBundleUncompressed {
		return invalid(CodeCount, "artifact totals disagree with manifest counts or exceed bundle limits")
	}
	return nil
}

func pathBase(name string) string {
	index := strings.LastIndexAny(name, `/\\`)
	return name[index+1:]
}

func v2FileNames(m *v2Manifest, kind string) []v2File {
	var files []v2File
	for _, file := range m.Files {
		if file.Kind == kind {
			files = append(files, file)
		}
	}
	sort.Slice(files, func(i, j int) bool { return files[i].Basename < files[j].Basename })
	return files
}
