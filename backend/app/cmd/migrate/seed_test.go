package main

import (
	"bytes"
	"context"
	"reflect"
	"strings"
	"testing"

	"github.com/NKC-CV2026/Graduation-research/backend/app/internal/seeddata"
)

func TestParseSeedDatasetIDs(t *testing.T) {
	cases := []struct {
		name    string
		raw     string
		want    []string
		wantErr bool
	}{
		{name: "unset means no seeding", raw: "", want: nil},
		{name: "blank means no seeding", raw: "  ,  ", want: nil},
		{name: "single", raw: "synthetic-v1", want: []string{"synthetic-v1"}},
		{name: "trims and drops duplicates", raw: " synthetic-v1 , other-1,synthetic-v1", want: []string{"synthetic-v1", "other-1"}},
		{name: "rejects path traversal", raw: "../etc", wantErr: true},
		{name: "rejects uppercase and spaces", raw: "Bad Id", wantErr: true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := parseSeedDatasetIDs(tc.raw)
			if (err != nil) != tc.wantErr {
				t.Fatalf("err = %v, wantErr %t", err, tc.wantErr)
			}
			if !tc.wantErr && !reflect.DeepEqual(got, tc.want) {
				t.Fatalf("ids = %#v, want %#v", got, tc.want)
			}
		})
	}
}

// A dataset that is not embedded must fail before any database work, so no
// earlier dataset in the list is written either.
func TestRunSeedValidatesEveryDatasetBeforeWriting(t *testing.T) {
	var out bytes.Buffer
	err := runSeed(context.Background(), nil, seeddata.FS(), []string{"synthetic-v1", "missing-dataset"}, &out)
	if err == nil || !strings.Contains(err.Error(), "missing-dataset") {
		t.Fatalf("err = %v, want failure naming missing-dataset", err)
	}
	if out.Len() != 0 {
		t.Fatalf("output written before validation finished: %q", out.String())
	}
}

func TestRunSeedWithoutDatasetsDoesNothing(t *testing.T) {
	var out bytes.Buffer
	if err := runSeed(context.Background(), nil, seeddata.FS(), nil, &out); err != nil || out.Len() != 0 {
		t.Fatalf("err = %v, output = %q", err, out.String())
	}
}
