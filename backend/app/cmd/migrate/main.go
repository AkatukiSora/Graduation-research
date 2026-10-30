package main

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"os"
	"os/exec"
	"strings"
	"time"

	"github.com/NKC-CV2026/Graduation-research/backend/app/internal/config"
	"github.com/NKC-CV2026/Graduation-research/backend/app/internal/db"
	"github.com/NKC-CV2026/Graduation-research/backend/app/internal/seeddata"
)

const (
	migrationRetryAttempts = 6
	migrationRetryDelay    = 2 * time.Second
	migrationMaxRetryDelay = 30 * time.Second
)

func main() {
	ctx := context.Background()

	if err := runWithRetry(ctx); err != nil {
		fail(err)
	}
}

func runWithRetry(ctx context.Context) error {
	delay := migrationRetryDelay

	for attempt := 1; attempt <= migrationRetryAttempts; attempt++ {
		err := runOnce(ctx)
		if err == nil {
			return nil
		}

		if attempt == migrationRetryAttempts || !isRetryableMigrationError(err) {
			return err
		}

		fmt.Fprintf(os.Stderr, "migration attempt %d failed, retrying in %s: %v\n", attempt, delay, err)

		select {
		case <-time.After(delay):
		case <-ctx.Done():
			return ctx.Err()
		}

		delay *= 2
		if delay > migrationMaxRetryDelay {
			delay = migrationMaxRetryDelay
		}
	}

	return fmt.Errorf("migration failed after %d attempts", migrationRetryAttempts)
}

func runOnce(ctx context.Context) error {
	seedIDs, err := parseSeedDatasetIDs(os.Getenv(seedDatasetsEnv))
	if err != nil {
		return err
	}
	pointsDatasets, bundleIDs, err := loadSeedPoints(seeddata.PointsFS(), seedIDs)
	if err != nil {
		return err
	}
	bundles, err := loadSeedBundles(seeddata.FS(), bundleIDs)
	if err != nil {
		return err
	}

	cfg, err := config.Load()
	if err != nil {
		return err
	}

	pool, err := db.NewPool(ctx, cfg)
	if err != nil {
		return err
	}
	defer pool.Close()

	if err := pool.Ping(ctx); err != nil {
		return fmt.Errorf("ping database before migration: %w", err)
	}

	password, err := db.ResolvePassword(ctx, cfg)
	if err != nil {
		return err
	}

	cmd := exec.CommandContext(
		ctx,
		cfg.DB.AtlasBinaryPath,
		"migrate",
		"apply",
		"--dir",
		cfg.DB.AtlasMigrationDir,
		"--url",
		db.BuildMigrationURL(cfg.DB, password),
	)
	var output bytes.Buffer
	cmd.Stdout = io.MultiWriter(os.Stdout, &output)
	cmd.Stderr = io.MultiWriter(os.Stderr, &output)

	if err := cmd.Run(); err != nil {
		return fmt.Errorf("run atlas migrate apply: %w: %s", err, strings.TrimSpace(output.String()))
	}

	if err := applySeedBundles(ctx, pool, bundles, os.Stdout); err != nil {
		return err
	}
	return applySeedPoints(ctx, pool, pointsDatasets, os.Stdout)
}

func fail(err error) {
	fmt.Fprintln(os.Stderr, err)
	os.Exit(1)
}

func isRetryableMigrationError(err error) bool {
	msg := strings.ToLower(err.Error())
	for _, needle := range []string{
		"connection refused",
		"connection reset by peer",
		"server closed the connection unexpectedly",
		"the database system is starting up",
		"dial tcp",
		"i/o timeout",
		"deadline exceeded",
		"eof",
		"too many connections",
	} {
		if strings.Contains(msg, needle) {
			return true
		}
	}

	return false
}
