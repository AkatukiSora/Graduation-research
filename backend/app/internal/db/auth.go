package db

import (
	"context"
	"errors"
	"fmt"
	"sync"

	"github.com/aws/aws-sdk-go-v2/aws"
	awsconfig "github.com/aws/aws-sdk-go-v2/config"
	rdsauth "github.com/aws/aws-sdk-go-v2/feature/rds/auth"

	"github.com/NKC-CV2026/Graduation-research/backend/app/internal/config"
)

// PasswordResolver returns the password for one new database connection. It is
// called for every new connection because RDS IAM tokens expire after 15
// minutes. Implementations must never log the value they return.
type PasswordResolver func(ctx context.Context) (string, error)

// NewPasswordResolver returns the resolver for the configured authentication
// mode: a static DB_PASSWORD (no AWS call), or an RDS IAM token built from the
// default AWS credential chain for each call.
func NewPasswordResolver(cfg config.Config) PasswordResolver {
	if cfg.DB.Password != "" {
		password := cfg.DB.Password
		return func(context.Context) (string, error) { return password, nil }
	}

	if !cfg.DB.IAMAuth {
		return func(context.Context) (string, error) {
			return "", errors.New("DB_PASSWORD is required when DB_IAM_AUTH is false")
		}
	}

	endpoint := fmt.Sprintf("%s:%d", cfg.DB.Host, cfg.DB.Port)

	// The credential provider caches and refreshes credentials itself, so it is
	// loaded once; only the token is rebuilt per connection. A failed load is
	// not cached.
	var (
		mutex       sync.Mutex
		credentials aws.CredentialsProvider
	)
	provider := func(ctx context.Context) (aws.CredentialsProvider, error) {
		mutex.Lock()
		defer mutex.Unlock()
		if credentials != nil {
			return credentials, nil
		}
		awsCfg, err := awsconfig.LoadDefaultConfig(ctx, awsconfig.WithRegion(cfg.AWSRegion))
		if err != nil {
			return nil, fmt.Errorf("load aws config: %w", err)
		}
		credentials = awsCfg.Credentials
		return credentials, nil
	}

	return func(ctx context.Context) (string, error) {
		creds, err := provider(ctx)
		if err != nil {
			return "", err
		}
		password, err := rdsauth.BuildAuthToken(ctx, endpoint, cfg.AWSRegion, cfg.DB.User, creds)
		if err != nil {
			return "", fmt.Errorf("build rds auth token: %w", err)
		}
		return password, nil
	}
}

// ResolvePassword resolves one password for a one-shot consumer such as the
// migration command.
func ResolvePassword(ctx context.Context, cfg config.Config) (string, error) {
	return NewPasswordResolver(cfg)(ctx)
}
