package seedexperiment

import "embed"

// embeddedData is kept behind the package API so the CLI cannot use the normal
// regulation seed filesystem by accident.
//
//go:embed data
var embeddedData embed.FS
