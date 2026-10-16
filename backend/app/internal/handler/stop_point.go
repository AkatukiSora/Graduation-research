package handler

import (
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"net/http"
	"strconv"

	"github.com/go-chi/chi/v5"

	"github.com/NKC-CV2026/Graduation-research/backend/app/internal/domain"
	"github.com/NKC-CV2026/Graduation-research/backend/app/internal/service"
)

const (
	defaultStopPointSearchLimit = 500
	maxStopPointSearchLimit     = 500
)

// Machine-readable error codes. The message keeps its previous wording.
const (
	errorCodeInvalidRequest      = "invalid_request"
	errorCodeNotFound            = "not_found"
	errorCodeDatabaseUnavailable = "database_unavailable"
	errorCodeInternal            = "internal_error"
)

type StopPointHandlers struct {
	service *service.StopPointService
}

// stopPointRangeResponse is one list item. az is null while the direction is unknown.
type stopPointRangeResponse struct {
	UniqueKey         string          `json:"uniqueKey"`
	AZ                *string         `json:"az"`
	Latitude          string          `json:"lat"`
	Longitude         string          `json:"long"`
	DirectionStatus   string          `json:"directionStatus"`
	ValidityStatus    string          `json:"validityStatus"`
	Synthetic         bool            `json:"synthetic"`
	DatasetID         string          `json:"datasetId"`
	SourceMonth       string          `json:"sourceMonth"`
	ImputationMethod  string          `json:"imputationMethod"`
	ImputationReason  string          `json:"imputationReason"`
	DirectionSource   string          `json:"directionSource"`
	DirectionEvidence json.RawMessage `json:"directionEvidence"`
	ValidityReason    string          `json:"validityReason"`
	ValidityEvidence  json.RawMessage `json:"validityEvidence"`
	DataPurpose       string          `json:"dataPurpose"`
}

type stopPointDetailResponse struct {
	stopPointRangeResponse
	CreatedAt string `json:"created_at"`
	UpdatedAt string `json:"updated_at"`
}

func NewStopPointHandlers(service *service.StopPointService) *StopPointHandlers {
	return &StopPointHandlers{service: service}
}

func (h *StopPointHandlers) GetByID(w http.ResponseWriter, r *http.Request) {
	id := chi.URLParam(r, "id")
	if id == "" {
		writeJSONError(w, http.StatusBadRequest, errorCodeInvalidRequest, "id is required")
		return
	}

	stopPoint, found, err := h.service.GetByID(r.Context(), domain.GetStopPointByIDInput{ID: id})
	if err != nil {
		writeServiceError(w, err)
		return
	}
	if !found {
		writeJSONError(w, http.StatusNotFound, errorCodeNotFound, "stop point not found")
		return
	}

	writeJSON(w, http.StatusOK, toStopPointDetailResponse(stopPoint))
}

func (h *StopPointHandlers) GetByRange(w http.ResponseWriter, r *http.Request) {
	input, err := parseGetByRangeInput(r)
	if err != nil {
		writeJSONError(w, http.StatusBadRequest, errorCodeInvalidRequest, err.Error())
		return
	}

	stopPoints, err := h.service.GetByRange(r.Context(), input)
	if err != nil {
		writeServiceError(w, err)
		return
	}

	response := make([]stopPointRangeResponse, 0, len(stopPoints))
	for _, stopPoint := range stopPoints {
		response = append(response, toStopPointRangeResponse(stopPoint))
	}

	writeJSON(w, http.StatusOK, response)
}

