package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"io/fs"
	"strings"

	"github.com/NKC-CV2026/Graduation-research/backend/app/internal/seed"
)

// seedDatasetsEnv lists the embedded datasets to write after the schema
// migration, separated by commas. When it is unset or empty, nothing is
// seeded.
const seedDatasetsEnv = "SEED_DATASET_IDS"

type seedReport struct {
	DatasetID  string `json:"dataset_id"`
	Status     string `json:"status"`
	Points     int    `json:"points"`
	Candidates int    `json:"candidates"`
}

// parseSeedDatasetIDs splits the comma-separated list, drops blanks and
// duplicates, and rejects ids that are not valid dataset ids.
func parseSeedDatasetIDs(raw string) ([]string, error) {
	var ids []string
	seen := map[string]bool{}
	for _, part := range strings.Split(raw, ",") {
		id := strings.TrimSpace(part)
		if id == "" || seen[id] {
			continue
		}
		if !seed.ValidDatasetID(id) {
			return nil, fmt.Errorf("%s: invalid dataset id %q", seedDatasetsEnv, id)
		}
		seen[id] = true
		ids = append(ids, id)
	}
	return ids, nil
}

// runSeed loads and validates every requested dataset first, so a bad entry
// stops the run before anything is written, then applies each one in order.
// Applying is idempotent: a dataset already stored with the same content is
// reported as unchanged.
func runSeed(ctx context.Context, db seed.Beginner, fsys fs.FS, ids []string, out io.Writer) error {
	bundles := make([]*seed.Bundle, 0, len(ids))
	for _, id := range ids {
		bundle, err := seed.Load(fsys, id)
		if err == nil {
			err = seed.Validate(bundle)
		}
		if err != nil {
			return fmt.Errorf("seed dataset %q: %w", id, err)
		}
		bundles = append(bundles, bundle)
	}
	for _, bundle := range bundles {
		result, err := seed.Apply(ctx, db, bundle)
		if err != nil {
			return fmt.Errorf("apply seed dataset %q: %w", bundle.Manifest.DatasetID, err)
		}
		line, _ := json.Marshal(seedReport{DatasetID: bundle.Manifest.DatasetID, Status: result.Status, Points: result.Points, Candidates: result.Candidates})
		fmt.Fprintln(out, string(line))
	}
	return nil
}
