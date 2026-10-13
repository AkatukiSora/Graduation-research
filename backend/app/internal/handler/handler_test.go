package handler_test

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/NKC-CV2026/Graduation-research/backend/app/internal/app"
	"github.com/NKC-CV2026/Graduation-research/backend/app/internal/domain"
	"github.com/NKC-CV2026/Graduation-research/backend/app/internal/handler"
	"github.com/NKC-CV2026/Graduation-research/backend/app/internal/service"
)

// fakeStopPoints is a test-only in-memory repository.
type fakeStopPoints struct {
	byID      map[string]domain.StopPointDetail
	list      []domain.StopPoint
	err       error
	lastRange domain.GetStopPointsByRangeInput
}

func (f *fakeStopPoints) FindByID(_ context.Context, id string) (domain.StopPointDetail, bool, error) {
	if f.err != nil {
		return domain.StopPointDetail{}, false, f.err
	}
	detail, ok := f.byID[id]
	return detail, ok, nil
}

func (f *fakeStopPoints) FindByRange(_ context.Context, input domain.GetStopPointsByRangeInput) ([]domain.StopPoint, error) {
	f.lastRange = input
	if f.err != nil {
		return nil, f.err
	}
	return f.list, nil
}

type fakeHealth struct{ err error }

func (f fakeHealth) Ping(context.Context) error { return f.err }

func newRouter(stopPoints *fakeStopPoints, health fakeHealth) http.Handler {
	return app.NewRouter(
		handler.NewHealthHandlers(service.NewHealthService(health)),
		handler.NewStopPointHandlers(service.NewStopPointService(stopPoints)),
	)
}

func float(v float64) *float64 { return &v }

func syntheticUnknown(key string) domain.StopPoint {
	return domain.StopPoint{
		UniqueKey:       key,
		Latitude:        1.00007,
		Longitude:       1.00031,
		DirectionStatus: domain.DirectionStatusUnknown,
		ValidityStatus:  domain.ValidityStatusUnknown,
		Synthetic:       true,
		DatasetID:       "synthetic-v1",
		SourceMonth:     "202701",
	}
}

func get(t *testing.T, router http.Handler, target string) *httptest.ResponseRecorder {
	t.Helper()
	rec := httptest.NewRecorder()
	router.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, target, nil))
	return rec
}

func decodeObject(t *testing.T, rec *httptest.ResponseRecorder) map[string]any {
	t.Helper()
	var body map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("response is not a JSON object: %v: %s", err, rec.Body.String())
	}
	return body
}

func assertError(t *testing.T, rec *httptest.ResponseRecorder, status int, code string) map[string]any {
	t.Helper()
	if rec.Code != status {
		t.Fatalf("status = %d, want %d: %s", rec.Code, status, rec.Body.String())
	}
	errBody, ok := decodeObject(t, rec)["error"].(map[string]any)
	if !ok {
		t.Fatalf("response has no error object: %s", rec.Body.String())
	}
	if errBody["code"] != code {
		t.Fatalf("error.code = %v, want %s", errBody["code"], code)
	}
	if message, _ := errBody["message"].(string); message == "" {
		t.Fatalf("error.message must not be empty: %s", rec.Body.String())
	}
	return errBody
}

func TestRangeReturnsUncertaintyFieldsAndNullAzimuth(t *testing.T) {
	repo := &fakeStopPoints{list: []domain.StopPoint{syntheticUnknown("SYN1")}}
	rec := get(t, newRouter(repo, fakeHealth{}), "/api/v1/stop-points?lat=1&long=1&radius=100&limit=7")

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d: %s", rec.Code, rec.Body.String())
	}
	var items []map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &items); err != nil || len(items) != 1 {
		t.Fatalf("unexpected body: %v: %s", err, rec.Body.String())
	}
	item := items[0]
	want := map[string]any{
		"uniqueKey":            "SYN1",
		"az":                   nil,
		"lat":                  "1.00007",
		"long":                 "1.00031",
		"directionStatus":      "unknown",
		"validityStatus":       "unknown",
		"synthetic":            true,
		"productionApproved":   false,
		"datasetId":            "synthetic-v1",
		"sourceMonth":          "202701",
		"notificationEligible": false,
	}
	if len(item) != len(want) {
		t.Fatalf("item has %d keys, want %d: %v", len(item), len(want), item)
	}
	for key, value := range want {
		got, present := item[key]
		if !present {
			t.Fatalf("missing key %q: %v", key, item)
		}
		if got != value {
			t.Fatalf("%s = %#v, want %#v", key, got, value)
		}
	}
	if repo.lastRange.Limit != 7 || repo.lastRange.RadiusMeters != 100 {
		t.Fatalf("range input not forwarded: %+v", repo.lastRange)
	}
	if !strings.Contains(rec.Body.String(), `"az":null`) {
		t.Fatalf("az must be an explicit JSON null: %s", rec.Body.String())
	}
}

func TestRangeEmptyResultIsEmptyArray(t *testing.T) {
	rec := get(t, newRouter(&fakeStopPoints{}, fakeHealth{}), "/api/v1/stop-points?lat=1&long=1&radius=100")
	if rec.Code != http.StatusOK || strings.TrimSpace(rec.Body.String()) != "[]" {
		t.Fatalf("status=%d body=%q", rec.Code, rec.Body.String())
	}
}

func TestKnownAzimuthIsDecimalString(t *testing.T) {
	point := syntheticUnknown("SYN2")
	point.AZ = float(197.5)
	point.DirectionStatus = domain.DirectionStatusInferred
	repo := &fakeStopPoints{list: []domain.StopPoint{point}}
	rec := get(t, newRouter(repo, fakeHealth{}), "/api/v1/stop-points?lat=1&long=1&radius=100")

	var items []map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &items); err != nil {
		t.Fatal(err)
	}
	if items[0]["az"] != "197.5" {
		t.Fatalf("az = %#v, want \"197.5\"", items[0]["az"])
	}
}

