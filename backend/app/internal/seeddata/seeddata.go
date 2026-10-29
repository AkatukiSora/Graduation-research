// Package seeddata embeds the versioned, validated seed datasets.
//
// Each dataset lives in data/<dataset-id>/ with a manifest.json and a
// dataset.json. The files are compiled into the binary, so the seed command
// never reads datasets from the file system at run time.
package seeddata

import (
	"embed"
	"io/fs"
)

//go:embed data
var embedded embed.FS

//go:embed points
var embeddedPoints embed.FS

// FS returns the embedded datasets. Paths are <dataset-id>/manifest.json and
// <dataset-id>/dataset.json.
func FS() fs.FS {
	sub, err := fs.Sub(embedded, "data")
	if err != nil {
		panic("seeddata: embedded data directory is missing: " + err.Error())
	}
	return sub
}

// PointsFS returns the embedded points-only datasets. Paths are
// <dataset-id>/meta.json and <dataset-id>/points.csv.gz.
func PointsFS() fs.FS {
	sub, err := fs.Sub(embeddedPoints, "points")
	if err != nil {
		panic("seeddata: embedded points directory is missing: " + err.Error())
	}
	return sub
}
