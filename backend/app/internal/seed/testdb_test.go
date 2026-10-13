package seed

import (
	"context"
	"fmt"
	"net"
	"os"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
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
			ip := net.ParseIP(host)
			if ip == nil || !ip.IsLoopback() {
				return fmt.Errorf("test database host %q is not loopback or a local socket", host)
			}
		}
	}
	return nil
}

func TestCheckLocalDSN(t *testing.T) {
	good := []string{
		"postgresql://validator@/postgres?host=/tmp/socket&port=5432",
		"postgres://u@127.0.0.1:5432/db",
		"postgres://u@localhost/db",
		"postgres://u@[::1]:5432/db",
	}
	for _, dsn := range good {
		if err := checkLocalDSN(dsn); err != nil {
			t.Errorf("checkLocalDSN(%q): %v", dsn, err)
		}
	}
	bad := []string{
		"postgres://u@db.example.com/db",
		"postgres://u@10.0.0.5:5432/db",
		"postgres://u@gr9.cluster-xyz.ap-northeast-3.rds.amazonaws.com/db",
		"postgres://u@127.0.0.1,db.example.com/db",
		"postgres://u@192.168.1.10/db",
	}
	for _, dsn := range bad {
		if err := checkLocalDSN(dsn); err == nil {
			t.Errorf("checkLocalDSN(%q) accepted a non-local host", dsn)
		}
	}
}

var datasetCounter atomic.Int64

// testPool connects to the disposable PostGIS named by TEST_DATABASE_URL. The
// test is skipped when the variable is unset and fails when it points anywhere
// other than a local socket or loopback address.
func testPool(t *testing.T) *pgxpool.Pool {
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

// uniqueDatasetID returns an identifier that no other test run has used, so
// tests never depend on or disturb rows they did not create.
func uniqueDatasetID(prefix string) string {
	return fmt.Sprintf("%s-%d-%d", prefix, time.Now().UnixNano(), datasetCounter.Add(1))
}
