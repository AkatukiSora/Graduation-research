// Command seed validates the embedded regulation datasets and, on explicit
// request, writes one of them to the configured PostgreSQL database.
//
//	seed --validate-only [--dataset ID]   check embedded data; no database needed
//	seed --apply --dataset ID             insert the dataset in one transaction
//
// The API binary never runs this command.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"io/fs"
	"os"
	"os/signal"
	"syscall"

	"github.com/NKC-CV2026/Graduation-research/backend/app/internal/config"
	"github.com/NKC-CV2026/Graduation-research/backend/app/internal/db"
	"github.com/NKC-CV2026/Graduation-research/backend/app/internal/seed"
	"github.com/NKC-CV2026/Graduation-research/backend/app/internal/seeddata"
)

type environment struct {
	fsys  fs.FS
	apply func(ctx context.Context, b *seed.Bundle) (seed.Result, error)
}

type report struct {
	DatasetID     string `json:"dataset_id"`
	Mode          string `json:"mode"`
	Status        string `json:"status"`
	Points        int    `json:"points"`
	Candidates    int    `json:"candidates"`
	ContentSHA256 string `json:"content_sha256"`
}

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	os.Exit(run(ctx, os.Args[1:], os.Stdout, os.Stderr, environment{fsys: seeddata.FS(), apply: applyToConfiguredDatabase}))
}

func run(ctx context.Context, args []string, stdout, stderr io.Writer, env environment) int {
	flags := flag.NewFlagSet("seed", flag.ContinueOnError)
	flags.SetOutput(stderr)
	validateOnly := flags.Bool("validate-only", false, "validate the embedded dataset without a database")
	apply := flags.Bool("apply", false, "insert the dataset into the configured database")
	datasetID := flags.String("dataset", "", "dataset id (required with --apply)")
	if err := flags.Parse(args); err != nil {
		if err == flag.ErrHelp {
			return 0
		}
		return 2
	}
	switch {
	case flags.NArg() > 0:
		return usage(stderr, "unexpected arguments")
	case *validateOnly == *apply:
		return usage(stderr, "choose exactly one of --validate-only and --apply")
	case *apply && *datasetID == "":
		return usage(stderr, "--apply requires --dataset")
	}

	ids := []string{*datasetID}
	if *datasetID == "" {
		listed, err := seed.DatasetIDs(env.fsys)
		if err != nil || len(listed) == 0 {
			return failure(stderr, fmt.Errorf("no embedded datasets: %v", err))
		}
		ids = listed
	}
	for _, id := range ids {
		bundle, err := seed.Load(env.fsys, id)
		if err == nil {
			err = seed.Validate(bundle)
		}
		if err != nil {
			return failure(stderr, err)
		}
		out := report{
			DatasetID: id, Mode: "validate-only", Status: "valid",
			Points: len(bundle.Dataset.Points), Candidates: len(bundle.Dataset.Candidates),
			ContentSHA256: bundle.ContentSHA256,
		}
		if *apply {
			result, err := env.apply(ctx, bundle)
			if err != nil {
				return failure(stderr, err)
			}
			out.Mode, out.Status, out.Points, out.Candidates = "apply", result.Status, result.Points, result.Candidates
		}
		line, _ := json.Marshal(out)
		fmt.Fprintln(stdout, string(line))
	}
	return 0
}

func usage(stderr io.Writer, message string) int {
	fmt.Fprintf(stderr, "seed: %s\nusage: seed --validate-only [--dataset ID] | seed --apply --dataset ID\n", message)
	return 2
}

func failure(stderr io.Writer, err error) int {
	fmt.Fprintf(stderr, "seed: %v\n", err)
	return 1
}

// applyToConfiguredDatabase connects with the application's database settings
// (never logged) and applies the dataset.
func applyToConfiguredDatabase(ctx context.Context, b *seed.Bundle) (seed.Result, error) {
	cfg, err := config.Load()
	if err != nil {
		return seed.Result{}, err
	}
	pool, err := db.NewPool(ctx, cfg)
	if err != nil {
		return seed.Result{}, err
	}
	defer pool.Close()
	if err := pool.Ping(ctx); err != nil {
		return seed.Result{}, fmt.Errorf("ping database: %w", err)
	}
	return seed.Apply(ctx, pool, b)
}
