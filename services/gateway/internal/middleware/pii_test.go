package middleware

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func capturedDownstreamBody(t *testing.T, req *http.Request, redactor Redactor) string {
	t.Helper()
	var captured []byte
	handler := PIIMiddleware(redactor)(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, err := io.ReadAll(r.Body)
		if err != nil {
			t.Fatalf("reading downstream body: %v", err)
		}
		captured = b
	}))
	handler.ServeHTTP(httptest.NewRecorder(), req)
	return string(captured)
}

func TestPIIMiddleware_RedactsAnEmailAddress(t *testing.T) {
	body := `{"question": "My email is jane.doe@example.com, can I file I-765?"}`
	req := httptest.NewRequest(http.MethodPost, "/v1/query", strings.NewReader(body))

	got := capturedDownstreamBody(t, req, NewRegexRedactor())

	if strings.Contains(got, "jane.doe@example.com") {
		t.Fatalf("expected the email to be redacted, got: %s", got)
	}
	if !strings.Contains(got, "[redacted-email]") {
		t.Fatalf("expected the stable placeholder in the output, got: %s", got)
	}
	if !strings.Contains(got, "can I file I-765") {
		t.Fatalf("expected the surrounding sentence to survive, got: %s", got)
	}
}

func TestPIIMiddleware_RedactsPhoneSSNAndANumber(t *testing.T) {
	cases := []struct {
		name        string
		question    string
		placeholder string
		mustNotHave string
	}{
		{"phone", "Call me at 555-123-4567 about my case.", "[redacted-phone]", "555-123-4567"},
		{"ssn", "My SSN is 123-45-6789.", "[redacted-ssn]", "123-45-6789"},
		{"a-number", "My A-number is A012345678.", "[redacted-a-number]", "A012345678"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			raw, err := json.Marshal(map[string]string{"question": tc.question})
			if err != nil {
				t.Fatalf("marshal fixture: %v", err)
			}
			req := httptest.NewRequest(http.MethodPost, "/v1/query", bytes.NewReader(raw))

			got := capturedDownstreamBody(t, req, NewRegexRedactor())

			if !strings.Contains(got, tc.placeholder) {
				t.Fatalf("expected %s in output, got: %s", tc.placeholder, got)
			}
			if strings.Contains(got, tc.mustNotHave) {
				t.Fatalf("expected %q to be redacted out of the output, got: %s", tc.mustNotHave, got)
			}
		})
	}
}

func TestPIIMiddleware_PreservesOtherFieldsAndProducesValidJSON(t *testing.T) {
	body := `{"question": "contact me at jane@example.com", "extra_field": "must survive"}`
	req := httptest.NewRequest(http.MethodPost, "/v1/query", strings.NewReader(body))

	got := capturedDownstreamBody(t, req, NewRegexRedactor())

	var parsed map[string]interface{}
	if err := json.Unmarshal([]byte(got), &parsed); err != nil {
		t.Fatalf("output is not valid JSON: %v, got: %s", err, got)
	}
	if parsed["extra_field"] != "must survive" {
		t.Fatalf("expected extra_field to survive untouched, got: %v", parsed["extra_field"])
	}
	if parsed["question"] == "contact me at jane@example.com" {
		t.Fatalf("expected question to have been redacted, got: %v", parsed["question"])
	}
}

func TestPIIMiddleware_PassesThroughANonJSONBodyUnchanged(t *testing.T) {
	body := "not json at all"
	req := httptest.NewRequest(http.MethodPost, "/v1/query", strings.NewReader(body))

	got := capturedDownstreamBody(t, req, NewRegexRedactor())

	if got != body {
		t.Fatalf("expected the body to pass through unchanged, got: %s", got)
	}
}

func TestPIIMiddleware_PassesThroughABodyWithNoQuestionFieldUnchanged(t *testing.T) {
	body := `{"other_field": "jane@example.com"}`
	req := httptest.NewRequest(http.MethodPost, "/v1/query", strings.NewReader(body))

	got := capturedDownstreamBody(t, req, NewRegexRedactor())

	if got != body {
		t.Fatalf("expected the body to pass through unchanged with no question field, got: %s", got)
	}
}

func TestPIIMiddleware_PassesThroughAnEmptyBodyUnchanged(t *testing.T) {
	req := httptest.NewRequest(http.MethodGet, "/v1/sources/status", nil)

	got := capturedDownstreamBody(t, req, NewRegexRedactor())

	if got != "" {
		t.Fatalf("expected an empty body to stay empty, got: %s", got)
	}
}

// fakeRedactor proves the Redactor interface is the real seam PIIMiddleware redacts through:
// swapping in a completely different implementation changes what gets redacted with no change to
// PIIMiddleware itself.
type fakeRedactor struct{ replacement string }

func (f fakeRedactor) Redact(text string) string { return f.replacement }

func TestPIIMiddleware_RedactorImplementationIsSwappable(t *testing.T) {
	body := `{"question": "anything at all"}`
	req := httptest.NewRequest(http.MethodPost, "/v1/query", strings.NewReader(body))

	got := capturedDownstreamBody(t, req, fakeRedactor{replacement: "TOTALLY DIFFERENT IMPLEMENTATION"})

	if !strings.Contains(got, "TOTALLY DIFFERENT IMPLEMENTATION") {
		t.Fatalf("expected the swapped-in Redactor's own output, got: %s", got)
	}
}
