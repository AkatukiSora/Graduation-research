package handler_test

import (
	"encoding/json"
	"errors"
	"net/http"
	"os"
	"testing"

	"github.com/NKC-CV2026/Graduation-research/backend/app/internal/domain"
)

// TestContractSamples writes real handler output to the file named by
// API_CONTRACT_SAMPLE_PATH. tests/check_openapi.py compares it with
// backend/docs/openapi.yaml so the document cannot drift from the handlers.
func TestContractSamples(t *testing.T) {
	path := os.Getenv("API_CONTRACT_SAMPLE_PATH")
	if path == "" {
		t.Skip("API_CONTRACT_SAMPLE_PATH is not set")
	}

	known := syntheticUnknown("SYN-KNOWN")
	known.AZ = float(197.5)
	known.DirectionStatus = domain.DirectionStatusConfirmed
	detail := detailOf(syntheticUnknown("SYN-1"))

	repo := &fakeStopPoints{
		list: []domain.StopPoint{syntheticUnknown("SYN-1"), known},
		byID: map[string]domain.StopPointDetail{"SYN-1": detail},
	}
	capture := func(router http.Handler, target string) json.RawMessage {
		rec := get(t, router, target)
		return json.RawMessage(rec.Body.Bytes())
	}

	router := newRouter(repo, fakeHealth{})
	failing := func(err error) http.Handler { return newRouter(&fakeStopPoints{err: err}, fakeHealth{err: err}) }

	samples := map[string]json.RawMessage{
		"range":                      capture(router, "/api/v1/stop-points?lat=1&long=1&radius=100"),
		"detail":                     capture(router, "/api/v1/stop-points/SYN-1"),
		"health_ok":                  capture(router, "/api/v1/health"),
		"health_degraded":            capture(failing(errors.New("down")), "/api/v1/health"),
		"error_invalid_request":      capture(router, "/api/v1/stop-points?long=1&radius=1"),
		"error_not_found":            capture(router, "/api/v1/stop-points/absent"),
		"error_database_unavailable": capture(failing(domain.ErrDatabaseUnavailable), "/api/v1/stop-points/SYN-1"),
		"error_internal_error":       capture(failing(errors.New("boom")), "/api/v1/stop-points/SYN-1"),
	}
	content, err := json.MarshalIndent(samples, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, content, 0o600); err != nil {
		t.Fatal(err)
	}
}
