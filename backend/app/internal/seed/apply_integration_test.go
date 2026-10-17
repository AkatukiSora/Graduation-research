package seed

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
)

// datasetVariant returns an independent copy of the embedded dataset under a
// new identifier with its own digest.
func datasetVariant(t *testing.T, id string) *Bundle {
	t.Helper()
	b := cloneBundle(t, loadEmbedded(t))
	sum := sha256.Sum256([]byte("variant:" + id))
	digest := hex.EncodeToString(sum[:])
	b.ContentSHA256, b.Manifest.ContentSHA256 = digest, digest
	b.Manifest.DatasetID, b.Dataset.DatasetID = id, id
	if err := Validate(b); err != nil {
		t.Fatalf("variant must be valid: %v", err)
	}
	return b
}

type rowCounts struct{ datasets, points, candidates int }

func countsFor(t *testing.T, pool *pgxpool.Pool, id string) rowCounts {
	t.Helper()
	var c rowCounts
	ctx := context.Background()
	for query, dest := range map[string]*int{
		"SELECT count(*) FROM regulation_datasets WHERE dataset_id = $1":   &c.datasets,
		"SELECT count(*) FROM regulation_points WHERE dataset_id = $1":     &c.points,
		"SELECT count(*) FROM regulation_candidates WHERE dataset_id = $1": &c.candidates,
	} {
		if err := pool.QueryRow(ctx, query, id).Scan(dest); err != nil {
			t.Fatalf("%s: %v", query, err)
		}
	}
	return c
}

func legacyCount(t *testing.T, pool *pgxpool.Pool) int {
	t.Helper()
	var n int
	if err := pool.QueryRow(context.Background(), "SELECT count(*) FROM stop_signs").Scan(&n); err != nil {
		t.Fatal(err)
	}
	return n
}

// cleanup removes only the rows of datasets this test created.
func cleanup(t *testing.T, pool *pgxpool.Pool, ids ...string) {
	t.Helper()
	t.Cleanup(func() {
		ctx := context.Background()
		for _, id := range ids {
			for _, table := range []string{"regulation_candidates", "regulation_points", "regulation_datasets"} {
				if _, err := pool.Exec(ctx, "DELETE FROM "+table+" WHERE dataset_id = $1", id); err != nil {
					t.Logf("cleanup %s %s: %v", table, id, err)
				}
			}
		}
	})
}

func TestIntegrationApplyV2LateDecodeRollsBackPointsAndDataset(t *testing.T) {
	pool := testPool(t)
	id := uniqueDatasetID("it-v2-late-decode")
	cleanup(t, pool, id)
	fixture, _ := mixedV2Fixture(t, id)
	stagedFS := lateCandidateFailureFS(t, fixture, id)
	bundle, err := Load(stagedFS, id)
	if err != nil {
		t.Fatalf("Load valid preflight fixture: %v", err)
	}
	if _, err := Apply(context.Background(), pool, bundle); err == nil {
		t.Fatal("Apply accepted a late candidate decode failure")
	}
	if got := countsFor(t, pool, id); got != (rowCounts{}) {
		t.Fatalf("late candidate failure left partial v2 rows: %+v", got)
	}
}

func TestIntegrationApplyV2CRCFailureAfterPointInsertRollsBack(t *testing.T) {
	pool := testPool(t)
	id := uniqueDatasetID("it-v2-crc")
	cleanup(t, pool, id)
	fixture, _ := mixedV2Fixture(t, id)
	stagedFS := lateCandidateCRCFailureFS(t, fixture, id)
	bundle, err := Load(stagedFS, id)
	if err != nil {
		t.Fatalf("Load CRC fixture: %v", err)
	}
	if _, err := Apply(context.Background(), pool, bundle); err == nil {
		t.Fatal("Apply accepted a late gzip CRC failure")
	}
	if stagedFS.targetOpens != 3 {
		t.Fatalf("candidate shard opened %d times, want CRC failure during insertion scan", stagedFS.targetOpens)
	}
	if got := countsFor(t, pool, id); got != (rowCounts{}) {
		t.Fatalf("CRC failure left partial v2 rows: %+v", got)
	}
}

