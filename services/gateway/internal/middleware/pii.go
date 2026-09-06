package middleware

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"regexp"
)

// Redactor is the hook PIIMiddleware redacts through. The default implementation (NewRegexRedactor)
// is a fixed set of regexes; a different implementation (a model-based PII detector, a hosted
// redaction API, ...) can be swapped in without touching PIIMiddleware itself.
type Redactor interface {
	Redact(text string) string
}

type redactionPattern struct {
	re          *regexp.Regexp
	placeholder string
}

// defaultPatterns covers, at minimum, the four classes CLAUDE.md/the coordinator named: email
// addresses (the DoD case), phone numbers, Social Security Numbers, and USCIS "A-numbers" (Alien
// Registration Numbers). Order matters: the SSN pattern (###-##-####, unambiguous) is checked
// before the looser phone pattern, since a phone-shaped regex loose enough to catch
// "555-123-4567" would also match an SSN's digit-dash-digit shape if checked first.
var defaultPatterns = []redactionPattern{
	{
		re:          regexp.MustCompile(`(?i)[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}`),
		placeholder: "[redacted-email]",
	},
	{
		// A-number: "A" (or "a") followed by 7-9 digits, an optional separating dash/space.
		re:          regexp.MustCompile(`(?i)\bA[- ]?\d{7,9}\b`),
		placeholder: "[redacted-a-number]",
	},
	{
		re:          regexp.MustCompile(`\b\d{3}-\d{2}-\d{4}\b`),
		placeholder: "[redacted-ssn]",
	},
	{
		// Phone: an optional leading +, an optional 1-3 digit country code, then a 10-digit North
		// American-shaped number grouped with spaces, dots, dashes, or parentheses. Deliberately
		// bounded (not "any long run of digits") so it does not also eat form numbers, dates, or
		// dollar amounts a question might legitimately contain.
		re: regexp.MustCompile(
			`\+?\d{0,3}[-. ]?\(?\d{3}\)?[-. ]?\d{3}[-. ]?\d{4}\b`,
		),
		placeholder: "[redacted-phone]",
	},
}

type regexRedactor struct {
	patterns []redactionPattern
}

// NewRegexRedactor builds the default Redactor.
func NewRegexRedactor() Redactor {
	return &regexRedactor{patterns: defaultPatterns}
}

// Redact replaces every match of every pattern with a STABLE placeholder, never deletion -- a
// redacted question still reads as a sensible sentence rather than a hole where the value was.
func (r *regexRedactor) Redact(text string) string {
	for _, p := range r.patterns {
		text = p.re.ReplaceAllString(text, p.placeholder)
	}
	return text
}

// requestFields is how PIIMiddleware reads and rewrites the JSON body: as a generic field map,
// not a struct tied to app/schemas.py::QueryRequest's exact shape, so a future field this
// middleware does not know about is carried through unchanged rather than silently dropped on
// re-serialization.
type requestFields map[string]json.RawMessage

// PIIMiddleware redacts the JSON body's "question" field BEFORE the request ever leaves this
// process -- the orchestrator never receives the raw string. Any request that is not a JSON
// object, or has no "question" field, or whose "question" field is not a plain string, passes
// through with its ORIGINAL body untouched rather than being guessed at or dropped. Every other
// field in the body survives re-serialization unchanged.
func PIIMiddleware(redactor Redactor) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.Body == nil || r.Body == http.NoBody {
				next.ServeHTTP(w, r)
				return
			}

			raw, err := io.ReadAll(r.Body)
			_ = r.Body.Close()
			if err != nil {
				// Body could not even be read -- forward as-is (empty); the proxy layer will
				// surface whatever error results from an unreadable/truncated body downstream,
				// the same way it would have without this middleware in front of it at all.
				r.Body = io.NopCloser(bytes.NewReader(nil))
				r.ContentLength = 0
				next.ServeHTTP(w, r)
				return
			}
			if len(raw) == 0 {
				r.Body = io.NopCloser(bytes.NewReader(raw))
				next.ServeHTTP(w, r)
				return
			}

			redacted, changed := redactQuestionField(raw, redactor)
			if !changed {
				r.Body = io.NopCloser(bytes.NewReader(raw))
				r.ContentLength = int64(len(raw))
				next.ServeHTTP(w, r)
				return
			}

			r.Body = io.NopCloser(bytes.NewReader(redacted))
			r.ContentLength = int64(len(redacted))
			next.ServeHTTP(w, r)
		})
	}
}

// redactQuestionField parses `raw` as a JSON object, redacts its "question" field if present and
// a plain string, and re-serializes. Returns (raw, false) unchanged for anything it does not
// recognize (not JSON, no "question" field, "question" not a string) rather than guessing.
func redactQuestionField(raw []byte, redactor Redactor) (out []byte, changed bool) {
	var fields requestFields
	if err := json.Unmarshal(raw, &fields); err != nil {
		return raw, false
	}

	questionRaw, ok := fields["question"]
	if !ok {
		return raw, false
	}

	var question string
	if err := json.Unmarshal(questionRaw, &question); err != nil {
		// "question" is present but not a plain JSON string -- leave the whole body alone rather
		// than guessing at a shape it does not have.
		return raw, false
	}

	redactedQuestion, err := json.Marshal(redactor.Redact(question))
	if err != nil {
		return raw, false
	}
	fields["question"] = redactedQuestion

	redactedBody, err := json.Marshal(fields)
	if err != nil {
		return raw, false
	}
	return redactedBody, true
}
