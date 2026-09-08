package middleware

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"strconv"
)

// MaxQuestionLength MUST match services/orchestrator/app/schemas.py's MAX_QUESTION_LENGTH exactly
// -- see that constant's own doc comment for the full derivation from EMBED_GGUF_N_CTX=2048. This
// is enforced in TWO places on purpose (this gateway AND the orchestrator, red-team fix
// 2026-09-07): the orchestrator answers unauthenticated on its own public hostname today (see
// infra/deploy/fly.orchestrator.toml's own comment, "Defect C"), completely bypassing this
// gateway, so neither check alone is sufficient. The two cannot share code across a language
// boundary, so keeping the same NUMBER in both places -- each half naming the other in its own
// comment -- is what keeps them from silently drifting apart.
const MaxQuestionLength = 4000

// maxRequestBodyBytes bounds how many bytes QuestionLengthMiddleware will even read off the wire,
// independent of (and checked before) the character-count check below. 4 is the maximum number of
// bytes a single UTF-8 code point can take, so MaxQuestionLength runes can never take more than
// MaxQuestionLength*4 = 16000 bytes; 32000 rounds that up generously to also cover the JSON
// envelope itself (`{"question":"..."}`) and any other field a future request shape adds, with
// real headroom. The point of this separate, cheaper check is that a caller must never be able to
// force this middleware to buffer an unbounded body in memory just to discover it is oversized --
// http.MaxBytesReader stops reading the instant this many bytes have been consumed, which is
// exactly the protection Go's own net/http documentation recommends for this situation.
const maxRequestBodyBytes = 32000

// questionOnlyField mirrors just enough of the request body's shape to read the "question" field
// without committing to app/schemas.py::QueryRequest's exact shape -- the same convention
// internal/middleware/pii.go's requestFields already uses, and for the same reason: a body that is
// not JSON, or has no "question" field, or whose "question" field is not a plain string, must pass
// through UNCHANGED rather than being guessed at.
type questionOnlyField struct {
	Question *string `json:"question"`
}

// QuestionLengthMiddleware rejects an oversized "question" field BEFORE it ever reaches the
// orchestrator (see MaxQuestionLength's own comment for why this exists in two places), with a
// response body that names no internal detail at all -- not Go's own "http: request body too
// large" wording, not a stack trace, nothing a caller could not already have guessed: "your
// question is too long, please shorten it." Any request whose body is not a JSON object, has no
// "question" field, or whose "question" field is not a plain string passes through UNCHANGED,
// exactly PIIMiddleware's own convention -- so GET /v1/sources/status (no body) and any future
// route with a different body shape are never second-guessed by this middleware.
func QuestionLengthMiddleware() func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.Body == nil || r.Body == http.NoBody {
				next.ServeHTTP(w, r)
				return
			}

			r.Body = http.MaxBytesReader(w, r.Body, maxRequestBodyBytes)
			raw, err := io.ReadAll(r.Body)
			if err != nil {
				// http.MaxBytesReader's own error names Go's internal wording ("http: request
				// body too large" as of Go 1.19+) -- never returned verbatim. A body this large is
				// itself proof enough that the question exceeds MaxQuestionLength (see
				// maxRequestBodyBytes's own comment: no question within the real limit could ever
				// produce a body this size), so the same clean message applies either way.
				writeQuestionTooLong(w)
				return
			}
			if len(raw) == 0 {
				r.Body = io.NopCloser(bytes.NewReader(raw))
				next.ServeHTTP(w, r)
				return
			}

			var fields questionOnlyField
			if jsonErr := json.Unmarshal(raw, &fields); jsonErr != nil || fields.Question == nil {
				r.Body = io.NopCloser(bytes.NewReader(raw))
				r.ContentLength = int64(len(raw))
				next.ServeHTTP(w, r)
				return
			}

			// Rune count, not byte count: this must match Python's `len(str)` semantics
			// (app/schemas.py's Field(max_length=...)), which counts Unicode code points, not
			// UTF-8 bytes -- a byte-count check here would reject some legitimate multi-byte-heavy
			// questions the orchestrator would otherwise have accepted, and disagreement between
			// the two layers is exactly the drift MaxQuestionLength's own comment warns about.
			if len([]rune(*fields.Question)) > MaxQuestionLength {
				writeQuestionTooLong(w)
				return
			}

			r.Body = io.NopCloser(bytes.NewReader(raw))
			r.ContentLength = int64(len(raw))
			next.ServeHTTP(w, r)
		})
	}
}

func writeQuestionTooLong(w http.ResponseWriter) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusUnprocessableEntity)
	_, _ = w.Write([]byte(
		`{"error":"That question is too long. Please shorten it to ` +
			strconv.Itoa(MaxQuestionLength) + ` characters or fewer and try again."}`,
	))
}
