package main

import (
	"context"
	"log"

	"github.com/aws/aws-lambda-go/events"
	"github.com/aws/aws-lambda-go/lambda"
	chiadapter "github.com/awslabs/aws-lambda-go-api-proxy/chi"

	"github.com/NKC-CV2026/Graduation-research/backend/app/internal/app"
	"github.com/NKC-CV2026/Graduation-research/backend/app/internal/config"
	"github.com/NKC-CV2026/Graduation-research/backend/app/internal/db"
	"github.com/NKC-CV2026/Graduation-research/backend/app/internal/handler"
	"github.com/NKC-CV2026/Graduation-research/backend/app/internal/repository/postgres"
	"github.com/NKC-CV2026/Graduation-research/backend/app/internal/service"
)

func main() {
	ctx := context.Background()

	cfg, err := config.Load()
	if err != nil {
		log.Fatalf("load config: %v", err)
	}

	pool, err := db.NewPool(ctx, cfg)
	if err != nil {
		log.Fatalf("initialize database pool: %v", err)
	}
	defer pool.Close()

	healthRepository := postgres.NewHealthRepository(pool, cfg.DB.HealthCheckTimeout)
	if err := healthRepository.Ping(ctx); err != nil {
		log.Fatalf("ping database: %v", err)
	}

	log.Printf("serving stop points from dataset %q", cfg.StopPointDatasetID)

	stopPointRepository := postgres.NewStopPointRepository(pool, cfg.StopPointDatasetID)

	healthHandlers := handler.NewHealthHandlers(service.NewHealthService(healthRepository))
	stopPointHandlers := handler.NewStopPointHandlers(service.NewStopPointService(stopPointRepository))
	router := app.NewRouter(healthHandlers, stopPointHandlers)
	adapter := chiadapter.NewV2(router)

	lambda.Start(func(ctx context.Context, request events.APIGatewayV2HTTPRequest) (events.APIGatewayV2HTTPResponse, error) {
		return adapter.ProxyWithContextV2(ctx, request)
	})
}
