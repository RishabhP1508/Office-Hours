package middleware

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func questionLengthHandler(t *testing.T) (http.Handler, *bool) {
	t.Helper()
	nextCalled := false
	handler := QuestionLengthMiddleware()(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		nextCalled = true
		w.WriteHeader(http.StatusOK)
	}))
	return handler, &nextCalled
}

func TestQuestionLengthMiddleware_AllowsAQuestionAtExactlyTheLimit(t *testing.T) {
	handler, nextCalled := questionLengthHandler(t)

	body := `{"question":"` + strings.Repeat("a", MaxQuestionLength) + `"}`
	req := httptest.NewRequest(http.MethodPost, "/v1/query", strings.NewReader(body))
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected a question of exactly MaxQuestionLength (%d) characters to pass, got %d: %s",
			MaxQuestionLength, rec.Code, rec.Body.String())
	}
	if !*nextCalled {
		t.Fatal("expected the request to reach the wrapped handler")
	}
}

func TestQuestionLengthMiddleware_RejectsAQuestionOneCharacterOverTheLimit(t *testing.T) {
	handler, nextCalled := questionLengthHandler(t)

	body := `{"question":"` + strings.Repeat("a", MaxQuestionLength+1) + `"}`
	req := httptest.NewRequest(http.MethodPost, "/v1/query", strings.NewReader(body))
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)

	if rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("expected 422 for a question one character over the limit, got %d", rec.Code)
	}
	if *nextCalled {
		t.Fatal("expected the oversized request to never reach the wrapped handler")
	}
}

// THE PROPERTY, not a matched string: no internal detail leaks into the response body at all --
// no Go stdlib wording, no source file/package reference, no environment variable name. This is
// exactly what CLAUDE.md's red-team fix asked to be tested as a property, not by matching the one
// example string from the incident report.
func TestQuestionLengthMiddleware_RejectionBodyLeaksNoInternalDetail(t *testing.T) {
	handler, _ := questionLengthHandler(t)

	body := `{"question":"` + strings.Repeat("a", MaxQuestionLength+1) + `"}`
	req := httptest.NewRequest(http.MethodPost, "/v1/query", strings.NewReader(body))
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)

	got := rec.Body.String()
	forbidden := []string{
		"http:",            // Go stdlib error prefixes (e.g. "http: request body too large")
		".go",              // a source file reference
		"internal/",        // a package path
		"MaxBytesReader",   // the Go API name that enforces this
		"EMBED_GGUF_N_CTX", // the orchestrator's own env var name for this same derivation
		"panic",            // a crash leaking through
		"runtime error",    // ditto
	}
	for _, s := range forbidden {
		if strings.Contains(got, s) {
			t.Fatalf("response body leaked internal detail (%q): %s", s, got)
		}
	}
	if !strings.Contains(strings.ToLower(got), "too long") {
		t.Fatalf("expected a plain-language 'too long' message, got: %s", got)
	}
}

// A body far larger than maxRequestBodyBytes (the byte-level ceiling, checked before the JSON is
// even parsed) must be rejected the same clean way -- this is the 60,000-character reproduction
// from the incident report, scaled further to also exceed the byte ceiling itself, not just the
// character count.
func TestQuestionLengthMiddleware_RejectsABodyLargerThanTheByteCeiling(t *testing.T) {
	handler, nextCalled := questionLengthHandler(t)

	body := `{"question":"` + strings.Repeat("a", 60000) + `"}`
	req := httptest.NewRequest(http.MethodPost, "/v1/query", strings.NewReader(body))
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)

	if rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("expected 422 for a 60,000-character question, got %d: %s", rec.Code, rec.Body.String())
	}
	if *nextCalled {
		t.Fatal("expected the oversized request to never reach the wrapped handler")
	}
	got := rec.Body.String()
	if strings.Contains(got, "http:") || strings.Contains(got, "too large") {
		t.Fatalf("expected no Go stdlib wording in the response body, got: %s", got)
	}
}

// Non-JSON bodies, bodies with no "question" field, and GET requests with no body at all must
// pass through completely unchanged -- the same convention internal/middleware/pii.go's
// PIIMiddleware already follows, and for the same reason: this middleware must never guess at, or
// second-guess, a request shape it does not recognize.
func TestQuestionLengthMiddleware_PassesThroughRequestsWithNoRecognizableQuestionField(t *testing.T) {
	cases := []struct {
		name string
		body string
	}{
		{"not JSON at all", "not json"},
		{"JSON object with no question field", `{"other":"value"}`},
		{"question field is not a string", `{"question": 12345}`},
		{"empty body", ""},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			handler, nextCalled := questionLengthHandler(t)
			var req *http.Request
			if tc.body == "" {
				req = httptest.NewRequest(http.MethodPost, "/v1/query", nil)
			} else {
				req = httptest.NewRequest(http.MethodPost, "/v1/query", strings.NewReader(tc.body))
			}
			rec := httptest.NewRecorder()
			handler.ServeHTTP(rec, req)

			if rec.Code != http.StatusOK {
				t.Fatalf("expected an unrecognized body shape to pass through untouched, got %d: %s",
					rec.Code, rec.Body.String())
			}
			if !*nextCalled {
				t.Fatal("expected the request to reach the wrapped handler")
			}
		})
	}
}

func TestQuestionLengthMiddleware_GetRequestWithNoBodyPassesThrough(t *testing.T) {
	handler, nextCalled := questionLengthHandler(t)

	req := httptest.NewRequest(http.MethodGet, "/v1/sources/status", nil)
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected a bodyless GET request to pass through, got %d", rec.Code)
	}
	if !*nextCalled {
		t.Fatal("expected the request to reach the wrapped handler")
	}
}
