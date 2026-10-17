package db

import (
	"context"
	"errors"
	"net"
	"net/url"
	"os"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/NKC-CV2026/Graduation-research/backend/app/internal/domain"
)

// TestIntegrationNewPoolResolvesPasswordPerConnection opens real connections to
// the disposable PostgreSQL named by TEST_DATABASE_TCP_URL (loopback only) and
// checks that the resolver runs for every new connection and that a resolver
// failure surfaces as an unavailable database.
func TestIntegrationNewPoolResolvesPasswordPerConnection(t *testing.T) {
	raw := os.Getenv("TEST_DATABASE_TCP_URL")
	if raw == "" {
		t.Skip("TEST_DATABASE_TCP_URL is not set; integration test skipped")
	}
	u, err := url.Parse(raw)
	if err != nil {
		t.Fatal("parse test database url")
	}
	if ip := net.ParseIP(u.Hostname()); u.Hostname() != "localhost" && (ip == nil || !ip.IsLoopback()) {
		t.Fatalf("refusing to run: host %q is not loopback", u.Hostname())
	}
	port, err := strconv.Atoi(u.Port())
	if err != nil {
		t.Fatal("test database url needs a port")
	}

	cfg := testConfig()
	cfg.DB.Host, cfg.DB.Port = u.Hostname(), port
	cfg.DB.User, cfg.DB.Name = u.User.Username(), strings.TrimPrefix(u.Path, "/")
	cfg.DB.SSLMode, cfg.DB.MaxConns = "disable", 3

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	var calls atomic.Int64
	pool, err := NewPoolWithResolver(ctx, cfg, func(context.Context) (string, error) {
		calls.Add(1)
		return "ignored-by-trust-authentication", nil
	})
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()

	first, err := pool.Acquire(ctx)
	if err != nil {
		t.Fatal(err)
	}
	second, err := pool.Acquire(ctx)
	if err != nil {
		t.Fatal(err)
	}
	first.Release()
	second.Release()
	if got := calls.Load(); got < 2 {
		t.Fatalf("the resolver ran %d times for two simultaneous connections, want at least 2", got)
	}

	failing, err := NewPoolWithResolver(ctx, cfg, func(context.Context) (string, error) {
		return "", errors.New("no credentials")
	})
	if err != nil {
		t.Fatal(err)
	}
	defer failing.Close()
	if err := failing.Ping(ctx); err == nil || !errors.Is(err, domain.ErrDatabaseUnavailable) {
		t.Fatalf("Ping with a failing resolver = %v, want an unavailable-database error", err)
	}
}