func TestIntegrationApplyV2CancellationAfterPointInsertRollsBack(t *testing.T) {
	pool := testPool(t)
	id := uniqueDatasetID("it-v2-cancel")
	cleanup(t, pool, id)
	fixture, _ := mixedV2Fixture(t, id)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	stagedFS := &stagedCandidateFS{
		validFS: fixture, lateFS: fixture, target: id + "/candidates-00001.jsonl.gz", onLateOpen: cancel,
	}
	bundle, err := Load(stagedFS, id)
	if err != nil {
		t.Fatalf("Load cancellation fixture: %v", err)
	}
	if _, err := Apply(ctx, pool, bundle); !errors.Is(err, context.Canceled) {
		t.Fatalf("Apply cancellation error = %v, want context.Canceled", err)
	}
	if stagedFS.targetOpens != 3 {
		t.Fatalf("candidate shard opened %d times, want cancellation during insertion scan", stagedFS.targetOpens)
	}
	if got := countsFor(t, pool, id); got != (rowCounts{}) {
		t.Fatalf("cancellation left partial v2 rows: %+v", got)
	}
}

func TestIntegrationApplyV2DatabaseFailureRollsBackEverything(t *testing.T) {
	pool := testPool(t)
	id := uniqueDatasetID("it-v2-db-failure")
	cleanup(t, pool, id)
	objectSuffix := strings.ReplaceAll(id, "-", "_")
	if len(objectSuffix) > 32 {
		objectSuffix = objectSuffix[len(objectSuffix)-32:]
	}
	functionName := "test_v2_candidate_fail_" + objectSuffix
	triggerName := "test_v2_candidate_fail_" + objectSuffix
	ctx := context.Background()
	if _, err := pool.Exec(ctx, "CREATE FUNCTION public."+functionName+`() RETURNS trigger LANGUAGE plpgsql AS $body$
BEGIN
  RAISE EXCEPTION 'injected candidate storage failure' USING ERRCODE = '23514';
END;
$body$`); err != nil {
		t.Fatalf("create candidate failure function: %v", err)
	}
	if _, err := pool.Exec(ctx, "CREATE TRIGGER "+triggerName+" BEFORE INSERT ON regulation_candidates FOR EACH ROW EXECUTE FUNCTION public."+functionName+"()"); err != nil {
		_, _ = pool.Exec(ctx, "DROP FUNCTION public."+functionName+"()")
		t.Fatalf("create candidate failure trigger: %v", err)
	}
	t.Cleanup(func() {
		_, _ = pool.Exec(context.Background(), "DROP TRIGGER IF EXISTS "+triggerName+" ON regulation_candidates")
		_, _ = pool.Exec(context.Background(), "DROP FUNCTION IF EXISTS public."+functionName+"()")
	})
	fixture, _ := mixedV2Fixture(t, id)
	bundle, err := Load(fixture, id)
	if err != nil {
		t.Fatalf("Load database-failure fixture: %v", err)
	}
	if _, err := Apply(ctx, pool, bundle); err == nil {
		t.Fatal("Apply succeeded despite the injected candidate database failure")
	} else {
		var pgErr *pgconn.PgError
		if !errors.As(err, &pgErr) || pgErr.Code != "23514" {
			t.Fatalf("Apply error = %v, want injected check violation (23514)", err)
		}
	}
	if got := countsFor(t, pool, id); got != (rowCounts{}) {
		t.Fatalf("database failure left partial v2 rows: %+v", got)
	}
}

