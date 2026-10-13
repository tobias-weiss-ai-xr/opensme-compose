package api

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"testing/fstest"
	"time"

	"opensme-dev-agent/internal/checker"
	"opensme-dev-agent/internal/config"
	"opensme-dev-agent/internal/healer"
	"opensme-dev-agent/internal/knowledge"
)

// emptyRunner answers `docker ps` with no containers.
type emptyRunner struct{}

func (emptyRunner) Run(ctx context.Context, args ...string) (string, error) {
	return "", nil
}

func testKBFS() fstest.MapFS {
	return fstest.MapFS{
		"kb/stalwart.json": &fstest.MapFile{Data: []byte(`{
			"schema": 1, "service": "stalwart", "kb_version": "2026.10",
			"runbooks": [{"symptoms": ["restarting"], "diagnosis": "port collision", "remediation": ["check bind error"]}]
		}`)},
	}
}

func testServer(t *testing.T, allow bool) (*Server, string) {
	t.Helper()
	dir := t.TempDir()
	cfg := &config.Config{
		Interval: 60 * time.Second, Watch: []string{"opensme"},
		StateDir: dir, AllowHeal: allow, APIAddr: "0.0.0.0:0",
		LLM: config.LLMConfig{Backend: "none"},
	}
	kb, err := knowledge.Load(testKBFS(), "kb")
	if err != nil {
		t.Fatal(err)
	}
	s, err := New(cfg, kb, checker.New(cfg.Watch, emptyRunner{}), healer.New(allow, nil), nil)
	if err != nil {
		t.Fatal(err)
	}
	return s, dir
}

func TestHealRejectedWithoutConsent(t *testing.T) {
	s, _ := testServer(t, false)
	req := httptest.NewRequest(http.MethodPost, "/heal",
		strings.NewReader(`{"action":"restart","target":"opensme-sogo-1"}`))
	rec := httptest.NewRecorder()
	s.Handler().ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d", rec.Code)
	}
	var rc healer.Receipt
	if err := json.Unmarshal(rec.Body.Bytes(), &rc); err != nil {
		t.Fatal(err)
	}
	if rc.Executed {
		t.Error("heal without consent must be a dry-run receipt")
	}
	if !strings.Contains(rc.Output, "dry-run") {
		t.Errorf("receipt = %q", rc.Output)
	}
}

func TestHealReceiptInHistoryAndEvidence(t *testing.T) {
	s, _ := testServer(t, false)
	h := s.Handler()
	body := `{"action":"restart","target":"c1"}`
	for i := 0; i < 2; i++ {
		h.ServeHTTP(httptest.NewRecorder(),
			httptest.NewRequest(http.MethodPost, "/heal", strings.NewReader(body)))
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/history", nil))
	var hist []healer.Receipt
	if err := json.Unmarshal(rec.Body.Bytes(), &hist); err != nil || len(hist) != 2 {
		t.Fatalf("history = %s err=%v", rec.Body.String(), err)
	}
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/evidence", nil))
	var ev []EvidenceEntry
	if err := json.Unmarshal(rec.Body.Bytes(), &ev); err != nil || len(ev) != 2 {
		t.Fatalf("evidence = %s err=%v", rec.Body.String(), err)
	}
	for _, e := range ev {
		if e.Kind != "heal" {
			t.Errorf("evidence kind = %s", e.Kind)
		}
	}
}

func TestReadyGatesOnReconcile(t *testing.T) {
	s, _ := testServer(t, false)
	h := s.Handler()
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/ready", nil))
	if rec.Code != http.StatusServiceUnavailable {
		t.Errorf("pre-reconcile ready = %d, want 503", rec.Code)
	}
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/healthz", nil))
	if rec.Code != http.StatusOK {
		t.Errorf("healthz = %d, want 200", rec.Code)
	}
}

func TestPersistenceRoundTrip(t *testing.T) {
	s, dir := testServer(t, false)
	s.Handler().ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodPost, "/heal",
		strings.NewReader(`{"action":"wait","target":""}`)))
	if err := s.Save(); err != nil {
		t.Fatal(err)
	}
	raw, err := os.ReadFile(filepath.Join(dir, "state.json"))
	if err != nil {
		t.Fatalf("state not persisted: %v", err)
	}
	if !strings.Contains(string(raw), "history") {
		t.Errorf("state.json = %s", raw)
	}
	cfg := &config.Config{Interval: time.Second, StateDir: dir, Watch: []string{"opensme"},
		LLM: config.LLMConfig{Backend: "none"}}
	kb, _ := knowledge.Load(testKBFS(), "kb")
	s2, err := New(cfg, kb, checker.New(cfg.Watch, emptyRunner{}), healer.New(false, nil), nil)
	if err != nil {
		t.Fatal(err)
	}
	if len(s2.history) != 1 {
		t.Errorf("reloaded history = %d, want 1", len(s2.history))
	}
}

func TestAnonymize(t *testing.T) {
	s, _ := testServer(t, false)
	raw := `postgresql://user:pw@10.20.30.40:5432/db API_KEY=abcd1234 key=bare-secret-9 host=traefik.internal path=/home/alice/data token="xyz" standalone 192.168.1.1 stuff`
	clean, recs := s.Anonymize(raw)
	for _, leak := range []string{"abcd1234", "bare-secret-9", "10.20.30.40", "/home/alice", "traefik.internal", `token="xyz"`, "192.168.1.1"} {
		if strings.Contains(clean, leak) {
			t.Errorf("raw value %q survived anonymization: %s", leak, clean)
		}
	}
	for _, want := range []string{"<conn-str>", "***", "<ip>", "/home/<user>", "<host>"} {
		if !strings.Contains(clean, want) {
			t.Errorf("expected %q in %q", want, clean)
		}
	}
	// Raw values must never enter the evidence log.
	for _, e := range s.evidence {
		for _, leak := range []string{"abcd1234", "10.20.30.40", "/home/alice", "traefik.internal"} {
			if strings.Contains(e.What+e.Where+e.Why, leak) {
				t.Errorf("evidence leaked raw value %q: %+v", leak, e)
			}
		}
	}
	if len(recs) < 3 {
		t.Errorf("expected ≥3 strip categories, got %+v", recs)
	}
}

