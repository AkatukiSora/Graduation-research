package config

import (
	"fmt"
	"os"
	"regexp"
	"strconv"
	"strings"
	"time"
)

type Config struct {
	AWSRegion string
	// StopPointDatasetID selects the single regulation dataset version the API
	// serves. It defaults to the provisional synthetic dataset; production must
	// set it explicitly once real datasets exist.
	StopPointDatasetID string
	DB                 DBConfig
}

const defaultStopPointDatasetID = "synthetic-v1"

var stopPointDatasetIDPattern = regexp.MustCompile(`^[a-z0-9][a-z0-9._-]{0,63}$`)

type DBConfig struct {
	Host               string
	Port               int
	Name               string
	User               string
	Password           string
	SSLMode            string
	IAMAuth            bool
	HealthCheckTimeout time.Duration
	MaxConns           int32
	MinConns           int32
	MaxConnLifetime    time.Duration
	MaxConnIdleTime    time.Duration
	AtlasBinaryPath    string
	AtlasMigrationDir  string
}

func Load() (Config, error) {
	port, err := intFromEnv("DB_PORT", 5432)
	if err != nil {
		return Config{}, err
	}

	healthTimeoutSeconds, err := intFromEnv("DB_HEALTHCHECK_TIMEOUT_SECONDS", 5)
	if err != nil {
		return Config{}, err
	}

	maxConns, err := int32FromEnv("DB_MAX_CONNS", 4)
	if err != nil {
		return Config{}, err
	}

	minConns, err := int32FromEnv("DB_MIN_CONNS", 0)
	if err != nil {
		return Config{}, err
	}

	maxConnLifetimeMinutes, err := intFromEnv("DB_MAX_CONN_LIFETIME_MINUTES", 10)
	if err != nil {
		return Config{}, err
	}

	maxConnIdleMinutes, err := intFromEnv("DB_MAX_CONN_IDLE_MINUTES", 2)
	if err != nil {
		return Config{}, err
	}

	datasetID, err := stopPointDatasetIDFromEnv()
	if err != nil {
		return Config{}, err
	}

	cfg := Config{
		AWSRegion:          getenvDefault("AWS_REGION", "ap-northeast-3"),
		StopPointDatasetID: datasetID,
		DB: DBConfig{
			Host:               strings.TrimSpace(os.Getenv("DB_HOST")),
			Port:               port,
			Name:               strings.TrimSpace(os.Getenv("DB_NAME")),
			User:               strings.TrimSpace(os.Getenv("DB_USER")),
			Password:           os.Getenv("DB_PASSWORD"),
			SSLMode:            getenvDefault("DB_SSL_MODE", "require"),
			IAMAuth:            boolFromEnv("DB_IAM_AUTH", true),
			HealthCheckTimeout: time.Duration(healthTimeoutSeconds) * time.Second,
			MaxConns:           maxConns,
			MinConns:           minConns,
			MaxConnLifetime:    time.Duration(maxConnLifetimeMinutes) * time.Minute,
			MaxConnIdleTime:    time.Duration(maxConnIdleMinutes) * time.Minute,
			AtlasBinaryPath:    getenvDefault("ATLAS_BINARY_PATH", "/usr/local/bin/atlas"),
			AtlasMigrationDir:  getenvDefault("ATLAS_MIGRATION_DIR", "file:///app/db/migrations"),
		},
	}

	if cfg.DB.Host == "" {
		return Config{}, fmt.Errorf("DB_HOST is required")
	}

	if cfg.DB.Name == "" {
		return Config{}, fmt.Errorf("DB_NAME is required")
	}

	if cfg.DB.User == "" {
		return Config{}, fmt.Errorf("DB_USER is required")
	}

	if cfg.AWSRegion == "" {
		return Config{}, fmt.Errorf("AWS_REGION is required")
	}

	return cfg, nil
}

// stopPointDatasetIDFromEnv returns the dataset version served by the API. An
// unset variable selects the provisional default; a blank or malformed value is
// an error so that a misconfiguration never silently serves another dataset.
func stopPointDatasetIDFromEnv() (string, error) {
	value, present := os.LookupEnv("STOP_POINT_DATASET_ID")
	if !present {
		return defaultStopPointDatasetID, nil
	}

	value = strings.TrimSpace(value)
	if value == "" {
		return "", fmt.Errorf("STOP_POINT_DATASET_ID must not be empty")
	}
	if !stopPointDatasetIDPattern.MatchString(value) {
		return "", fmt.Errorf("STOP_POINT_DATASET_ID must match %s", stopPointDatasetIDPattern.String())
	}

	return value, nil
}

func getenvDefault(key string, fallback string) string {
	value := strings.TrimSpace(os.Getenv(key))
	if value == "" {
		return fallback
	}

	return value
}

func boolFromEnv(key string, fallback bool) bool {
	value := strings.TrimSpace(os.Getenv(key))
	if value == "" {
		return fallback
	}

	parsed, err := strconv.ParseBool(value)
	if err != nil {
		return fallback
	}

	return parsed
}

func intFromEnv(key string, fallback int) (int, error) {
	value := strings.TrimSpace(os.Getenv(key))
	if value == "" {
		return fallback, nil
	}

	parsed, err := strconv.Atoi(value)
	if err != nil {
		return 0, fmt.Errorf("%s must be an integer: %w", key, err)
	}

	return parsed, nil
}

func int32FromEnv(key string, fallback int32) (int32, error) {
	value := strings.TrimSpace(os.Getenv(key))
	if value == "" {
		return fallback, nil
	}

	parsed, err := strconv.ParseInt(value, 10, 32)
	if err != nil {
		return 0, fmt.Errorf("%s must be an integer: %w", key, err)
	}

	return int32(parsed), nil
}
