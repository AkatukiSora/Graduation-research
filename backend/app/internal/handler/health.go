package handler

import (
	"encoding/json"
	"net/http"

	"github.com/NKC-CV2026/Graduation-research/backend/app/internal/service"
)

type HealthHandlers struct {
	service *service.HealthService
}

func NewHealthHandlers(service *service.HealthService) *HealthHandlers {
	return &HealthHandlers{service: service}
}

func (h *HealthHandlers) Get(w http.ResponseWriter, r *http.Request) {
	result := h.service.Check(r.Context())

	statusCode := http.StatusOK
	if result.Status != service.HealthStatusOK {
		statusCode = http.StatusServiceUnavailable
	}

	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(statusCode)
	if err := json.NewEncoder(w).Encode(result); err != nil {
		http.Error(w, http.StatusText(http.StatusInternalServerError), http.StatusInternalServerError)
	}
}