func TestLLMDisabledByDefault(t *testing.T) {
	if NewLLM("none", "", "", "", 0) != nil {
		t.Error("backend none must produce nil LLM (no dial-out)")
	}
	if NewLLM("", "", "", "", 0) != nil {
		t.Error("empty backend must produce nil LLM")
	}
	l := NewLLM("ollama", "http://x", "", "m", 0)
	if !l.Enabled() {
		t.Error("configured backend must be enabled")
	}
	// Analyze against a nil-server guard: it must error, never dial.
	if _, err := l.Analyze(context.Background(), nil, checker.Finding{}); err == nil {
		t.Error("nil server must error, not dial")
	}
}

func TestStatusEndpoint(t *testing.T) {
	s, _ := testServer(t, false)
	rec := httptest.NewRecorder()
	s.Handler().ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/status", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d", rec.Code)
	}
	var out map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatal(err)
	}
	if _, ok := out["kb_services"]; !ok {
		t.Errorf("status missing kb_services: %s", rec.Body.String())
	}
}

func TestMetricsEndpoint(t *testing.T) {
	s, _ := testServer(t, false)
	// Trigger a reconcile to increment counters
	s.Reconcile(context.Background())
	h := s.Handler()
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/metrics", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("metrics = %d", rec.Code)
	}
	body := rec.Body.String()
	for _, want := range []string{
		"dev_agent_reconciles_total",
		"dev_agent_findings_current",
		"dev_agent_heals_total",
		"dev_agent_llm_calls_total",
		"dev_agent_llm_errors_total",
		"dev_agent_llm_latency_seconds_last",
		"dev_agent_history_entries",
	} {
		if !strings.Contains(body, want) {
			t.Errorf("metrics missing %q: %s", want, body)
		}
	}
	if !strings.Contains(rec.Header().Get("Content-Type"), "text/plain") {
		t.Errorf("metrics content-type should be text/plain: %s", rec.Header().Get("Content-Type"))
	}
}

func TestKnowledgeEndpoint(t *testing.T) {
	s, _ := testServer(t, false)
	h := s.Handler()
	// List all services
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/knowledge", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("knowledge list = %d", rec.Code)
	}
	var services []string
	if err := json.Unmarshal(rec.Body.Bytes(), &services); err != nil {
		t.Fatal(err)
	}
	if len(services) == 0 || services[0] != "stalwart" {
		t.Errorf("knowledge list = %v", services)
	}
	// Query by service
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/knowledge?service=stalwart", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("knowledge service = %d", rec.Code)
	}
	var rbs []knowledge.Runbook
	if err := json.Unmarshal(rec.Body.Bytes(), &rbs); err != nil {
		t.Fatal(err)
	}
	if len(rbs) == 0 {
		t.Errorf("stalwart runbooks empty")
	}
	// Query by symptom
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/knowledge?symptom=restarting", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("knowledge symptom = %d", rec.Code)
	}
	var matches []knowledge.Match
	if err := json.Unmarshal(rec.Body.Bytes(), &matches); err != nil {
		t.Fatal(err)
	}
	if len(matches) == 0 {
		t.Errorf("symptom 'restarting' should match")
	}
	// source duty: the answer evidence must carry provenance
	m := matches[0]
	if m.Source != "stalwart.json" {
		t.Errorf("source duty: Match.Source = %q, want stalwart.json", m.Source)
	}
	if m.KBVersion != "2026.10" {
		t.Errorf("source duty: Match.KBVersion = %q, want 2026.10", m.KBVersion)
	}
}

func TestHistoryCap(t *testing.T) {
	s, _ := testServer(t, false)
	s.cfg.HistoryMax = 3
	h := s.Handler()
	body := `{"action":"wait","target":""}`
	for i := 0; i < 5; i++ {
		h.ServeHTTP(httptest.NewRecorder(),
			httptest.NewRequest(http.MethodPost, "/heal", strings.NewReader(body)))
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/history", nil))
	var hist []healer.Receipt
	if err := json.Unmarshal(rec.Body.Bytes(), &hist); err != nil {
		t.Fatal(err)
	}
	if len(hist) != 3 {
		t.Errorf("history = %d, want 3 (capped at HistoryMax=3)", len(hist))
	}
}

func TestAnonymizeExpanded(t *testing.T) {
	s, _ := testServer(t, false)
	raw := `postgres://user:secretpass@10.20.30.40:5432/db ` +
		`Authorization: Basic dXNlcjpwYXNz ` +
		`token=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c ` +
		`email=admin@example.com `
	clean, _ := s.Anonymize(raw)
	// Raw values must not survive.
	for _, leak := range []string{
		"secretpass", "dXNlcjpwYXNz", "eyJhbGciOiJIUzI1NiJ9", "admin@example.com", "10.20.30.40",
	} {
		if strings.Contains(clean, leak) {
			t.Errorf("raw value %q survived: %s", leak, clean)
		}
	}
	// Connection string and email must be redacted.
	for _, want := range []string{"<conn-str>", "<email>", "***"} {
		if !strings.Contains(clean, want) {
			t.Errorf("expected %q in %q", want, clean)
		}
	}
}