func parseGetByRangeInput(r *http.Request) (domain.GetStopPointsByRangeInput, error) {
	query := r.URL.Query()

	latitudeRaw := query.Get("lat")
	if latitudeRaw == "" {
		return domain.GetStopPointsByRangeInput{}, fmt.Errorf("lat is required")
	}

	latitude, err := strconv.ParseFloat(latitudeRaw, 64)
	if err != nil || math.IsNaN(latitude) {
		return domain.GetStopPointsByRangeInput{}, fmt.Errorf("lat must be a number")
	}
	if latitude < -90 || latitude > 90 {
		return domain.GetStopPointsByRangeInput{}, fmt.Errorf("lat must be between -90 and 90")
	}

	longitudeRaw := query.Get("long")
	if longitudeRaw == "" {
		return domain.GetStopPointsByRangeInput{}, fmt.Errorf("long is required")
	}

	longitude, err := strconv.ParseFloat(longitudeRaw, 64)
	if err != nil || math.IsNaN(longitude) {
		return domain.GetStopPointsByRangeInput{}, fmt.Errorf("long must be a number")
	}
	if longitude < -180 || longitude > 180 {
		return domain.GetStopPointsByRangeInput{}, fmt.Errorf("long must be between -180 and 180")
	}

	radiusRaw := query.Get("radius")
	if radiusRaw == "" {
		return domain.GetStopPointsByRangeInput{}, fmt.Errorf("radius is required")
	}

	radiusMeters, err := strconv.Atoi(radiusRaw)
	if err != nil {
		return domain.GetStopPointsByRangeInput{}, fmt.Errorf("radius must be an integer")
	}
	if radiusMeters <= 0 {
		return domain.GetStopPointsByRangeInput{}, fmt.Errorf("radius must be greater than 0")
	}

	limit := defaultStopPointSearchLimit
	if value := query.Get("limit"); value != "" {
		limit, err = strconv.Atoi(value)
		if err != nil {
			return domain.GetStopPointsByRangeInput{}, fmt.Errorf("limit must be an integer")
		}
		if limit <= 0 {
			return domain.GetStopPointsByRangeInput{}, fmt.Errorf("limit must be greater than 0")
		}
		if limit > maxStopPointSearchLimit {
			return domain.GetStopPointsByRangeInput{}, fmt.Errorf("limit must be less than or equal to %d", maxStopPointSearchLimit)
		}
	}

	return domain.GetStopPointsByRangeInput{
		Latitude:     latitude,
		Longitude:    longitude,
		RadiusMeters: radiusMeters,
		Limit:        limit,
	}, nil
}

func toStopPointRangeResponse(stopPoint domain.StopPoint) stopPointRangeResponse {
	var az *string
	if stopPoint.AZ != nil {
		formatted := strconv.FormatFloat(*stopPoint.AZ, 'f', -1, 64)
		az = &formatted
	}

	return stopPointRangeResponse{
		UniqueKey: stopPoint.UniqueKey, AZ: az,
		Latitude: strconv.FormatFloat(stopPoint.Latitude, 'f', -1, 64),
		Longitude: strconv.FormatFloat(stopPoint.Longitude, 'f', -1, 64),
		DirectionStatus: stopPoint.DirectionStatus, ValidityStatus: stopPoint.ValidityStatus,
		Synthetic: stopPoint.Synthetic, DatasetID: stopPoint.DatasetID, SourceMonth: stopPoint.SourceMonth,
		ImputationMethod: stopPoint.ImputationMethod, ImputationReason: stopPoint.ImputationReason,
		DirectionSource: stopPoint.DirectionSource, DirectionEvidence: stopPoint.DirectionEvidence,
		ValidityReason: stopPoint.ValidityReason, ValidityEvidence: stopPoint.ValidityEvidence,
		DataPurpose: stopPoint.DataPurpose,
	}
}

func toStopPointDetailResponse(stopPoint domain.StopPointDetail) stopPointDetailResponse {
	return stopPointDetailResponse{
		stopPointRangeResponse: toStopPointRangeResponse(stopPoint.StopPoint),
		CreatedAt:              stopPoint.CreatedAt,
		UpdatedAt:              stopPoint.UpdatedAt,
	}
}

// writeServiceError maps a repository failure to a response. Database
// availability failures become 503; everything else is a generic 500. The
// underlying error text is never sent to the client.
func writeServiceError(w http.ResponseWriter, err error) {
	if errors.Is(err, domain.ErrDatabaseUnavailable) {
		writeJSONError(w, http.StatusServiceUnavailable, errorCodeDatabaseUnavailable, "database is temporarily unavailable")
		return
	}

	writeJSONError(w, http.StatusInternalServerError, errorCodeInternal, http.StatusText(http.StatusInternalServerError))
}

func writeJSON(w http.ResponseWriter, statusCode int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(statusCode)
	if err := json.NewEncoder(w).Encode(value); err != nil {
		http.Error(w, http.StatusText(http.StatusInternalServerError), http.StatusInternalServerError)
	}
}

func writeJSONError(w http.ResponseWriter, statusCode int, code string, message string) {
	writeJSON(w, statusCode, map[string]any{
		"error": map[string]string{
			"code":    code,
			"message": message,
		},
	})
}
