package seed

import (
	"bytes"
	"compress/gzip"
	"strings"
	"testing"
	"testing/fstest"

	"github.com/NKC-CV2026/Graduation-research/backend/app/internal/seeddata"
)

func pointsFS(t *testing.T, meta, csv string) fstest.MapFS {
	t.Helper()
	var buf bytes.Buffer
	gz := gzip.NewWriter(&buf)
	if _, err := gz.Write([]byte(csv)); err != nil {
		t.Fatal(err)
	}
	if err := gz.Close(); err != nil {
		t.Fatal(err)
	}
	return fstest.MapFS{
		"ds/meta.json":     {Data: []byte(meta)},
		"ds/points.csv.gz": {Data: buf.Bytes()},
	}
}

const pointsMeta = `{"dataset_id":"ds","source_month":"202602","region_id":"r","content_sha256":"` +
	"0000000000000000000000000000000000000000000000000000000000000000" + `","points":2}`

func TestLoadPoints(t *testing.T) {
	d, err := LoadPoints(pointsFS(t, pointsMeta, "a,35.1,136.9,90\nb,35.2,136.8,\n"), "ds")
	if err != nil {
		t.Fatal(err)
	}
	if len(d.Rows) != 2 || d.Rows[0].Az == nil || *d.Rows[0].Az != 90 || d.Rows[1].Az != nil {
		t.Fatalf("rows = %+v", d.Rows)
	}
}

func TestLoadPointsRejectsInvalidRows(t *testing.T) {
	for name, csv := range map[string]string{
		"duplicate uid":  "a,35.1,136.9,90\na,35.2,136.8,\n",
		"latitude range": "a,91,136.9,90\nb,35.2,136.8,\n",
		"az range":       "a,35.1,136.9,360\nb,35.2,136.8,\n",
		"long uid":       strings.Repeat("a", 33) + ",35.1,136.9,\nb,35.2,136.8,\n",
		"row count":      "a,35.1,136.9,90\n",
		"field count":    "a,35.1,136.9\nb,35.2,136.8,\n",
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := LoadPoints(pointsFS(t, pointsMeta, csv), "ds"); err == nil {
				t.Fatal("LoadPoints succeeded")
			}
		})
	}
}

func TestLoadEmbeddedExternalPoints(t *testing.T) {
	const id = "external-stop-approach-test-202602-v2"
	if !HasPoints(seeddata.PointsFS(), id) {
		t.Fatalf("%s is not embedded", id)
	}
	d, err := LoadPoints(seeddata.PointsFS(), id)
	if err != nil {
		t.Fatal(err)
	}
	unknown := 0
	for _, row := range d.Rows {
		if row.Az == nil {
			unknown++
		}
	}
	if len(d.Rows) != 168652 || unknown != 2072 {
		t.Fatalf("rows=%d unknown=%d", len(d.Rows), unknown)
	}
}
