package config

import (
	"os"
	"strings"
	"testing"
)

func setRequiredEnv(t *testing.T) {
	t.Helper()
	t.Setenv("DB_HOST", "db.invalid")
	t.Setenv("DB_NAME", "app")
	t.Setenv("DB_USER", "api")
}

func TestStopPointDatasetIDDefaultsToSyntheticV1(t *testing.T) {
	setRequiredEnv(t)
	// t.Setenv restores the original value on cleanup; unset it for this test.
	t.Setenv("STOP_POINT_DATASET_ID", "placeholder")
	if err := os.Unsetenv("STOP_POINT_DATASET_ID"); err != nil {
		t.Fatal(err)
	}
	cfg, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	if cfg.StopPointDatasetID != "synthetic-v1" {
		t.Fatalf("default dataset id = %q", cfg.StopPointDatasetID)
	}
}

func TestStopPointDatasetIDFromEnvironment(t *testing.T) {
	setRequiredEnv(t)
	t.Setenv("STOP_POINT_DATASET_ID", "  regulation-202701  ")
	cfg, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	if cfg.StopPointDatasetID != "regulation-202701" {
		t.Fatalf("dataset id = %q", cfg.StopPointDatasetID)
	}
}

func TestStopPointDatasetIDRejectsBlankAndMalformedValues(t *testing.T) {
	for _, value := range []string{"", "   ", "Upper", "with space", "../x", strings.Repeat("a", 65)} {
		t.Run(value, func(t *testing.T) {
			setRequiredEnv(t)
			t.Setenv("STOP_POINT_DATASET_ID", value)
			if _, err := Load(); err == nil || !strings.Contains(err.Error(), "STOP_POINT_DATASET_ID") {
				t.Fatalf("Load() error = %v, want a STOP_POINT_DATASET_ID error", err)
			}
		})
	}
}
