package domain_test

import (
	"fmt"
	"testing"

	"github.com/NKC-CV2026/Graduation-research/backend/app/internal/domain"
)

// TestNotificationEligibleTruthTable enumerates every combination of the four
// inputs. A point is eligible for notification only when its dataset is
// production approved and not synthetic and the point direction is confirmed
// and the point is valid.
func TestNotificationEligibleTruthTable(t *testing.T) {
	directions := []string{domain.DirectionStatusUnknown, domain.DirectionStatusInferred, domain.DirectionStatusConfirmed}
	validities := []string{domain.ValidityStatusUnknown, domain.ValidityStatusValid, domain.ValidityStatusInvalid}

	eligibleCount := 0
	for _, approved := range []bool{false, true} {
		for _, synthetic := range []bool{false, true} {
			for _, direction := range directions {
				for _, validity := range validities {
					name := fmt.Sprintf("approved=%t/synthetic=%t/direction=%s/validity=%s", approved, synthetic, direction, validity)
					t.Run(name, func(t *testing.T) {
						point := domain.StopPoint{
							ProductionApproved: approved,
							Synthetic:          synthetic,
							DirectionStatus:    direction,
							ValidityStatus:     validity,
						}
						want := approved && !synthetic &&
							direction == domain.DirectionStatusConfirmed &&
							validity == domain.ValidityStatusValid
						if got := point.NotificationEligible(); got != want {
							t.Fatalf("NotificationEligible() = %t, want %t", got, want)
						}
						if want {
							eligibleCount++
						}
					})
				}
			}
		}
	}
	if eligibleCount != 1 {
		t.Fatalf("exactly one combination must be eligible, got %d", eligibleCount)
	}
}

func TestZeroValueStopPointIsNotEligible(t *testing.T) {
	if (domain.StopPoint{}).NotificationEligible() {
		t.Fatal("a zero value stop point must not be eligible for notification")
	}
}
