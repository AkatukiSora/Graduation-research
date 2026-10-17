package db

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/NKC-CV2026/Graduation-research/backend/app/internal/config"
)

func testConfig() config.Config {
	return config.Config{
		AWSRegion: "ap-northeast-3",
		DB: config.DBConfig{
			Host: "127.0.0.1", Port: 5432, Name: "app", User: "api", SSLMode: "disable",
			MaxConns: 2, MaxConnLifetime: 10 * time.Minute, MaxConnIdleTime: 2 * time.Minute,
		},
	}
}

func TestBeforeConnectResolvesPasswordForEveryNewConnection(t *testing.T) {
	calls := 0
	resolver := func(context.Context) (string, error) {
		calls++
		return fmt.Sprintf("token-%d", calls), nil
	}
	poolConfig, err := newPoolConfig(testConfig(), resolver)
	if err != nil {
		t.Fatal(err)
	}
	if poolConfig.ConnConfig.Password != "" {
		t.Fatal("the pool must not carry a fixed password; it is resolved per connection")
	}
	if poolConfig.BeforeConnect == nil {
		t.Fatal("BeforeConnect must be installed")
	}
	for i := 1; i <= 3; i++ {
		connConfig := poolConfig.ConnConfig.Copy()
		if err := poolConfig.BeforeConnect(context.Background(), connConfig); err != nil {
			t.Fatal(err)
		}
		if want := fmt.Sprintf("token-%d", i); connConfig.Password != want {
			t.Fatalf("connection %d password = %q, want %q", i, connConfig.Password, want)
		}
	}
	if calls != 3 {
		t.Fatalf("resolver called %d times, want 3", calls)
	}
}

func TestBeforeConnectPropagatesResolverErrorWithoutLeakingIt(t *testing.T) {
	sentinel := errors.New("sts unavailable")
	poolConfig, err := newPoolConfig(testConfig(), func(context.Context) (string, error) {
		return "super-secret-token", sentinel
	})
	if err != nil {
		t.Fatal(err)
	}
	connConfig := poolConfig.ConnConfig.Copy()
	err = poolConfig.BeforeConnect(context.Background(), connConfig)
	if !errors.Is(err, sentinel) {
		t.Fatalf("error = %v, want it to wrap the resolver error", err)
	}
	if strings.Contains(err.Error(), "super-secret-token") {
		t.Fatal("the password must not appear in the error")
	}
	if connConfig.Password != "" {
		t.Fatal("a failed resolution must not set a password")
	}
}

func TestBeforeConnectPassesContextToResolver(t *testing.T) {
	type key struct{}
	ctx := context.WithValue(context.Background(), key{}, "marker")
	var seen any
	poolConfig, err := newPoolConfig(testConfig(), func(ctx context.Context) (string, error) {
		seen = ctx.Value(key{})
		return "p", nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := poolConfig.BeforeConnect(ctx, &pgx.ConnConfig{}); err != nil {
		t.Fatal(err)
	}
	if seen != "marker" {
		t.Fatal("the connection context must reach the resolver")
	}
}

func TestStaticPasswordResolverNeverCallsAWS(t *testing.T) {
	cfg := testConfig()
	cfg.DB.Password = "static-password"
	cfg.DB.IAMAuth = true
	for i := 0; i < 2; i++ {
		got, err := NewPasswordResolver(cfg)(context.Background())
		if err != nil || got != "static-password" {
			t.Fatalf("resolver = (%q, %v)", got, err)
		}
	}
	// ResolvePassword keeps working for the migration and seed commands.
	if got, err := ResolvePassword(context.Background(), cfg); err != nil || got != "static-password" {
		t.Fatalf("ResolvePassword = (%q, %v)", got, err)
	}
}

func TestResolverRequiresPasswordWhenIAMIsDisabled(t *testing.T) {
	cfg := testConfig()
	cfg.DB.IAMAuth = false
	if _, err := NewPasswordResolver(cfg)(context.Background()); err == nil || !strings.Contains(err.Error(), "DB_PASSWORD") {
		t.Fatalf("error = %v, want a DB_PASSWORD error", err)
	}
}