func TestIntegrationApplyV2MixedEvidenceStatesAndReplay(t *testing.T) {
	pool := testPool(t)
	ctx := context.Background()
	id := uniqueDatasetID("it-v2-mixed")
	cleanup(t, pool, id)
	fixture, _ := mixedV2Fixture(t, id)
	bundle, err := Load(fixture, id)
	if err != nil {
		t.Fatalf("Load mixed v2 fixture: %v", err)
	}
	if err := Validate(bundle); err != nil {
		t.Fatalf("Validate mixed v2 fixture: %v", err)
	}
	inserted, err := Apply(ctx, pool, bundle)
	if err != nil || inserted.Status != StatusInserted || inserted.Points != 12 || inserted.Candidates != 58 {
		t.Fatalf("Apply mixed v2 fixture = %+v, %v", inserted, err)
	}
	unchanged, err := Apply(ctx, pool, bundle)
	if err != nil || unchanged.Status != StatusUnchanged {
		t.Fatalf("replay mixed v2 fixture = %+v, %v", unchanged, err)
	}
	var unknownDirections, inferredDirections, confirmedDirections, unknownValidity, valid, invalid int
	if err := pool.QueryRow(ctx, `SELECT count(*) FILTER (WHERE direction_status='unknown'),
		count(*) FILTER (WHERE direction_status='inferred'), count(*) FILTER (WHERE direction_status='confirmed'),
		count(*) FILTER (WHERE validity_status='unknown'), count(*) FILTER (WHERE validity_status='valid'),
		count(*) FILTER (WHERE validity_status='invalid') FROM regulation_points WHERE dataset_id=$1`, id).
		Scan(&unknownDirections, &inferredDirections, &confirmedDirections, &unknownValidity, &valid, &invalid); err != nil {
		t.Fatal(err)
	}
	if unknownDirections != 9 || inferredDirections != 2 || confirmedDirections != 1 || unknownValidity != 10 || valid != 1 || invalid != 1 {
		t.Fatalf("stored mixed states = direction %d/%d/%d validity %d/%d/%d", unknownDirections, inferredDirections, confirmedDirections, unknownValidity, valid, invalid)
	}
	if _, err := pool.Exec(ctx, `UPDATE regulation_points SET direction_evidence=jsonb_set(direction_evidence, '{entrance,lon}', '1'::jsonb)
		WHERE dataset_id=$1 AND imputation_method='source_entrance_geometry'`, id); err != nil {
		t.Fatalf("tamper stored v2 evidence: %v", err)
	}
	if _, err := Apply(ctx, pool, bundle); err == nil {
		t.Fatal("replay accepted changed stored evidence")
	}
	if got := countsFor(t, pool, id); got != (rowCounts{datasets: 1, points: 12, candidates: 58}) {
		t.Fatalf("stored state mismatch changed dataset counts: %+v", got)
	}
}

