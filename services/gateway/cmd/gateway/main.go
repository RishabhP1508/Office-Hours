// Command gateway is the Go edge in front of the Python orchestrator (ARCHITECTURE.md: "Python
// orchestrator now, Go gateway at Phase 7"). It is the only thing the browser talks to: it rate
// limits, redacts PII out of the request body, times upstream calls out, and propagates one
// OpenTelemetry trace across both services, then proxies the four routes below to the
// orchestrator.
package main

import (
	"context"
	"log"
	"net/http"
	"os/signal"
	"syscall"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/go-chi/cors"
	"github.com/redis/go-redis/v9"
	"go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp"

	"office-hours/gateway/internal/config"
	"office-hours/gateway/internal/middleware"
	"office-hours/gateway/internal/proxy"
)

func main() {
	cfg := config.Load()

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	shutdownTracing, err := middleware.SetupTracing(ctx, cfg.OTLPEndpoint, cfg.ServiceName)
	if err != nil {
		log.Fatalf("failed to set up tracing: %v", err)
	}
	defer func() {
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		if err := shutdownTracing(shutdownCtx); err != nil {
			log.Printf("tracing shutdown: %v", err)
		}
	}()

	redisOpts, err := redis.ParseURL(cfg.RedisURL)
	if err != nil {
		log.Fatalf("invalid REDIS_URL %q: %v", cfg.RedisURL, err)
	}
	redisClient := redis.NewClient(redisOpts)
	defer redisClient.Close()

	// Fail open on a Redis outage: see docs/adr/0007-go-python-split.md for why availability is
	// weighed above abuse protection for this product's actual risk profile.
	limiter := middleware.NewRateLimiter(redisClient, cfg.RateLimitCapacity, cfg.RateLimitRefillPerSecond, true)

	p := proxy.New(cfg.OrchestratorURL, cfg.UpstreamTimeout)

	router := chi.NewRouter()
	router.Use(cors.Handler(cors.Options{
		AllowedOrigins:   cfg.AllowedOrigins,
		AllowedMethods:   []string{"GET", "POST", "OPTIONS"},
		AllowedHeaders:   []string{"Content-Type"},
		AllowCredentials: true,
	}))

	// /health: the gateway's own liveness, never proxied, never rate limited (a container
	// orchestrator's health probe must not be able to get 429'd) -- see docs/reports for the
	// reasoning captured alongside this phase's verification.
	router.Get("/health", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"status":"ok"}`))
	})

	router.Route("/v1", func(v1 chi.Router) {
		v1.Use(middleware.RateLimitMiddleware(limiter, cfg.TrustedProxyCIDRs))
		v1.Use(middleware.PIIMiddleware(middleware.NewRegexRedactor()))
		// Phase 8 round 4: stamps every forwarded request with an opaque, salted hash of the
		// caller's own address (see internal/middleware/session.go) so the orchestrator's GET
		// /usage distinct_sessions count is real behind this gateway, not stuck at 1 forever.
		v1.Use(middleware.SessionHashMiddleware(cfg.SessionHashSalt, cfg.TrustedProxyCIDRs))

		v1.With(middleware.Timeout(cfg.UpstreamTimeout)).Post("/query", func(w http.ResponseWriter, r *http.Request) {
			p.ProxyJSON(w, r, "/query")
		})
		v1.With(middleware.Timeout(cfg.UpstreamTimeout)).Get("/sources/status", func(w http.ResponseWriter, r *http.Request) {
			p.ProxyJSON(w, r, "/sources/status")
		})
		// No Timeout middleware here: ProxyStream applies its own time-to-first-byte-only bound
		// internally (see internal/proxy/proxy.go's doc comment).
		v1.Post("/query/stream", func(w http.ResponseWriter, r *http.Request) {
			p.ProxyStream(w, r, "/query/stream")
		})
	})

	// otelhttp.NewHandler wraps the whole router: every incoming request gets a SERVER span,
	// which becomes the parent of the CLIENT span internal/proxy/proxy.go's otelhttp-wrapped
	// client creates for the upstream call -- this is the other half of what makes one trace span
	// both services (see internal/middleware/tracing.go's doc comment for the propagator half).
	handler := otelhttp.NewHandler(router, "gateway")

	server := &http.Server{
		Addr:    cfg.ListenAddr,
		Handler: handler,
	}

	go func() {
		<-ctx.Done()
		log.Println("shutting down gateway...")
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		if err := server.Shutdown(shutdownCtx); err != nil {
			log.Printf("gateway shutdown: %v", err)
		}
	}()

	log.Printf("office-hours-gateway listening on %s, proxying to %s", cfg.ListenAddr, cfg.OrchestratorURL)
	if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatalf("gateway server failed: %v", err)
	}
}
