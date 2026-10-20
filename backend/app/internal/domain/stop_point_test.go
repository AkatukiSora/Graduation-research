package domain_test

import (
	"reflect"
	"testing"

	"github.com/NKC-CV2026/Graduation-research/backend/app/internal/domain"
)

// The public models carry only what a client needs. Evidence, provenance and
// dataset metadata live in the database and must not leak into the domain
// model, from where they could reach a response by accident.
func TestStopPointModelsCarryOnlyPublicFields(t *testing.T) {
	want := []string{"UniqueKey", "AZ", "Latitude", "Longitude", "DirectionStatus"}
	for _, model := range []any{domain.StopPoint{}, domain.StopPointDetail{}} {
		typeOf := reflect.TypeOf(model)
		if typeOf.NumField() != len(want) {
			t.Errorf("%s has %d fields, want %d", typeOf.Name(), typeOf.NumField(), len(want))
		}
		for _, name := range want {
			if _, ok := typeOf.FieldByName(name); !ok {
				t.Errorf("%s is missing field %q", typeOf.Name(), name)
			}
		}
	}
}
