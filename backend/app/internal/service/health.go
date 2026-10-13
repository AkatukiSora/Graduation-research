package service

import (
	"context"

	"github.com/NKC-CV2026/Graduation-research/backend/app/internal/repository"
)

type HealthStatus string

const (
	HealthStatusOK       HealthStatus = "ok"
	HealthStatusDegraded HealthStatus = "degraded"

	databaseOK          = "ok"
	databaseUnavailable = "unavailable"
)

type HealthResult struct {
	Status   HealthStatus `json:"status"`
	Database string       `json:"database"`
}

type HealthService struct {
	repository repository.HealthRepository
}

func NewHealthService(repository repository.HealthRepository) *HealthService {
	return &HealthService{repository: repository}
}

// Check reports the real database state. The cause of a failure is not part of
// the result so no connection detail leaves the service.
func (s *HealthService) Check(ctx context.Context) HealthResult {
	if err := s.repository.Ping(ctx); err != nil {
		return HealthResult{Status: HealthStatusDegraded, Database: databaseUnavailable}
	}

	return HealthResult{Status: HealthStatusOK, Database: databaseOK}
}
