package postgres

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"net"
	"os"
	"sort"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/NKC-CV2026/Graduation-research/backend/app/internal/domain"
	"github.com/NKC-CV2026/Graduation-research/backend/app/internal/seed"
	"github.com/NKC-CV2026/Graduation-research/backend/app/internal/seeddata"
)

const (
	syntheticDatasetID      = "synthetic-v1"
	nagoyaRuleTestDatasetID = "nagoya-rule-test-202602-v1"
)

// checkLocalDSN accepts only connection strings whose every host is a local
// Unix socket directory or a loopback address.
func checkLocalDSN(dsn string) error {
	cfg, err := pgconn.ParseConfig(dsn)
	if err != nil {
		return fmt.Errorf("parse test database url")
	}
	hosts := []string{cfg.Host}
	for _, fb := range cfg.Fallbacks {
		hosts = append(hosts, fb.Host)
	}
	for _, host := range hosts {
		switch {
		case strings.HasPrefix(host, "/"):
		case host == "localhost":
		default:
			if ip := net.ParseIP(host); ip == nil || !ip.IsLoopback() {
				return fmt.Errorf("test database host %q is not loopback or a local socket", host)
			}
		}
	}
	return nil
}

func TestCheckLocalDSN(t *testing.T) {
	for _, dsn := range []string{"postgresql://v@/postgres?host=/tmp/s&port=5432", "postgres://u@127.0.0.1:5432/db", "postgres://u@[::1]/db"} {
		if err := checkLocalDSN(dsn); err != nil {
			t.Errorf("checkLocalDSN(%q): %v", dsn, err)
		}
	}
	for _, dsn := range []string{"postgres://u@db.example.com/db", "postgres://u@10.0.0.5/db", "postgres://u@127.0.0.1,db.example.com/db"} {
		if checkLocalDSN(dsn) == nil {
			t.Errorf("checkLocalDSN(%q) accepted a non-local host", dsn)
		}
	}
}