func TestIntegrationApplyInsertsAndReplaysAsNoop(t *testing.T) {
	pool := testPool(t)
	ctx := context.Background()
	id := uniqueDatasetID("it-apply")
	cleanup(t, pool, id)
	b := datasetVariant(t, id)
	legacyBefore := legacyCount(t, pool)

	res, err := Apply(ctx, pool, b)
	if err != nil {
		t.Fatalf("first apply: %v", err)
	}
	if res.Status != StatusInserted || res.Points != 12 || res.Candidates != len(b.Dataset.Candidates) {
		t.Fatalf("first apply result = %+v", res)
	}
	want := rowCounts{1, 12, len(b.Dataset.Candidates)}
	if got := countsFor(t, pool, id); got != want {
		t.Fatalf("counts = %+v, want %+v", got, want)
	}

	// Flags and uncertainty are visible at database level.
	var synthetic, approved bool
	var digest string
	if err := pool.QueryRow(ctx, `SELECT synthetic, production_approved, content_sha256 FROM regulation_datasets WHERE dataset_id=$1`, id).
		Scan(&synthetic, &approved, &digest); err != nil {
		t.Fatal(err)
	}
	if !synthetic || approved || digest != b.ContentSHA256 {
		t.Fatalf("dataset row synthetic=%v approved=%v digest=%s", synthetic, approved, digest)
	}
	var unknown int
	if err := pool.QueryRow(ctx, `SELECT count(*) FROM regulation_points WHERE dataset_id=$1 AND direction_degrees IS NULL
		AND direction_status='unknown' AND validity_status='unknown'`, id).Scan(&unknown); err != nil || unknown != 12 {
		t.Fatalf("unknown points = %d, err %v", unknown, err)
	}
	var approvedCandidates int
	if err := pool.QueryRow(ctx, `SELECT count(*) FROM regulation_candidates WHERE dataset_id=$1
		AND (production_approved OR bearing_semantics <> 'candidate_point_neighbor_only')`, id).Scan(&approvedCandidates); err != nil || approvedCandidates != 0 {
		t.Fatalf("approved/unsemantic candidates = %d, err %v", approvedCandidates, err)
	}
	// Stored coordinates answer a geographic query.
	var near int
	if err := pool.QueryRow(ctx, `SELECT count(*) FROM regulation_points WHERE dataset_id=$1
		AND ST_DWithin(geom, ST_SetSRID(ST_MakePoint(1.0, 1.0), 4326)::geography, 150)`, id).Scan(&near); err != nil || near != 10 {
		t.Fatalf("points within 150 m = %d, err %v", near, err)
	}
	var createdAt time.Time
	if err := pool.QueryRow(ctx, `SELECT created_at FROM regulation_datasets WHERE dataset_id=$1`, id).Scan(&createdAt); err != nil {
		t.Fatal(err)
	}

	replay, err := Apply(ctx, pool, b)
	if err != nil {
		t.Fatalf("replay: %v", err)
	}
	if replay.Status != StatusUnchanged || replay.Points != 12 {
		t.Fatalf("replay result = %+v", replay)
	}
	if got := countsFor(t, pool, id); got != want {
		t.Fatalf("counts after replay = %+v, want %+v", got, want)
	}
	var createdAgain time.Time
	if err := pool.QueryRow(ctx, `SELECT created_at FROM regulation_datasets WHERE dataset_id=$1`, id).Scan(&createdAgain); err != nil || !createdAgain.Equal(createdAt) {
		t.Fatalf("replay rewrote the dataset row: %v vs %v (%v)", createdAgain, createdAt, err)
	}
	if legacyCount(t, pool) != legacyBefore {
		t.Fatal("legacy stop_signs changed")
	}
}

func TestIntegrationApplyEmbeddedDataset(t *testing.T) {
	pool := testPool(t)
	ctx := context.Background()
	b := loadEmbedded(t)
	res, err := Apply(ctx, pool, b)
	if err != nil {
		t.Fatalf("apply embedded: %v", err)
	}
	if res.Status == StatusInserted {
		cleanup(t, pool, b.Manifest.DatasetID)
	}
	if got := countsFor(t, pool, b.Manifest.DatasetID); got.points != b.Manifest.Counts.Points || got.candidates != b.Manifest.Counts.Candidates || got.datasets != 1 {
		t.Fatalf("embedded counts = %+v", got)
	}
	again, err := Apply(ctx, pool, b)
	if err != nil || again.Status != StatusUnchanged {
		t.Fatalf("embedded replay = %+v, %v", again, err)
	}
}

