package domain_test

import (
	"encoding/json"
	"reflect"
	"testing"

	"github.com/NKC-CV2026/Graduation-research/backend/app/internal/domain"
)

func TestStopPointCarriesEvidenceWithoutBackendApprovalPolicy(t *testing.T) {
	typeOf := reflect.TypeOf(domain.StopPoint{})
	for _, name := range []string{"ImputationMethod", "ImputationReason", "DirectionSource", "DirectionEvidence", "ValidityReason", "ValidityEvidence", "DataPurpose"} {
		if _, ok := typeOf.FieldByName(name); !ok {
			t.Errorf("StopPoint is missing API evidence field %q", name)
		}
	}
	for _, name := range []string{"ProductionApproved", "NotificationEligible"} {
		if _, ok := typeOf.FieldByName(name); ok {
			t.Errorf("StopPoint must not retain backend policy field %q", name)
		}
	}
	if _, ok := typeOf.MethodByName("NotificationEligible"); ok {
		t.Fatal("notification eligibility belongs to the client, not the domain model")
	}
	for _, name := range []string{"DirectionEvidence", "ValidityEvidence"} {
		field, _ := typeOf.FieldByName(name)
		if field.Type != reflect.TypeOf(json.RawMessage{}) {
			t.Errorf("%s has type %s, want json.RawMessage", name, field.Type)
		}
	}
}
