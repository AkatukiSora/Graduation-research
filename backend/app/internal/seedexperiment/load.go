package seedexperiment

import (
	"bufio"
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"strings"
)

const (
	maxManifestBytes = 1 << 20
	maxDataBytes     = 4 << 20
	maxJSONLLine     = 64 << 10
)

func embeddedFS() fs.FS {
	sub, err := fs.Sub(embeddedData, "data")
	if err != nil {
		panic("seedexperiment: embedded data directory is missing: " + err.Error())
	}
	return sub
}

// Load reads only the fixed dataset, with explicit size bounds and strict JSON decoding.
func Load(fsys fs.FS, id string) (*Bundle, error) {
	if id != DatasetID {
		return nil, fmt.Errorf("unknown experimental direction dataset %q", id)
	}
	manifestPath := id + "/manifest.json"
	manifestBytes, err := readBounded(fsys, manifestPath, maxManifestBytes)
	if err != nil {
		return nil, fmt.Errorf("read manifest: %w", err)
	}
	var manifest Manifest
	if err := decodeStrict(manifestBytes, &manifest); err != nil {
		return nil, fmt.Errorf("decode manifest: %w", err)
	}
	if err := requireManifestFields(manifestBytes); err != nil {
		return nil, err
	}

	dataPath := id + "/experimental-directions.jsonl"
	data, err := readBounded(fsys, dataPath, maxDataBytes)
	if err != nil {
		return nil, fmt.Errorf("read estimates: %w", err)
	}
	digest := sha256.Sum256(data)
	contentSHA256 := hex.EncodeToString(digest[:])
	if contentSHA256 != manifest.DataSHA256 {
		return nil, fmt.Errorf("content_hash: manifest has %s, data has %s", manifest.DataSHA256, contentSHA256)
	}
	rows, err := decodeRows(data)
	if err != nil {
		return nil, err
	}
	bundle := &Bundle{Manifest: manifest, Rows: rows, ContentSHA256: contentSHA256}
	if err := Validate(bundle); err != nil {
		return nil, err
	}
	return bundle, nil
}

func readBounded(fsys fs.FS, name string, limit int64) ([]byte, error) {
	file, err := fsys.Open(name)
	if err != nil {
		return nil, err
	}
	defer file.Close()
	data, err := io.ReadAll(io.LimitReader(file, limit+1))
	if err != nil {
		return nil, err
	}
	if int64(len(data)) > limit {
		return nil, fmt.Errorf("%s exceeds the %d-byte limit", name, limit)
	}
	return data, nil
}

func requireManifestFields(data []byte) error {
	var manifest map[string]json.RawMessage
	if err := json.Unmarshal(data, &manifest); err != nil {
		return fmt.Errorf("decode manifest object: %w", err)
	}
	for _, key := range []string{"schema_version", "dataset_id", "source_month", "data_purpose", "production_approved", "confidence_semantics", "rows", "counts", "source_semantics", "data_sha256", "provenance"} {
		if _, ok := manifest[key]; !ok {
			return fmt.Errorf("manifest is missing required field %q", key)
		}
	}
	var counts map[string]json.RawMessage
	if err := json.Unmarshal(manifest["counts"], &counts); err != nil || counts == nil {
		return errors.New("manifest counts must be an object")
	}
	for _, subset := range []string{"teacher", "target"} {
		var values map[string]json.RawMessage
		if err := json.Unmarshal(counts[subset], &values); err != nil || values == nil {
			return fmt.Errorf("manifest counts.%s must be an object", subset)
		}
		for _, key := range []string{"rows", "inferred", "unknown"} {
			if _, ok := values[key]; !ok {
				return fmt.Errorf("manifest counts.%s is missing %q", subset, key)
			}
		}
	}
	return nil
}

func decodeStrict(data []byte, target any) error {
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(target); err != nil {
		return err
	}
	var extra any
	if err := decoder.Decode(&extra); !errors.Is(err, io.EOF) {
		if err == nil {
			return errors.New("multiple JSON values")
		}
		return err
	}
	return nil
}

func decodeRows(data []byte) ([]Row, error) {
	if len(data) == 0 || data[len(data)-1] != '\n' {
		return nil, errors.New("experimental direction JSONL must end with LF")
	}
	scanner := bufio.NewScanner(bytes.NewReader(data))
	scanner.Buffer(make([]byte, 4096), maxJSONLLine)
	rows := make([]Row, 0, 2333)
	line := 0
	for scanner.Scan() {
		line++
		text := scanner.Bytes()
		if len(text) == 0 || strings.ContainsRune(string(text), '\r') {
			return nil, fmt.Errorf("JSONL line %d is empty or contains CR", line)
		}
		var row Row
		if err := decodeStrict(text, &row); err != nil {
			return nil, fmt.Errorf("decode JSONL line %d: %w", line, err)
		}
		rows = append(rows, row)
	}
	if err := scanner.Err(); err != nil {
		return nil, fmt.Errorf("scan JSONL: %w", err)
	}
	return rows, nil
}
