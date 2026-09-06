package proxy

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/go-chi/cors"
)

// upstreamSettingCORSAndHopByHop simulates the orchestrator: its OWN CORS middleware answers with
// Access-Control-Allow-Origin (and the Vary: Origin that comes with it), and it also sets a
// hop-by-hop header (Connection) that a proxy must never forward verbatim. This is exactly the
// shape services/orchestrator/app/main.py's CORSMiddleware produces, reproduced here without
// depending on Python at all.
func upstreamSettingCORSAndHopByHop(body string, streaming bool) *httptest.Server {
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Access-Control-Allow-Origin", "http://localhost:3000")
		w.Header().Set("Access-Control-Allow-Credentials", "true")
		w.Header().Add("Vary", "Origin")
		w.Header().Set("Connection", "keep-alive")
		w.Header().Set("Content-Type", "application/json")
		if streaming {
			w.Header().Set("Content-Type", "text/event-stream")
		}
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(body))
	}))
}

// gatewayCORSHandler wraps `next` in go-chi/cors the same way cmd/gateway/main.go does, so this
// test exercises the REAL header-count bug: chi/cors sets its own Access-Control-Allow-Origin
// BEFORE the proxy handler runs, and the proxy must not add a second one on top of it.
func gatewayCORSHandler(next http.HandlerFunc) http.Handler {
	return cors.Handler(cors.Options{
		AllowedOrigins:   []string{"http://localhost:3000"},
		AllowedMethods:   []string{"GET", "POST", "OPTIONS"},
		AllowedHeaders:   []string{"Content-Type"},
		AllowCredentials: true,
	})(next)
}

func TestProxyJSON_NeverDuplicatesCORSHeadersFromTheUpstreamsOwnCORSMiddleware(t *testing.T) {
	upstream := upstreamSettingCORSAndHopByHop(`{"answer":"ok"}`, false)
	defer upstream.Close()

	p := New(upstream.URL, 15*time.Second)
	handler := gatewayCORSHandler(func(w http.ResponseWriter, r *http.Request) {
		p.ProxyJSON(w, r, "/query")
	})

	req := httptest.NewRequest(http.MethodPost, "/v1/query", strings.NewReader(`{"question":"x"}`))
	req.Header.Set("Origin", "http://localhost:3000")
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)

	// The header COUNT is the actual bug: a presence-only check ("is the header there at all")
	// passes whether the gateway answers once or the response carries the gateway's AND the
	// upstream's copy of the same header -- exactly what a browser rejects outright (a duplicated
	// Access-Control-Allow-Origin value, not just an unexpected one).
	if got := len(rec.Header().Values("Access-Control-Allow-Origin")); got != 1 {
		t.Fatalf("expected exactly 1 Access-Control-Allow-Origin header, got %d: %v",
			got, rec.Header().Values("Access-Control-Allow-Origin"))
	}
	if got := rec.Header().Get("Access-Control-Allow-Origin"); got != "http://localhost:3000" {
		t.Fatalf("expected the gateway's own CORS answer, got %q", got)
	}
	if got := len(rec.Header().Values("Access-Control-Allow-Credentials")); got != 1 {
		t.Fatalf("expected exactly 1 Access-Control-Allow-Credentials header, got %d", got)
	}
	if got := len(rec.Header().Values("Vary")); got != 1 {
		t.Fatalf("expected exactly 1 Vary header (the gateway's own, not a second copy from the "+
			"upstream), got %d: %v", got, rec.Header().Values("Vary"))
	}
	if rec.Header().Get("Connection") != "" {
		t.Fatalf("expected the hop-by-hop Connection header to never be forwarded, got %q",
			rec.Header().Get("Connection"))
	}
}

func TestProxyStream_NeverDuplicatesCORSHeadersFromTheUpstreamsOwnCORSMiddleware(t *testing.T) {
	upstream := upstreamSettingCORSAndHopByHop(`data: {"event":"ping"}`+"\n\n", true)
	defer upstream.Close()

	p := New(upstream.URL, 15*time.Second)
	handler := gatewayCORSHandler(func(w http.ResponseWriter, r *http.Request) {
		p.ProxyStream(w, r, "/query/stream")
	})

	req := httptest.NewRequest(http.MethodPost, "/v1/query/stream", strings.NewReader(`{"question":"x"}`))
	req.Header.Set("Origin", "http://localhost:3000")
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)

	if got := len(rec.Header().Values("Access-Control-Allow-Origin")); got != 1 {
		t.Fatalf("expected exactly 1 Access-Control-Allow-Origin header on the streamed response, "+
			"got %d: %v", got, rec.Header().Values("Access-Control-Allow-Origin"))
	}
	if got := rec.Header().Get("Access-Control-Allow-Origin"); got != "http://localhost:3000" {
		t.Fatalf("expected the gateway's own CORS answer, got %q", got)
	}
	if got := len(rec.Header().Values("Vary")); got != 1 {
		t.Fatalf("expected exactly 1 Vary header, got %d: %v", got, rec.Header().Values("Vary"))
	}
	if rec.Header().Get("Connection") != "" {
		t.Fatalf("expected the hop-by-hop Connection header to never be forwarded on the streamed "+
			"response, got %q", rec.Header().Get("Connection"))
	}
}

func TestShouldSkipResponseHeader_CoversEveryHopByHopHeaderAndAccessControlAndVary(t *testing.T) {
	mustSkip := []string{
		"Access-Control-Allow-Origin",
		"Access-Control-Allow-Credentials",
		"access-control-expose-headers", // lowercase input must match just as reliably
		"Vary",
		"vary",
		"Connection",
		"Keep-Alive",
		"Proxy-Authenticate",
		"Proxy-Authorization",
		"TE",
		"Trailer",
		"Transfer-Encoding",
		"Upgrade",
	}
	for _, h := range mustSkip {
		if !shouldSkipResponseHeader(h) {
			t.Errorf("expected %q to be skipped, but it was not", h)
		}
	}

	mustForward := []string{
		"Content-Type",
		"Content-Length",
		"X-Request-Id",
		"Cache-Control",
	}
	for _, h := range mustForward {
		if shouldSkipResponseHeader(h) {
			t.Errorf("expected %q to be forwarded, but it was skipped", h)
		}
	}
}