func TestIntegrationDigestConflictIsRejectedWithoutWrites(t *testing.T) {
	pool := testPool(t)
	ctx := context.Background()
	id := uniqueDatasetID("it-conflict")
	cleanup(t, pool, id)
	first := datasetVariant(t, id)
	if _, err := Apply(ctx, pool, first); err != nil {
		t.Fatal(err)
	}
	before := countsFor(t, pool, id)

	changed := datasetVariant(t, id)
	sum := sha256.Sum256([]byte("different content"))
	changed.ContentSHA256 = hex.EncodeToString(sum[:])
	changed.Manifest.ContentSHA256 = changed.ContentSHA256
	changed.Dataset.Points = changed.Dataset.Points[:11]
	changed.Dataset.Candidates = nil
	changed.Manifest.Counts.Points, changed.Manifest.Counts.Candidates = 11, 0

	_, err := Apply(ctx, pool, changed)
	if !errors.Is(err, ErrDigestConflict) {
		t.Fatalf("err = %v, want ErrDigestConflict", err)
	}
	if got := countsFor(t, pool, id); got != before {
		t.Fatalf("counts changed on conflict: %+v -> %+v", before, got)
	}
	var digest string
	if err := pool.QueryRow(ctx, `SELECT content_sha256 FROM regulation_datasets WHERE dataset_id=$1`, id).Scan(&digest); err != nil || digest != first.ContentSHA256 {
		t.Fatalf("stored digest = %s (%v), want original", digest, err)
	}
}

func TestIntegrationSameDigestUnderAnotherIDIsRejected(t *testing.T) {
	pool := testPool(t)
	ctx := context.Background()
	idA, idB := uniqueDatasetID("it-digest-a"), uniqueDatasetID("it-digest-b")
	cleanup(t, pool, idA, idB)
	a := datasetVariant(t, idA)
	if _, err := Apply(ctx, pool, a); err != nil {
		t.Fatal(err)
	}
	b := datasetVariant(t, idB)
	b.ContentSHA256, b.Manifest.ContentSHA256 = a.ContentSHA256, a.ContentSHA256
	_, err := Apply(ctx, pool, b)
	if !errors.Is(err, ErrDigestInUse) {
		t.Fatalf("err = %v, want ErrDigestInUse", err)
	}
	if got := countsFor(t, pool, idB); got != (rowCounts{}) {
		t.Fatalf("rows left behind: %+v", got)
	}
}

func TestIntegrationInvalidSeedWritesNothing(t *testing.T) {
	pool := testPool(t)
	ctx := context.Background()
	id := uniqueDatasetID("it-invalid")
	cleanup(t, pool, id)
	b := datasetVariant(t, id)
	b.Dataset.Candidates[0].NeighborEntityID = "missing-point"

	_, err := Apply(ctx, pool, b)
	var verr *ValidationError
	if !errors.As(err, &verr) || verr.Code != CodeCandidateRef {
		t.Fatalf("err = %v, want candidate reference validation error", err)
	}
	if got := countsFor(t, pool, id); got != (rowCounts{}) {
		t.Fatalf("rows written for an invalid seed: %+v", got)
	}
}

func TestIntegrationDatabaseErrorRollsBackEverything(t *testing.T) {
	pool := testPool(t)
	ctx := context.Background()
	id := uniqueDatasetID("it-rollback-fk")
	cleanup(t, pool, id)
	b := datasetVariant(t, id)
	// Skip pre-validation so the foreign key rejects the last candidate after
	// the dataset, all points and most candidates have been inserted.
	last := len(b.Dataset.Candidates) - 1
	b.Dataset.Candidates[last].NeighborEntityID = "missing-point"

	_, err := applier{skipValidation: true}.apply(ctx, pool, b)
	if err == nil {
		t.Fatal("apply succeeded despite a dangling candidate")
	}
	var pgErr *pgconn.PgError
	if !errors.As(err, &pgErr) || pgErr.Code != "23503" {
		t.Fatalf("err = %v, want foreign key violation (23503)", err)
	}
	if got := countsFor(t, pool, id); got != (rowCounts{}) {
		t.Fatalf("partial rows survived rollback: %+v", got)
	}
}

func TestIntegrationFailureAfterPointsRollsBack(t *testing.T) {
	pool := testPool(t)
	ctx := context.Background()
	id := uniqueDatasetID("it-rollback-hook")
	cleanup(t, pool, id)
	b := datasetVariant(t, id)
	injected := errors.New("injected failure")
	_, err := applier{afterPoints: func() error { return injected }}.apply(ctx, pool, b)
	if !errors.Is(err, injected) {
		t.Fatalf("err = %v, want injected failure", err)
	}
	if got := countsFor(t, pool, id); got != (rowCounts{}) {
		t.Fatalf("rows survived rollback: %+v", got)
	}
	// The same dataset can still be applied afterwards.
	if res, err := Apply(ctx, pool, b); err != nil || res.Status != StatusInserted {
		t.Fatalf("apply after rollback = %+v, %v", res, err)
	}
}

