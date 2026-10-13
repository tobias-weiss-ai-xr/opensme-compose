package api

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"testing/fstest"

	"opensme-dev-agent/internal/checker"
	"opensme-dev-agent/internal/config"
	"opensme-dev-agent/internal/healer"
	"opensme-dev-agent/internal/knowledge"
)

// ollamaCounter serves fake LLM completions and counts outbound requests.
func ollamaCounter(t *testing.T, calls *atomic.Int64) *httptest.Server {
	t.Helper()
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"response":"root cause"}`))
	}))
}

func budgetServer(t *testing.T, maxRequests int) (*Server, *atomic.Int64) {
	t.Helper()
	var calls atomic.Int64
	srv := ollamaCounter(t, &calls)
	t.Cleanup(srv.Close)
	kb, err := knowledge.Load(fstest.MapFS{"kb/app.json": &fstest.MapFile{Data: []byte(`{
		"schema": 1, "service": "app",
		"runbooks": [{"symptoms": ["restarting"], "diagnosis": "port collision", "remediation": ["check bind"]}]
	}`)}}, "kb")
	if err != nil {
		t.Fatal(err)
	}
	chk := checker.New([]string{"opensme"}, dualRunner{})
	cfg := &config.Config{
		Watch: []string{"opensme"}, StateDir: t.TempDir(), HistoryMax: 100,
		LLM: config.LLMConfig{Backend: "ollama", URL: srv.URL, Model: "m", MaxRequests: maxRequests},
	}
	hl := healer.New(true, nil)
	llm := NewLLM(cfg.LLM.Backend, cfg.LLM.URL, cfg.LLM.Key, cfg.LLM.Model, cfg.LLM.MaxRequests)
	s, err := New(cfg, kb, chk, hl, llm)
	if err != nil {
		t.Fatal(err)
	}
	return s, &calls
}

// An exhausted per-run budget must stop outbound calls with a sentinel error
// and never exceed the limit on the wire.
func TestLLMBudgetBlocksExcessCalls(t *testing.T) {
	s, calls := budgetServer(t, 2)
	// Three distinct findings (varying Detail → distinct cache keys) with a
	// budget of 2 must allow exactly 2 outbound calls.
	for i := 0; i < 3; i++ {
		f := checker.Finding{Container: "opensme-app-1", Service: "app", State: "running",
			Health: "unhealthy", Symptoms: []string{"restarting"}, Detail: fmt.Sprintf("boom-%d", i)}
		_, err := s.llm.Analyze(context.Background(), s, f)
		if i < 2 {
			if err != nil {
				t.Fatalf("call %d: unexpected error: %v", i, err)
			}
		} else if !errors.Is(err, ErrBudgetExceeded) {
			t.Fatalf("call %d: want ErrBudgetExceeded, got %v", i, err)
		}
	}
	if got := calls.Load(); got != 2 {
		t.Fatalf("backend saw %d requests, want exactly 2 (budget)", got)
	}
}

// Cache hits must never consume budget — same finding-context within TTL is
// served from cache without dialing out.
func TestLLMCacheHitsDoNotConsumeBudget(t *testing.T) {
	s, calls := budgetServer(t, 1)
	f := checker.Finding{Container: "opensme-app-1", Service: "app", State: "running",
		Health: "unhealthy", Symptoms: []string{"restarting"}, Detail: "boom"}
	if _, err := s.llm.Analyze(context.Background(), s, f); err != nil {
		t.Fatalf("first call: %v", err)
	}
	// Different finding objects, same context → cache hit, no dial.
	for i := 0; i < 4; i++ {
		if _, err := s.llm.Analyze(context.Background(), s, f); err != nil {
			t.Fatalf("repeat %d: %v", i, err)
		}
	}
	if got := calls.Load(); got != 1 {
		t.Fatalf("cache: backend saw %d requests, want 1", got)
	}
}

// Exceeded reports the budget state without dialing.
func TestLLMExceededReportsBeforeDialing(t *testing.T) {
	if l := NewLLM("ollama", "http://127.0.0.1:1", "", "m", 0); l.Exceeded() {
		t.Fatal("unlimited budget must never read as exceeded")
	}
	if l := NewLLM("ollama", "http://127.0.0.1:1", "", "m", 1); l.Exceeded() {
		t.Fatal("budget of 1 with zero calls must not be exceeded")
	}
}

// Reconcile must pause the rest of the cycle once the budget is exhausted and
// leave an auditable budget evidence entry (kind "budget").
func TestReconcileBudgetPausesAndRecordsEvidence(t *testing.T) {
	s, calls := budgetServer(t, 1)
	if err := s.Reconcile(context.Background()); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	if got := calls.Load(); got != 1 {
		t.Fatalf("reconcile dialed %d times, want exactly 1 (pause after budget)", got)
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	hasBudget := false
	for _, e := range s.evidence {
		if e.Kind == "budget" {
			hasBudget = true
		}
	}
	if !hasBudget {
		t.Fatal("expected a budget evidence entry after truncation")
	}
	if s.llmBudgetTruncations != 1 {
		t.Fatalf("llmBudgetTruncations = %d, want 1", s.llmBudgetTruncations)
	}
}

// — runners ----------------------------------------------------------------

// dualRunner returns two unhealthy containers with distinct contexts.
type dualRunner struct{}

func (dualRunner) Run(_ context.Context, args ...string) (string, error) {
	if args[0] == "ps" {
		return `{"Names":"opensme-app-1","State":"restarting","Status":"Restarting (1) 5s ago","Labels":"com.docker.compose.project=opensme,com.docker.compose.service=app"}` + "\n" +
			`{"Names":"opensme-web-1","State":"restarting","Status":"Restarting (1) 5s ago","Labels":"com.docker.compose.project=opensme,com.docker.compose.service=web"}` + "\n", nil
	}
	return "", nil
}
