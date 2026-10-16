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

// loadSeedBundles validates every requested embedded dataset without touching a database.
func loadSeedBundles(fsys fs.FS, ids []string) ([]*seed.Bundle, error) {
	bundles := make([]*seed.Bundle, 0, len(ids))
	for _, id := range ids {
		bundle, err := seed.Load(fsys, id)
		if err == nil {
			err = seed.Validate(bundle)
		}
		if err != nil {
			return nil, fmt.Errorf("seed dataset %q: %w", id, err)
		}
		bundles = append(bundles, bundle)
	}
	return bundles, nil
}

// applySeedBundles applies bundles only after all requested bundles are preflighted.
func applySeedBundles(ctx context.Context, db seed.Beginner, bundles []*seed.Bundle, out io.Writer) error {
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

// runSeed retains the test and caller behavior while ensuring all datasets are
// validated before the first database write.
func runSeed(ctx context.Context, db seed.Beginner, fsys fs.FS, ids []string, out io.Writer) error {
	bundles, err := loadSeedBundles(fsys, ids)
	if err != nil {
		return err
	}
	return applySeedBundles(ctx, db, bundles, out)
}
