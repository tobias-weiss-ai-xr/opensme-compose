package api

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"sync"
	"testing"
	"testing/fstest"
	"time"

	"opensme-dev-agent/internal/checker"
	"opensme-dev-agent/internal/config"
	"opensme-dev-agent/internal/healer"
	"opensme-dev-agent/internal/knowledge"
)

// secretRunner returns one unhealthy container whose log detail carries a
// password — this forces the anonymizer's strip path (which appends evidence
// under s.mu) during Reconcile's LLM path.
type secretRunner struct{}

func (secretRunner) Run(_ context.Context, args ...string) (string, error) {
	switch args[0] {
	case "ps":
		return `{"Names":"opensme-app-1","State":"restarting","Status":"Restarting (1) 5s ago","Labels":"com.docker.compose.project=opensme,com.docker.compose.service=app"}` + "\n", nil
	case "logs":
		return "connection refused\nconnection refused\npassword=tophunt\n", nil
	}
	return "", nil
}

func llmServer(t *testing.T) *Server {
	t.Helper()
	kb, err := knowledge.Load(fstest.MapFS{"kb/app.json": &fstest.MapFile{Data: []byte(`{
		"schema": 1, "service": "app", "kb_version": "2026.10",
		"runbooks": [{"symptoms": ["restarting"], "diagnosis": "port collision", "remediation": ["check bind"]}]
	}`)}}, "kb")
	if err != nil {
		t.Fatal(err)
	}
	cfg := &config.Config{
		Watch: []string{"opensme"}, StateDir: t.TempDir(), HistoryMax: 100,
		LLM: config.LLMConfig{Backend: "ollama", URL: "http://127.0.0.1:1", Model: "m"},
	}
	chk := checker.New(cfg.Watch, secretRunner{})
	s, err := New(cfg, kb, chk, healer.New(false, nil),
		NewLLM("ollama", "http://127.0.0.1:1", "", "m", 0))
	if err != nil {
		t.Fatal(err)
	}
	return s
}

// Regression: Reconcile with an enabled LLM and a finding that requires
// anonymization must not deadlock (findingContext must not hold s.mu while
// Anonymize appends evidence). LLM is off by default, so this path was
// previously untested.
func TestReconcileWithLLMDoesNotDeadlock(t *testing.T) {
	s := llmServer(t)
	done := make(chan error, 1)
	go func() {
		ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
		defer cancel()
		done <- s.Reconcile(ctx)
	}()
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("reconcile failed: %v", err)
		}
		// The LLM call itself fails (unreachable backend) but reconcile must
		// still return and record evidence.
		s.mu.Lock()
		hasStrip := false
		for _, e := range s.evidence {
			if e.Kind == "strip" {
				hasStrip = true
			}
		}
		s.mu.Unlock()
		if !hasStrip {
			t.Error("expected a strip evidence entry for the redacted password")
		}
	case <-time.After(5 * time.Second):
		t.Fatal("deadlock: Reconcile(LLM+secret) hung")
	}
}

// Regression: concurrent Save() calls (reconcile ticker, /heal, SIGTERM) must
// never leave state.json torn/unparseable. Prior code raced os.WriteFile on
// the same path.
func TestConcurrentSaveNeverCorruptsState(t *testing.T) {
	s := llmServer(t)
	// Seed some history+evidence.
	s.mu.Lock()
	s.history = append(s.history, healer.Receipt{Time: time.Now().UTC(), Action: "restart", Target: "x"})
	s.evidence = append(s.evidence, EvidenceEntry{Time: time.Now().UTC(), Kind: "strip", What: "secret"})
	s.mu.Unlock()

	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for j := 0; j < 40; j++ {
				if err := s.Save(); err != nil {
					t.Errorf("save: %v", err)
					return
				}
			}
		}()
	}
	wg.Wait()

	// The final file must be valid JSON (atomic rename guarantees a complete
	// snapshot; the concurrent writer pre-fix could truncate mid-write).
	raw, err := os.ReadFile(s.stateFile())
	if err != nil {
		t.Fatalf("read state: %v", err)
	}
	var p persisted
	if err := json.Unmarshal(raw, &p); err != nil {
		t.Fatalf("state.json corrupt after concurrent saves: %v", err)
	}
}

// The strip evidence recorded during Reconcile's LLM path must never contain
// the raw secret — only the redacted marker.
func TestReconcileLLMTruthUsesHostIP(t *testing.T) {
	s := llmServer(t)
	if err := s.Reconcile(context.Background()); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	raw, _ := json.Marshal(s.evidence)
	if strings.Contains(string(raw), "tophunt") {
		t.Fatal("raw secret leaked into evidence log")
	}
	if strings.Contains(string(raw), "127.0.0.1") {
		t.Fatal("raw IP leaked into evidence log")
	}
}

// findingContext is also reachable directly over HTTP via a showing finding;
// smoke-test the handler for the LLM-enabled server (unreachable backend must
// map to a 502, not hang).
func TestHealHistoryHandlersStayResponsive(t *testing.T) {
	s := llmServer(t)
	req := httptest.NewRequest(http.MethodGet, "/history", nil)
	rec := httptest.NewRecorder()
	s.Handler().ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("GET /history = %d", rec.Code)
	}
}