func TestDetailIncludesDatabaseTimestamps(t *testing.T) {
	detail := domain.StopPointDetail{
		StopPoint: syntheticUnknown("SYN1"),
		CreatedAt: "2026-10-13T01:02:03.456789Z",
		UpdatedAt: "2026-10-13T04:05:06Z",
	}
	repo := &fakeStopPoints{byID: map[string]domain.StopPointDetail{"SYN1": detail}}
	rec := get(t, newRouter(repo, fakeHealth{}), "/api/v1/stop-points/SYN1")

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d: %s", rec.Code, rec.Body.String())
	}
	body := decodeObject(t, rec)
	if body["created_at"] != detail.CreatedAt || body["updated_at"] != detail.UpdatedAt {
		t.Fatalf("timestamps not passed through: %v", body)
	}
	if body["az"] != nil || body["notificationEligible"] != false || body["directionStatus"] != "unknown" {
		t.Fatalf("unexpected uncertainty fields: %v", body)
	}
	if len(body) != 13 {
		t.Fatalf("detail has %d keys, want 13: %v", len(body), body)
	}
}

func TestNotificationEligibilityIsDerivedFromState(t *testing.T) {
	point := syntheticUnknown("OK1")
	point.Synthetic = false
	point.ProductionApproved = true
	point.AZ = float(90)
	point.DirectionStatus = domain.DirectionStatusConfirmed
	point.ValidityStatus = domain.ValidityStatusValid
	repo := &fakeStopPoints{list: []domain.StopPoint{point}}
	rec := get(t, newRouter(repo, fakeHealth{}), "/api/v1/stop-points?lat=1&long=1&radius=100")

	var items []map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &items); err != nil {
		t.Fatal(err)
	}
	if items[0]["notificationEligible"] != true {
		t.Fatalf("approved, real, confirmed, valid point must be eligible: %v", items[0])
	}
}

func TestInvalidRequestKeepsMessageAndAddsCode(t *testing.T) {
	cases := map[string]string{
		"/api/v1/stop-points?long=1&radius=1":                 "lat is required",
		"/api/v1/stop-points?lat=91&long=1&radius=1":          "lat must be between -90 and 90",
		"/api/v1/stop-points?lat=1&long=1&radius=0":           "radius must be greater than 0",
		"/api/v1/stop-points?lat=1&long=1&radius=1&limit=501": "limit must be less than or equal to 500",
		"/api/v1/stop-points?lat=NaN&long=1&radius=1":         "lat must be a number",
		"/api/v1/stop-points?lat=1&long=NaN&radius=1":         "long must be a number",
	}
	for target, message := range cases {
		t.Run(target, func(t *testing.T) {
			errBody := assertError(t, get(t, newRouter(&fakeStopPoints{}, fakeHealth{}), target), http.StatusBadRequest, "invalid_request")
			if errBody["message"] != message {
				t.Fatalf("message = %v, want %q", errBody["message"], message)
			}
		})
	}
}

func TestNotFoundKeepsMessageAndAddsCode(t *testing.T) {
	rec := get(t, newRouter(&fakeStopPoints{}, fakeHealth{}), "/api/v1/stop-points/absent")
	errBody := assertError(t, rec, http.StatusNotFound, "not_found")
	if errBody["message"] != "stop point not found" {
		t.Fatalf("message = %v", errBody["message"])
	}
}

func TestDatabaseUnavailableMapsTo503OnBothRoutes(t *testing.T) {
	wrapped := fmt.Errorf("find: %w", domain.ErrDatabaseUnavailable)
	repo := &fakeStopPoints{err: wrapped}
	for _, target := range []string{"/api/v1/stop-points?lat=1&long=1&radius=1", "/api/v1/stop-points/x"} {
		errBody := assertError(t, get(t, newRouter(repo, fakeHealth{}), target), http.StatusServiceUnavailable, "database_unavailable")
		if strings.Contains(fmt.Sprint(errBody["message"]), "find:") {
			t.Fatalf("internal error text leaked: %v", errBody["message"])
		}
	}
}

func TestUnexpectedErrorMapsTo500WithoutDetail(t *testing.T) {
	repo := &fakeStopPoints{err: errors.New("scan column secret-detail")}
	for _, target := range []string{"/api/v1/stop-points?lat=1&long=1&radius=1", "/api/v1/stop-points/x"} {
		rec := get(t, newRouter(repo, fakeHealth{}), target)
		assertError(t, rec, http.StatusInternalServerError, "internal_error")
		if strings.Contains(rec.Body.String(), "secret-detail") {
			t.Fatalf("internal error text leaked: %s", rec.Body.String())
		}
	}
}

func TestHealthReflectsDatabaseState(t *testing.T) {
	ok := get(t, newRouter(&fakeStopPoints{}, fakeHealth{}), "/api/v1/health")
	if ok.Code != http.StatusOK || strings.TrimSpace(ok.Body.String()) != `{"status":"ok","database":"ok"}` {
		t.Fatalf("healthy: status=%d body=%s", ok.Code, ok.Body.String())
	}

	down := get(t, newRouter(&fakeStopPoints{}, fakeHealth{err: errors.New("dial tcp 10.0.0.1: password=hunter2")}), "/api/v1/health")
	if down.Code != http.StatusServiceUnavailable || strings.TrimSpace(down.Body.String()) != `{"status":"degraded","database":"unavailable"}` {
		t.Fatalf("degraded: status=%d body=%s", down.Code, down.Body.String())
	}
}
