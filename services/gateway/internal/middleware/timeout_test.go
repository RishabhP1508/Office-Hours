package middleware

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestTimeout_SetsADeadlineOnTheRequestContext(t *testing.T) {
	var hasDeadline bool
	var deadline time.Time
	handler := Timeout(50 * time.Millisecond)(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		deadline, hasDeadline = r.Context().Deadline()
	}))

	before := time.Now()
	req := httptest.NewRequest(http.MethodGet, "/", nil)
	handler.ServeHTTP(httptest.NewRecorder(), req)

	if !hasDeadline {
		t.Fatal("expected the wrapped handler's context to carry a deadline")
	}
	if !deadline.After(before) {
		t.Fatalf("expected the deadline (%v) to be after the request started (%v)", deadline, before)
	}
}

// No sleep call: waits on the context's own Done channel, which is exactly the event under test,
// with time.After only as a deadlock guard so a real failure reports promptly instead of hanging
// the test suite forever.
func TestTimeout_CancelsTheContextOnceTheDurationElapses(t *testing.T) {
	done := make(chan struct{})
	handler := Timeout(1 * time.Millisecond)(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		<-r.Context().Done()
		close(done)
	}))

	req := httptest.NewRequest(http.MethodGet, "/", nil)
	handler.ServeHTTP(httptest.NewRecorder(), req)

	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("context was never canceled by Timeout")
	}
}

func TestTimeToFirstByte_CancelsIfStopIsNeverCalled(t *testing.T) {
	ctx, stop := TimeToFirstByte(context.Background(), 1*time.Millisecond)
	defer stop()

	select {
	case <-ctx.Done():
	case <-time.After(5 * time.Second):
		t.Fatal("context was never canceled")
	}
}

// This is the exact behavior ProxyStream depends on: calling stop() before the timer fires must
// lift the bound for the rest of the context's life, so a real 20-140s stream is not cut off at
// 15s just because it took a moment to receive headers.
func TestTimeToFirstByte_StopLiftsTheBoundBeforeItFires(t *testing.T) {
	parent, cancelParent := context.WithCancel(context.Background())
	defer cancelParent()

	ctx, stop := TimeToFirstByte(parent, 20*time.Millisecond)
	stop() // called immediately, well before the 20ms timer would fire

	select {
	case <-ctx.Done():
		t.Fatal("context was canceled even though stop() ran before the timer fired")
	case <-time.After(100 * time.Millisecond):
		// Still alive well past the original bound -- stop() genuinely lifted it.
	}

	cancelParent()
	select {
	case <-ctx.Done():
	case <-time.After(5 * time.Second):
		t.Fatal("context should still respond to the PARENT context's own cancellation")
	}
}

// This is what lets ProxyStream tell "our own timer fired" (report 504) apart from "the client
// disconnected" (a plain context.Canceled with no special cause) -- both look identical to
// ctx.Err() alone, so ProxyStream reads context.Cause(ctx) instead. See
// internal/proxy/proxy.go's ProxyStream for the real consumer of this distinction.
func TestTimeToFirstByte_CauseIsErrTimeToFirstByteExceededWhenTheTimerFires(t *testing.T) {
	ctx, stop := TimeToFirstByte(context.Background(), 1*time.Millisecond)
	defer stop()

	select {
	case <-ctx.Done():
	case <-time.After(5 * time.Second):
		t.Fatal("context was never canceled")
	}

	if cause := context.Cause(ctx); !errors.Is(cause, ErrTimeToFirstByteExceeded) {
		t.Fatalf("expected cause to be ErrTimeToFirstByteExceeded, got %v", cause)
	}
}

func TestTimeToFirstByte_CauseIsNotErrTimeToFirstByteExceededWhenTheParentCancelsFirst(t *testing.T) {
	parent, cancelParent := context.WithCancel(context.Background())
	ctx, stop := TimeToFirstByte(parent, time.Minute)
	defer stop()

	cancelParent()

	select {
	case <-ctx.Done():
	case <-time.After(5 * time.Second):
		t.Fatal("context was never canceled")
	}

	if cause := context.Cause(ctx); errors.Is(cause, ErrTimeToFirstByteExceeded) {
		t.Fatalf("expected a plain parent cancellation, not ErrTimeToFirstByteExceeded, got %v", cause)
	}
}

func TestTimeToFirstByte_ParentCancellationStillCancelsTheChild(t *testing.T) {
	parent, cancelParent := context.WithCancel(context.Background())
	ctx, stop := TimeToFirstByte(parent, time.Minute)
	defer stop()

	cancelParent()

	select {
	case <-ctx.Done():
	case <-time.After(5 * time.Second):
		t.Fatal("expected the derived context to be canceled when the parent is canceled")
	}
}
