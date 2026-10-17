package postgres

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"

	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/puddle/v2"

	"github.com/NKC-CV2026/Graduation-research/backend/app/internal/domain"
)

// classifyError reports availability failures (connection, timeout, shutdown,
// resource exhaustion, closed pool) as domain.ErrDatabaseUnavailable while
// keeping the original error in the chain. Every other error is returned
// unchanged and is treated as an internal error by callers.
func classifyError(err error) error {
	if err == nil {
		return nil
	}
	if isUnavailable(err) {
		if errors.Is(err, domain.ErrDatabaseUnavailable) {
			return err
		}
		return fmt.Errorf("%w: %w", domain.ErrDatabaseUnavailable, err)
	}

	return err
}

func isUnavailable(err error) bool {
	switch {
	case errors.Is(err, domain.ErrDatabaseUnavailable),
		errors.Is(err, context.DeadlineExceeded),
		errors.Is(err, context.Canceled),
		errors.Is(err, puddle.ErrClosedPool),
		errors.Is(err, io.EOF),
		errors.Is(err, io.ErrUnexpectedEOF):
		return true
	}

	var netErr net.Error
	if errors.As(err, &netErr) {
		return true
	}

	var connectErr *pgconn.ConnectError
	if errors.As(err, &connectErr) {
		return true
	}

	var pgErr *pgconn.PgError
	if errors.As(err, &pgErr) && len(pgErr.Code) >= 2 {
		switch pgErr.Code[:2] {
		case "08", "53", "57", "58": // connection, resources, operator intervention, system
			return true
		}
	}

	return false
}
