// Command seed validates embedded regulation datasets and, on explicit
// request, writes a dataset to the configured PostgreSQL database. The isolated
// test-only direction dataset requires an additional explicit opt-in flag.
//
//	seed --validate-only [--dataset ID]   check regulation data; no database needed
//	seed --apply --dataset ID             insert regulation data in one transaction
//	seed --experimental-directions --validate-only --dataset ID
//	seed --experimental-directions --apply --dataset ID
//
// The API binary never runs this command.
package main

import (
	"context"
	"encoding/json"
	"errors"
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
	"github.com/NKC-CV2026/Graduation-research/backend/app/internal/seedexperiment"
)

type environment struct {
	fsys              fs.FS
	apply             func(ctx context.Context, b *seed.Bundle) (seed.Result, error)
	experimentalFS    fs.FS
	applyExperimental func(ctx context.Context, b *seedexperiment.Bundle) (seedexperiment.Result, error)
}

type report struct {
	DatasetID     string `json:"dataset_id"`
	Mode          string `json:"mode"`
	Status        string `json:"status"`
	Points        int    `json:"points"`
	Candidates    int    `json:"candidates"`
	Rows          int    `json:"rows,omitempty"`
	Inferred      int    `json:"inferred,omitempty"`
	Unknown       int    `json:"unknown,omitempty"`
	ContentSHA256 string `json:"content_sha256"`
}

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	os.Exit(run(ctx, os.Args[1:], os.Stdout, os.Stderr, environment{
		fsys: seeddata.FS(), apply: applyToConfiguredDatabase,
		experimentalFS: seedexperiment.FS(), applyExperimental: applyExperimentalToConfiguredDatabase,
	}))
}

func run(ctx context.Context, args []string, stdout, stderr io.Writer, env environment) int {
	flags := flag.NewFlagSet("seed", flag.ContinueOnError)
	flags.SetOutput(stderr)
	validateOnly := flags.Bool("validate-only", false, "validate the embedded dataset without a database")
	apply := flags.Bool("apply", false, "insert the dataset into the configured database")
	experimentalDirections := flags.Bool("experimental-directions", false, "use the isolated test-only direction dataset")
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
	if *experimentalDirections {
		return runExperimental(ctx, *validateOnly, *apply, *datasetID, stdout, stderr, env)
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
			Points: bundle.Manifest.Counts.Points, Candidates: bundle.Manifest.Counts.Candidates,
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

func runExperimental(ctx context.Context, validateOnly, apply bool, datasetID string, stdout, stderr io.Writer, env environment) int {
	if datasetID == "" {
		return usage(stderr, "--experimental-directions requires an explicit --dataset")
	}
	if datasetID != seedexperiment.DatasetID {
		return failure(stderr, fmt.Errorf("unknown experimental direction dataset %q", datasetID))
	}
	bundle, err := seedexperiment.Load(env.experimentalFS, datasetID)
	if err != nil {
		return failure(stderr, err)
	}
	result := seedexperiment.Result{
		Status: "valid", Rows: bundle.Manifest.Rows,
		Inferred: bundle.Manifest.Counts.Teacher.Inferred + bundle.Manifest.Counts.Target.Inferred,
		Unknown:  bundle.Manifest.Counts.Teacher.Unknown + bundle.Manifest.Counts.Target.Unknown,
	}
	mode := "validate-only"
	if apply {
		if env.applyExperimental == nil {
			return failure(stderr, errors.New("experimental apply is unavailable"))
		}
		result, err = env.applyExperimental(ctx, bundle)
		if err != nil {
			return failure(stderr, err)
		}
		mode = "apply"
	}
	out := report{
		DatasetID: datasetID, Mode: mode, Status: result.Status, Rows: result.Rows,
		Inferred: result.Inferred, Unknown: result.Unknown, ContentSHA256: bundle.ContentSHA256,
	}
	line, _ := json.Marshal(out)
	fmt.Fprintln(stdout, string(line))
	return 0
}

func usage(stderr io.Writer, message string) int {
	fmt.Fprintf(stderr, "seed: %s\nusage: seed --validate-only [--dataset ID] | seed --apply --dataset ID | seed --experimental-directions --validate-only|--apply --dataset ID\n", message)
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

// applyExperimentalToConfiguredDatabase uses the existing application database
// configuration without exposing connection credentials in CLI output.
func applyExperimentalToConfiguredDatabase(ctx context.Context, b *seedexperiment.Bundle) (seedexperiment.Result, error) {
	cfg, err := config.Load()
	if err != nil {
		return seedexperiment.Result{}, err
	}
	pool, err := db.NewPool(ctx, cfg)
	if err != nil {
		return seedexperiment.Result{}, err
	}
	defer pool.Close()
	if err := pool.Ping(ctx); err != nil {
		return seedexperiment.Result{}, fmt.Errorf("ping database: %w", err)
	}
	return seedexperiment.Apply(ctx, pool, b)
}
