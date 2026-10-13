package postgres

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"testing"

	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/puddle/v2"

	"github.com/NKC-CV2026/Graduation-research/backend/app/internal/domain"
)

func TestClassifyErrorMapsAvailabilityFailuresToTheSentinel(t *testing.T) {
	unavailable := map[string]error{
		"deadline":         context.DeadlineExceeded,
		"canceled":         context.Canceled,
		"closed pool":      puddle.ErrClosedPool,
		"eof":              io.EOF,
		"unexpected eof":   io.ErrUnexpectedEOF,
		"network":          &net.OpError{Op: "dial", Err: errors.New("connection refused")},
		"connection class": &pgconn.PgError{Code: "08006"},
		"too many":         &pgconn.PgError{Code: "53300"},
		"admin shutdown":   &pgconn.PgError{Code: "57P01"},
		"wrapped":          fmt.Errorf("query: %w", &pgconn.PgError{Code: "57P03"}),
	}
	for name, err := range unavailable {
		t.Run("unavailable/"+name, func(t *testing.T) {
			if got := classifyError(err); !errors.Is(got, domain.ErrDatabaseUnavailable) {
				t.Fatalf("classifyError(%v) = %v, want ErrDatabaseUnavailable", err, got)
			}
		})
	}

	other := map[string]error{
		"syntax":    &pgconn.PgError{Code: "42601"},
		"undefined": &pgconn.PgError{Code: "42P01"},
		"scan":      errors.New("scanning column 3"),
	}
	for name, err := range other {
		t.Run("internal/"+name, func(t *testing.T) {
			got := classifyError(err)
			if errors.Is(got, domain.ErrDatabaseUnavailable) {
				t.Fatalf("classifyError(%v) must not be reported as unavailable", err)
			}
			if !errors.Is(got, err) {
				t.Fatalf("classifyError must keep the original error, got %v", got)
			}
		})
	}

	if classifyError(nil) != nil {
		t.Fatal("classifyError(nil) must be nil")
	}
}