func TestIntegrationCountMismatchIsNotCommitted(t *testing.T) {
	pool := testPool(t)
	ctx := context.Background()
	id := uniqueDatasetID("it-count")
	cleanup(t, pool, id)
	b := datasetVariant(t, id)
	b.Manifest.Counts.Candidates++ // manifest promises one more candidate than the data holds

	_, err := applier{skipValidation: true}.apply(ctx, pool, b)
	if !errors.Is(err, ErrCountMismatch) {
		t.Fatalf("err = %v, want ErrCountMismatch", err)
	}
	if got := countsFor(t, pool, id); got != (rowCounts{}) {
		t.Fatalf("rows committed despite count mismatch: %+v", got)
	}
}

func TestIntegrationConcurrentAppliesInsertExactlyOnce(t *testing.T) {
	pool := testPool(t)
	ctx := context.Background()
	id := uniqueDatasetID("it-concurrent")
	cleanup(t, pool, id)
	b := datasetVariant(t, id)

	const workers = 6
	statuses := make([]string, workers)
	errs := make([]error, workers)
	var wg sync.WaitGroup
	start := make(chan struct{})
	for i := 0; i < workers; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			<-start
			res, err := Apply(ctx, pool, b)
			statuses[i], errs[i] = res.Status, err
		}(i)
	}
	close(start)
	wg.Wait()

	inserted := 0
	for i := range statuses {
		if errs[i] != nil {
			t.Fatalf("worker %d: %v", i, errs[i])
		}
		switch statuses[i] {
		case StatusInserted:
			inserted++
		case StatusUnchanged:
		default:
			t.Fatalf("worker %d status %q", i, statuses[i])
		}
	}
	if inserted != 1 {
		t.Fatalf("inserted by %d workers, want exactly 1 (%v)", inserted, statuses)
	}
	if got, want := countsFor(t, pool, id), (rowCounts{1, 12, len(b.Dataset.Candidates)}); got != want {
		t.Fatalf("counts = %+v, want %+v", got, want)
	}
}

func TestIntegrationOtherDatasetsAndLegacyTableAreUntouched(t *testing.T) {
	pool := testPool(t)
	ctx := context.Background()
	idA, idB := uniqueDatasetID("it-other-a"), uniqueDatasetID("it-other-b")
	cleanup(t, pool, idA, idB)
	if _, err := Apply(ctx, pool, datasetVariant(t, idA)); err != nil {
		t.Fatal(err)
	}
	beforeA := countsFor(t, pool, idA)
	var digestBefore string
	if err := pool.QueryRow(ctx, `SELECT content_sha256 FROM regulation_datasets WHERE dataset_id=$1`, idA).Scan(&digestBefore); err != nil {
		t.Fatal(err)
	}
	legacyBefore := legacyCount(t, pool)

	if _, err := Apply(ctx, pool, datasetVariant(t, idB)); err != nil {
		t.Fatal(err)
	}
	if got := countsFor(t, pool, idA); got != beforeA {
		t.Fatalf("dataset A changed: %+v -> %+v", beforeA, got)
	}
	var digestAfter string
	if err := pool.QueryRow(ctx, `SELECT content_sha256 FROM regulation_datasets WHERE dataset_id=$1`, idA).Scan(&digestAfter); err != nil || digestAfter != digestBefore {
		t.Fatalf("dataset A digest changed: %s -> %s (%v)", digestBefore, digestAfter, err)
	}
	if legacyCount(t, pool) != legacyBefore {
		t.Fatal("legacy stop_signs changed")
	}
}
