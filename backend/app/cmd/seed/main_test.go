package main

import (
	"bytes"
	"context"
	"encoding/json"
	"io/fs"
	"strings"
	"testing"
	"testing/fstest"

	"github.com/NKC-CV2026/Graduation-research/backend/app/internal/seed"
	"github.com/NKC-CV2026/Graduation-research/backend/app/internal/seeddata"
)

type fakeApplier struct {
	calls  int
	result seed.Result
	err    error
}

func (f *fakeApplier) apply(_ context.Context, _ *seed.Bundle) (seed.Result, error) {
	f.calls++
	return f.result, f.err
}

func runCLI(t *testing.T, fsys fs.FS, applier *fakeApplier, args ...string) (int, string, string) {
	t.Helper()
	var stdout, stderr bytes.Buffer
	code := run(context.Background(), args, &stdout, &stderr, environment{fsys: fsys, apply: applier.apply})
	return code, stdout.String(), stderr.String()
}

func TestValidateOnlyPrintsCountsWithoutTouchingTheDatabase(t *testing.T) {
	applier := &fakeApplier{}
	code, out, errOut := runCLI(t, seeddata.FS(), applier, "--validate-only", "--dataset", "synthetic-v1")
	if code != 0 {
		t.Fatalf("exit %d, stderr %q", code, errOut)
	}
	var report struct {
		DatasetID  string `json:"dataset_id"`
		Mode       string `json:"mode"`
		Status     string `json:"status"`
		Points     int    `json:"points"`
		Candidates int    `json:"candidates"`
		SHA256     string `json:"content_sha256"`
	}
	if err := json.Unmarshal([]byte(strings.TrimSpace(out)), &report); err != nil {
		t.Fatalf("stdout %q is not JSON: %v", out, err)
	}
	if report.DatasetID != "synthetic-v1" || report.Mode != "validate-only" || report.Status != "valid" ||
		report.Points != 12 || report.Candidates == 0 || len(report.SHA256) != 64 {
		t.Fatalf("report = %+v", report)
	}
	if applier.calls != 0 {
		t.Fatal("validate-only must never apply")
	}
}

func TestValidateOnlyWithoutDatasetChecksEveryEmbeddedDataset(t *testing.T) {
	code, out, errOut := runCLI(t, seeddata.FS(), &fakeApplier{}, "--validate-only")
	if code != 0 || !strings.Contains(out, `"synthetic-v1"`) {
		t.Fatalf("exit %d, stdout %q, stderr %q", code, out, errOut)
	}
}

func TestApplyRunsOnlyWithAnExplicitDatasetAfterValidation(t *testing.T) {
	applier := &fakeApplier{result: seed.Result{Status: seed.StatusInserted, Points: 12, Candidates: 58}}
	code, out, errOut := runCLI(t, seeddata.FS(), applier, "--apply", "--dataset", "synthetic-v1")
	if code != 0 || applier.calls != 1 {
		t.Fatalf("exit %d calls %d stderr %q", code, applier.calls, errOut)
	}
	if !strings.Contains(out, `"status":"inserted"`) || !strings.Contains(out, `"mode":"apply"`) {
		t.Fatalf("stdout = %q", out)
	}
}

func TestUsageErrorsExitTwoWithoutApplying(t *testing.T) {
	for _, args := range [][]string{
		{},
		{"--apply"},
		{"--apply", "--validate-only", "--dataset", "synthetic-v1"},
		{"--dataset", "synthetic-v1"},
		{"--validate-only", "extra"},
		{"--unknown"},
	} {
		applier := &fakeApplier{}
		code, _, _ := runCLI(t, seeddata.FS(), applier, args...)
		if code != 2 || applier.calls != 0 {
			t.Errorf("args %v: exit %d calls %d, want exit 2 and no apply", args, code, applier.calls)
		}
	}
}

func TestInvalidOrUnknownDatasetNeverReachesApply(t *testing.T) {
	applier := &fakeApplier{}
	if code, _, _ := runCLI(t, seeddata.FS(), applier, "--apply", "--dataset", "missing"); code != 1 || applier.calls != 0 {
		t.Fatalf("unknown dataset: exit %d calls %d", code, applier.calls)
	}
	dataset, _ := fs.ReadFile(seeddata.FS(), "synthetic-v1/dataset.json")
	manifest, _ := fs.ReadFile(seeddata.FS(), "synthetic-v1/manifest.json")
	tampered := bytes.Replace(dataset, []byte(`"lat":1.0004,`), []byte(`"lat":1.0005,`), 1)
	fsys := fstest.MapFS{
		"synthetic-v1/dataset.json":  {Data: tampered},
		"synthetic-v1/manifest.json": {Data: manifest},
	}
	code, _, errOut := runCLI(t, fsys, applier, "--apply", "--dataset", "synthetic-v1")
	if code != 1 || applier.calls != 0 || !strings.Contains(errOut, "content_hash") {
		t.Fatalf("tampered dataset: exit %d calls %d stderr %q", code, applier.calls, errOut)
	}
}
