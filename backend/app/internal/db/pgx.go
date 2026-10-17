package db

import (
	"context"
	"fmt"
	"net/url"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/NKC-CV2026/Graduation-research/backend/app/internal/config"
	"github.com/NKC-CV2026/Graduation-research/backend/app/internal/domain"
)

// NewPool creates a pool whose password is resolved for every new connection.
func NewPool(ctx context.Context, cfg config.Config) (*pgxpool.Pool, error) {
	return NewPoolWithResolver(ctx, cfg, NewPasswordResolver(cfg))
}

// NewPoolWithResolver is NewPool with an explicit password resolver.
func NewPoolWithResolver(ctx context.Context, cfg config.Config, resolve PasswordResolver) (*pgxpool.Pool, error) {
	poolConfig, err := newPoolConfig(cfg, resolve)
	if err != nil {
		return nil, err
	}

	pool, err := pgxpool.NewWithConfig(ctx, poolConfig)
	if err != nil {
		return nil, fmt.Errorf("create pgx pool: %w", err)
	}

	return pool, nil
}

// newPoolConfig parses the connection settings without a password and installs
// a BeforeConnect hook that resolves the password for every new connection, so
// short-lived tokens are never reused after they expire. The resolved value is
// never logged or included in returned errors.
func newPoolConfig(cfg config.Config, resolve PasswordResolver) (*pgxpool.Config, error) {
	poolConfig, err := pgxpool.ParseConfig(buildDSN(cfg.DB, ""))
	if err != nil {
		return nil, fmt.Errorf("parse pgx config: %w", err)
	}

	poolConfig.MaxConns = cfg.DB.MaxConns
	poolConfig.MinConns = cfg.DB.MinConns
	poolConfig.MaxConnLifetime = cfg.DB.MaxConnLifetime
	poolConfig.MaxConnIdleTime = cfg.DB.MaxConnIdleTime
	poolConfig.BeforeConnect = func(ctx context.Context, connConfig *pgx.ConnConfig) error {
		password, err := resolve(ctx)
		if err != nil {
			return fmt.Errorf("resolve database password: %w: %w", domain.ErrDatabaseUnavailable, err)
		}
		connConfig.Password = password
		return nil
	}

	return poolConfig, nil
}

func BuildMigrationURL(cfg config.DBConfig, password string) string {
	return buildDSN(cfg, password)
}

func buildDSN(cfg config.DBConfig, password string) string {
	user := url.QueryEscape(cfg.User)
	pass := url.QueryEscape(password)
	name := url.QueryEscape(cfg.Name)
	sslMode := url.QueryEscape(cfg.SSLMode)

	return fmt.Sprintf("postgres://%s:%s@%s:%d/%s?sslmode=%s", user, pass, cfg.Host, cfg.Port, name, sslMode)
}