func newTestPool(t *testing.T) *pgxpool.Pool {
	t.Helper()
	dsn := os.Getenv("TEST_DATABASE_URL")
	if dsn == "" {
		t.Skip("TEST_DATABASE_URL is not set; integration test skipped")
	}
	if err := checkLocalDSN(dsn); err != nil {
		t.Fatalf("refusing to run: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	pool, err := pgxpool.New(ctx, dsn)
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	t.Cleanup(pool.Close)
	if err := pool.Ping(ctx); err != nil {
		t.Fatalf("ping: %v", err)
	}
	return pool
}

// seededPool returns a pool whose database contains the synthetic-v1 dataset.
func seededPool(t *testing.T) (*pgxpool.Pool, *seed.Bundle) {
	t.Helper()
	pool := newTestPool(t)
	bundle, err := seed.Load(seeddata.FS(), syntheticDatasetID)
	if err != nil {
		t.Fatalf("load embedded dataset: %v", err)
	}
	if _, err := seed.Apply(context.Background(), pool, bundle); err != nil {
		t.Fatalf("apply embedded dataset: %v", err)
	}
	return pool, bundle
}

var scratchCounter atomic.Int64

type scratchPoint struct {
	entity, raw string
	lon, lat    float64
	degrees     *float64
	direction   string
	validity    string
}

// insertScratchDataset writes a dataset the test owns and removes it afterwards.
func insertScratchDataset(t *testing.T, pool *pgxpool.Pool, synthetic, approved bool, points []scratchPoint) string {
	t.Helper()
	ctx := context.Background()
	id := fmt.Sprintf("scratch-%d-%d", time.Now().UnixNano(), scratchCounter.Add(1))
	digest := sha256.Sum256([]byte(id))
	_, err := pool.Exec(ctx, `INSERT INTO regulation_datasets (dataset_id, content_sha256, source_month, region_id, synthetic, production_approved)
		VALUES ($1, $2, '202612', 'test:region', $3, $4)`, id, hex.EncodeToString(digest[:]), synthetic, approved)
	if err != nil {
		t.Fatalf("insert dataset: %v", err)
	}
	t.Cleanup(func() {
		_, _ = pool.Exec(context.Background(), `DELETE FROM regulation_points WHERE dataset_id = $1`, id)
		_, _ = pool.Exec(context.Background(), `DELETE FROM regulation_datasets WHERE dataset_id = $1`, id)
	})
	for _, p := range points {
		_, err := pool.Exec(ctx, `INSERT INTO regulation_points
			(dataset_id, entity_id, raw_uid, geom, direction_degrees, direction_status, validity_status, normalization_status)
			VALUES ($1, $2, $3, ST_SetSRID(ST_MakePoint($4::float8, $5::float8), 4326)::geography, $6, $7, $8, 'test')`,
			id, p.entity, p.raw, p.lon, p.lat, p.degrees, p.direction, p.validity)
		if err != nil {
			t.Fatalf("insert point %s: %v", p.entity, err)
		}
	}
	return id
}

func haversineMeters(lat1, lon1, lat2, lon2 float64) float64 {
	const earthRadius = 6371008.8
	rad := math.Pi / 180
	dLat, dLon := (lat2-lat1)*rad, (lon2-lon1)*rad
	a := math.Sin(dLat/2)*math.Sin(dLat/2) + math.Cos(lat1*rad)*math.Cos(lat2*rad)*math.Sin(dLon/2)*math.Sin(dLon/2)
	return 2 * earthRadius * math.Asin(math.Sqrt(a))
}

func keys(points []domain.StopPoint) []string {
	out := make([]string, 0, len(points))
	for _, p := range points {
		out = append(out, p.UniqueKey)
	}
	return out
}

// oracleOrder asks the database for the expected order with a differently
// written query (EWKT literal, no radius filter) so that the repository
// ordering is compared against an independent formulation.
func oracleOrder(t *testing.T, pool *pgxpool.Pool, dataset string, lon, lat float64) []string {
	t.Helper()
	rows, err := pool.Query(context.Background(), `SELECT raw_uid FROM regulation_points WHERE dataset_id = $1
		ORDER BY ST_Distance(geom, ST_GeogFromText($2)), entity_id`, dataset, fmt.Sprintf("SRID=4326;POINT(%v %v)", lon, lat))
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	var out []string
	for rows.Next() {
		var raw string
		if err := rows.Scan(&raw); err != nil {
			t.Fatal(err)
		}
		out = append(out, raw)
	}
	return out
}

func TestIntegrationRangeReturnsExpectedSetInStableOrder(t *testing.T) {
	pool, bundle := seededPool(t)
	repo := NewStopPointRepository(pool, syntheticDatasetID)
	ctx := context.Background()
	center := bundle.Dataset.Points[0]

	type measured struct {
		raw  string
		dist float64
	}
	var distances []measured
	for _, p := range bundle.Dataset.Points {
		distances = append(distances, measured{p.RawUID, haversineMeters(center.Lat, center.Lon, p.Lat, p.Lon)})
	}
	sort.Slice(distances, func(i, j int) bool { return distances[i].dist < distances[j].dist })

	const inside = 5
	low, high := distances[inside-1].dist, distances[inside].dist
	radius := int(math.Floor((low + high) / 2))
	if (high-low)/high < 0.02 {
		t.Fatalf("fixture has no clear gap around the radius: %f..%f", low, high)
	}
	wantSet := map[string]bool{}
	for _, m := range distances[:inside] {
		wantSet[m.raw] = true
	}

	got, err := repo.FindByRange(ctx, domain.GetStopPointsByRangeInput{Latitude: center.Lat, Longitude: center.Lon, RadiusMeters: radius, Limit: 500})
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != inside {
		t.Fatalf("got %d points within %d m, want %d: %v", len(got), radius, inside, keys(got))
	}
	for _, p := range got {
		if !wantSet[p.UniqueKey] {
			t.Fatalf("unexpected point %s within %d m", p.UniqueKey, radius)
		}
	}

	all, err := repo.FindByRange(ctx, domain.GetStopPointsByRangeInput{Latitude: center.Lat, Longitude: center.Lon, RadiusMeters: 100000, Limit: 500})
	if err != nil {
		t.Fatal(err)
	}
	if len(all) != len(bundle.Dataset.Points) {
		t.Fatalf("got %d points, want %d", len(all), len(bundle.Dataset.Points))
	}
	wantOrder := oracleOrder(t, pool, syntheticDatasetID, center.Lon, center.Lat)
	if fmt.Sprint(keys(all)) != fmt.Sprint(wantOrder) {
		t.Fatalf("order = %v, want %v", keys(all), wantOrder)
	}
	again, _ := repo.FindByRange(ctx, domain.GetStopPointsByRangeInput{Latitude: center.Lat, Longitude: center.Lon, RadiusMeters: 100000, Limit: 500})
	if fmt.Sprint(keys(again)) != fmt.Sprint(keys(all)) {
		t.Fatal("repeated queries must return the same order")
	}

	limited, err := repo.FindByRange(ctx, domain.GetStopPointsByRangeInput{Latitude: center.Lat, Longitude: center.Lon, RadiusMeters: 100000, Limit: 3})
	if err != nil {
		t.Fatal(err)
	}
	if fmt.Sprint(keys(limited)) != fmt.Sprint(wantOrder[:3]) {
		t.Fatalf("limit 3 = %v, want the nearest three %v", keys(limited), wantOrder[:3])
	}
}

func TestIntegrationRangeRadiusBoundary(t *testing.T) {
	pool, bundle := seededPool(t)
	repo := NewStopPointRepository(pool, syntheticDatasetID)
	ctx := context.Background()
	center, target := bundle.Dataset.Points[0], bundle.Dataset.Points[1]

	var distance float64
	err := pool.QueryRow(ctx, `SELECT ST_Distance(geom, ST_GeogFromText('SRID=4326;POINT(1 1)')) FROM regulation_points WHERE dataset_id = $1 AND raw_uid = $2`,
		syntheticDatasetID, target.RawUID).Scan(&distance)
	if err != nil || distance < 2 {
		t.Fatalf("distance = %f, err = %v", distance, err)
	}
	contains := func(radius int) bool {
		got, err := repo.FindByRange(ctx, domain.GetStopPointsByRangeInput{Latitude: center.Lat, Longitude: center.Lon, RadiusMeters: radius, Limit: 500})
		if err != nil {
			t.Fatal(err)
		}
		for _, p := range got {
			if p.UniqueKey == target.RawUID {
				return true
			}
		}
		return false
	}
	if !contains(int(math.Ceil(distance))) {
		t.Fatalf("a radius of %d m must include a point at %f m", int(math.Ceil(distance)), distance)
	}
	if contains(int(math.Floor(distance)) - 1) {
		t.Fatalf("a radius of %d m must exclude a point at %f m", int(math.Floor(distance))-1, distance)
	}
}

func TestIntegrationSyntheticPointsExposeUnknownEvidenceAndPurpose(t *testing.T) {
	pool, bundle := seededPool(t)
	repo := NewStopPointRepository(pool, syntheticDatasetID)
	center := bundle.Dataset.Points[0]

	got, err := repo.FindByRange(context.Background(), domain.GetStopPointsByRangeInput{Latitude: center.Lat, Longitude: center.Lon, RadiusMeters: 100000, Limit: 500})
	if err != nil || len(got) == 0 {
		t.Fatalf("got %d points, err = %v", len(got), err)
	}
	for _, p := range got {
		if p.AZ != nil || p.DirectionStatus != "unknown" || p.ValidityStatus != "unknown" {
			t.Fatalf("%s: az=%v direction=%s validity=%s", p.UniqueKey, p.AZ, p.DirectionStatus, p.ValidityStatus)
		}
		if !p.Synthetic || p.DatasetID != syntheticDatasetID || p.SourceMonth != bundle.Manifest.SourceMonth || p.DataPurpose != "synthetic_fixture" {
			t.Fatalf("%s: dataset fields = %+v", p.UniqueKey, p)
		}
		if p.ImputationMethod != "none" || p.ImputationReason != "legacy_unrecorded" ||
			p.DirectionSource != "unknown" || string(p.DirectionEvidence) != "{}" ||
			p.ValidityReason != "legacy_unrecorded" || string(p.ValidityEvidence) != "{}" {
			t.Fatalf("%s: evidence fields = %+v", p.UniqueKey, p)
		}
	}
}

func TestIntegrationFindByIDUsesDatabaseValues(t *testing.T) {
	pool, bundle := seededPool(t)
	repo := NewStopPointRepository(pool, syntheticDatasetID)
	ctx := context.Background()
	want := bundle.Dataset.Points[3]

	detail, found, err := repo.FindByID(ctx, want.RawUID)
	if err != nil || !found {
		t.Fatalf("found = %t, err = %v", found, err)
	}
	if detail.UniqueKey != want.RawUID || math.Abs(detail.Latitude-want.Lat) > 1e-9 || math.Abs(detail.Longitude-want.Lon) > 1e-9 {
		t.Fatalf("detail = %+v, want %s at (%v, %v)", detail, want.RawUID, want.Lat, want.Lon)
	}
	if detail.AZ != nil || detail.DatasetID != syntheticDatasetID {
		t.Fatalf("detail = %+v", detail)
	}

	var created, updated time.Time
	if err := pool.QueryRow(ctx, `SELECT created_at, updated_at FROM regulation_points WHERE dataset_id = $1 AND raw_uid = $2`,
		syntheticDatasetID, want.RawUID).Scan(&created, &updated); err != nil {
		t.Fatal(err)
	}
	if detail.CreatedAt != created.UTC().Format(time.RFC3339Nano) || detail.UpdatedAt != updated.UTC().Format(time.RFC3339Nano) {
		t.Fatalf("timestamps = %s / %s, want the stored %s / %s", detail.CreatedAt, detail.UpdatedAt, created, updated)
	}
	if !strings.HasSuffix(detail.CreatedAt, "Z") {
		t.Fatalf("created_at must be UTC: %s", detail.CreatedAt)
	}

	missing, found, err := repo.FindByID(ctx, "NO-SUCH-RAW-UID")
	if err != nil || found || missing.UniqueKey != "" {
		t.Fatalf("absent id: found = %t, err = %v, detail = %+v", found, err, missing)
	}

	// Identifiers the database cannot store are simply absent, not an error.
	for _, id := range []string{"bad\x00id", "bad\xffid", ""} {
		if _, found, err := repo.FindByID(ctx, id); err != nil || found {
			t.Fatalf("FindByID(%q): found = %t, err = %v", id, found, err)
		}
	}
}

func TestIntegrationOtherDatasetsAreIsolated(t *testing.T) {
	pool, bundle := seededPool(t)
	ctx := context.Background()
	shared := bundle.Dataset.Points[0].RawUID
	other := insertScratchDataset(t, pool, true, false, []scratchPoint{
		{entity: "other:1", raw: shared, lon: 10, lat: 10, direction: "unknown", validity: "unknown"},
		{entity: "other:2", raw: "OTHER-ONLY", lon: 10.0001, lat: 10, direction: "unknown", validity: "unknown"},
	})

	syntheticRepo := NewStopPointRepository(pool, syntheticDatasetID)
	otherRepo := NewStopPointRepository(pool, other)

	detail, found, err := syntheticRepo.FindByID(ctx, shared)
	if err != nil || !found || detail.DatasetID != syntheticDatasetID || math.Abs(detail.Latitude-1.0) > 1e-9 {
		t.Fatalf("synthetic repository returned %+v (found %t, err %v)", detail, found, err)
	}
	if _, found, _ := syntheticRepo.FindByID(ctx, "OTHER-ONLY"); found {
		t.Fatal("a point of another dataset must not be found")
	}
	near, err := syntheticRepo.FindByRange(ctx, domain.GetStopPointsByRangeInput{Latitude: 10, Longitude: 10, RadiusMeters: 1000, Limit: 500})
	if err != nil || len(near) != 0 {
		t.Fatalf("range around another dataset returned %v (err %v)", keys(near), err)
	}

	otherDetail, found, err := otherRepo.FindByID(ctx, shared)
	if err != nil || !found || otherDetail.DatasetID != other || math.Abs(otherDetail.Latitude-10) > 1e-9 {
		t.Fatalf("other repository returned %+v (found %t, err %v)", otherDetail, found, err)
	}
	otherRange, err := otherRepo.FindByRange(ctx, domain.GetStopPointsByRangeInput{Latitude: 10, Longitude: 10, RadiusMeters: 100000, Limit: 500})
	if err != nil || len(otherRange) != 2 {
		t.Fatalf("other range = %v (err %v)", keys(otherRange), err)
	}
}

func TestIntegrationRangeOrdersEqualDistancesByEntityID(t *testing.T) {
	pool := newTestPool(t)
	dataset := insertScratchDataset(t, pool, true, false, []scratchPoint{
		// Inserted in reverse entity order on purpose.
		{entity: "e:c", raw: "RAW-C", lon: 20, lat: 20, direction: "unknown", validity: "unknown"},
		{entity: "e:a", raw: "RAW-A", lon: 20, lat: 20, direction: "unknown", validity: "unknown"},
		{entity: "e:b", raw: "RAW-B", lon: 20, lat: 20, direction: "unknown", validity: "unknown"},
		{entity: "e:d", raw: "RAW-D", lon: 20.001, lat: 20, direction: "unknown", validity: "unknown"},
	})
	repo := NewStopPointRepository(pool, dataset)
	got, err := repo.FindByRange(context.Background(), domain.GetStopPointsByRangeInput{Latitude: 20, Longitude: 20, RadiusMeters: 1000, Limit: 500})
	if err != nil {
		t.Fatal(err)
	}
	if want := "[RAW-A RAW-B RAW-C RAW-D]"; fmt.Sprint(keys(got)) != want {
		t.Fatalf("order = %v, want %s", keys(got), want)
	}
	limited, _ := repo.FindByRange(context.Background(), domain.GetStopPointsByRangeInput{Latitude: 20, Longitude: 20, RadiusMeters: 1000, Limit: 2})
	if want := "[RAW-A RAW-B]"; fmt.Sprint(keys(limited)) != want {
		t.Fatalf("limited order = %v, want %s", keys(limited), want)
	}
}

func TestIntegrationRuleTestPurposeAndDirectionEvidenceAreReturned(t *testing.T) {
	pool := newTestPool(t)
	ctx := context.Background()
	dataset := insertScratchDataset(t, pool, true, false, []scratchPoint{
		{entity: "e:rule", raw: "RULE-TEST-POINT", lon: 30, lat: 30, direction: "unknown", validity: "unknown"},
		{entity: "e:neighbor", raw: "RULE-TEST-NEIGHBOR", lon: 30.0001, lat: 30, direction: "unknown", validity: "unknown"},
	})
	if _, err := pool.Exec(ctx, `UPDATE regulation_datasets SET provenance='{"data_purpose":"rule_test"}'::jsonb WHERE dataset_id=$1`, dataset); err != nil {
		t.Fatal(err)
	}
	if _, err := pool.Exec(ctx, `UPDATE regulation_points SET
		direction_degrees=90, direction_status='inferred', imputation_method='nearest_stop_reference',
		imputation_reason='topology_proxy_unverified', direction_source='nearest_stop',
		direction_evidence=jsonb_build_object(
			'schema_version', 2, 'kind', 'nearest_stop_reference',
			'source_csv_sha256', repeat('a', 64), 'method_version', 'nearest-stop-proxy-v1',
			'direction_semantics', 'stop_to_neighbor_proxy', 'neighbor_entity_id', 'e:neighbor', 'candidate_rank', 1),
		validity_reason='source_validity_unverified'
		WHERE dataset_id=$1 AND raw_uid='RULE-TEST-POINT'`, dataset); err != nil {
		t.Fatal(err)
	}

	repo := NewStopPointRepository(pool, dataset)
	detail, found, err := repo.FindByID(ctx, "RULE-TEST-POINT")
	if err != nil || !found {
		t.Fatalf("found=%t err=%v", found, err)
	}
	if detail.DataPurpose != "rule_test" || detail.ImputationMethod != "nearest_stop_reference" ||
		detail.ImputationReason != "topology_proxy_unverified" || detail.DirectionSource != "nearest_stop" ||
		detail.ValidityReason != "source_validity_unverified" || len(detail.DirectionEvidence) == 0 || string(detail.ValidityEvidence) != "{}" {
		t.Fatalf("rule-test detail omitted evidence/purpose: %+v", detail)
	}
	var evidence map[string]any
	if err := json.Unmarshal(detail.DirectionEvidence, &evidence); err != nil || evidence["direction_semantics"] != "stop_to_neighbor_proxy" {
		t.Fatalf("direction evidence is not the stored JSON object: %s err=%v", detail.DirectionEvidence, err)
	}
	rangePoints, err := repo.FindByRange(ctx, domain.GetStopPointsByRangeInput{Latitude: 30, Longitude: 30, RadiusMeters: 1000, Limit: 10})
	if err != nil || len(rangePoints) != 2 || rangePoints[0].DataPurpose != "rule_test" {
		t.Fatalf("range = %+v err=%v", rangePoints, err)
	}
}

func TestIntegrationEmbeddedNagoyaRuleTestDatasetReturnsProvenance(t *testing.T) {
	pool := newTestPool(t)
	ctx := context.Background()
	bundle, err := seed.Load(seeddata.FS(), nagoyaRuleTestDatasetID)
	if err != nil {
		t.Fatalf("load embedded rule_test dataset: %v", err)
	}
	if _, err := seed.Apply(ctx, pool, bundle); err != nil {
		t.Fatalf("apply embedded rule_test dataset: %v", err)
	}

	var rawUID string
	var latitude, longitude float64
	if err := pool.QueryRow(ctx, `SELECT raw_uid, ST_Y(geom::geometry), ST_X(geom::geometry)
		FROM regulation_points WHERE dataset_id=$1 AND direction_status='inferred' ORDER BY entity_id LIMIT 1`,
		nagoyaRuleTestDatasetID).Scan(&rawUID, &latitude, &longitude); err != nil {
		t.Fatalf("select embedded inferred point: %v", err)
	}
	repo := NewStopPointRepository(pool, nagoyaRuleTestDatasetID)
	detail, found, err := repo.FindByID(ctx, rawUID)
	if err != nil || !found {
		t.Fatalf("find embedded rule_test point: found=%t err=%v", found, err)
	}
	if detail.DataPurpose != "rule_test" || detail.DatasetID != nagoyaRuleTestDatasetID ||
		detail.DirectionStatus != "inferred" || detail.ValidityStatus != "unknown" ||
		detail.ImputationMethod != "nearest_stop_reference" || detail.DirectionSource != "nearest_stop" ||
		detail.ValidityReason != "source_validity_unverified" || detail.AZ == nil {
		t.Fatalf("stored rule_test metadata is incomplete: %+v", detail)
	}
	var evidence map[string]any
	if err := json.Unmarshal(detail.DirectionEvidence, &evidence); err != nil ||
		evidence["direction_semantics"] != "stop_to_neighbor_proxy" || evidence["neighbor_entity_id"] == "" {
		t.Fatalf("stored direction evidence is incomplete: %s err=%v", detail.DirectionEvidence, err)
	}

	rangePoints, err := repo.FindByRange(ctx, domain.GetStopPointsByRangeInput{
		Latitude: latitude, Longitude: longitude, RadiusMeters: 1, Limit: 10,
	})
	if err != nil {
		t.Fatalf("range query for embedded rule_test point: %v", err)
	}
	for _, point := range rangePoints {
		if point.UniqueKey == rawUID && point.DataPurpose == "rule_test" &&
			point.DirectionEvidence != nil && string(point.DirectionEvidence) != "{}" {
			return
		}
	}
	t.Fatalf("range result did not preserve rule_test purpose/evidence: %+v", rangePoints)
}

func TestIntegrationDatabaseFailureMapsToUnavailableSentinel(t *testing.T) {
	pool := newTestPool(t)
	dsn := os.Getenv("TEST_DATABASE_URL")
	closed, err := pgxpool.New(context.Background(), dsn)
	if err != nil {
		t.Fatal(err)
	}
	repo := NewStopPointRepository(closed, syntheticDatasetID)
	health := NewHealthRepository(closed, time.Second)
	if err := health.Ping(context.Background()); err != nil {
		t.Fatalf("a live pool must ping: %v", err)
	}
	closed.Close()

	if _, _, err := repo.FindByID(context.Background(), "x"); !errors.Is(err, domain.ErrDatabaseUnavailable) {
		t.Fatalf("FindByID on a closed pool = %v, want ErrDatabaseUnavailable", err)
	}
	if _, err := repo.FindByRange(context.Background(), domain.GetStopPointsByRangeInput{Latitude: 1, Longitude: 1, RadiusMeters: 10, Limit: 1}); !errors.Is(err, domain.ErrDatabaseUnavailable) {
		t.Fatalf("FindByRange on a closed pool = %v, want ErrDatabaseUnavailable", err)
	}
	if err := health.Ping(context.Background()); err == nil {
		t.Fatal("ping on a closed pool must fail")
	}

	// A canceled request context is also reported as unavailable, not as an internal error.
	canceled, cancel := context.WithCancel(context.Background())
	cancel()
	if _, _, err := NewStopPointRepository(pool, syntheticDatasetID).FindByID(canceled, "x"); !errors.Is(err, domain.ErrDatabaseUnavailable) {
		t.Fatalf("FindByID with a canceled context = %v, want ErrDatabaseUnavailable", err)
	}
}

// Latitude and longitude differ here, so swapping the ST_MakePoint arguments
// would return an empty result instead of the nearby points.
func TestIntegrationRangeDistinguishesLatitudeFromLongitude(t *testing.T) {
	pool := newTestPool(t)
	dataset := insertScratchDataset(t, pool, true, false, []scratchPoint{
		{entity: "e:1", raw: "ASYM-1", lon: 100, lat: 10, direction: "unknown", validity: "unknown"},
		{entity: "e:2", raw: "ASYM-2", lon: 100.0005, lat: 10, direction: "unknown", validity: "unknown"},
		{entity: "e:3", raw: "ASYM-FAR", lon: 10, lat: 100 - 90, direction: "unknown", validity: "unknown"},
	})
	repo := NewStopPointRepository(pool, dataset)
	got, err := repo.FindByRange(context.Background(), domain.GetStopPointsByRangeInput{Latitude: 10, Longitude: 100, RadiusMeters: 500, Limit: 500})
	if err != nil {
		t.Fatal(err)
	}
	if want := "[ASYM-1 ASYM-2]"; fmt.Sprint(keys(got)) != want {
		t.Fatalf("keys = %v, want %s", keys(got), want)
	}
	if got[0].Latitude != 10 || got[0].Longitude != 100 {
		t.Fatalf("coordinates = (%v, %v), want lat 10 / long 100", got[0].Latitude, got[0].Longitude)
	}
}

// Raw UIDs sort opposite to entity IDs, so an ORDER BY that omits entity_id
// as the tiebreaker would not reproduce the expected order.
func TestIntegrationRangeTiebreakFollowsEntityIDNotRawUID(t *testing.T) {
	pool := newTestPool(t)
	dataset := insertScratchDataset(t, pool, true, false, []scratchPoint{
		{entity: "e:2", raw: "RAW-A", lon: 40, lat: 40, direction: "unknown", validity: "unknown"},
		{entity: "e:1", raw: "RAW-B", lon: 40, lat: 40, direction: "unknown", validity: "unknown"},
		{entity: "e:3", raw: "RAW-C", lon: 40, lat: 40, direction: "unknown", validity: "unknown"},
	})
	repo := NewStopPointRepository(pool, dataset)
	got, err := repo.FindByRange(context.Background(), domain.GetStopPointsByRangeInput{Latitude: 40, Longitude: 40, RadiusMeters: 100, Limit: 500})
	if err != nil {
		t.Fatal(err)
	}
	if want := "[RAW-B RAW-A RAW-C]"; fmt.Sprint(keys(got)) != want {
		t.Fatalf("order = %v, want %s", keys(got), want)
	}
}
