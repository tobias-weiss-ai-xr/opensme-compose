// Package api serves the dev-agent REST surface (GET /status, /healthz,
// /ready, /history, /evidence; POST /heal) and owns the strip-then-review
// anonymizer (dev-agent-privacy spec).
package api

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"opensme-dev-agent/internal/checker"
	"opensme-dev-agent/internal/config"
	"opensme-dev-agent/internal/healer"
	"opensme-dev-agent/internal/knowledge"
)

// EvidenceEntry is one auditable record: an anonymizer strip operation, a
// heal action, or a budget truncation. Raw values NEVER enter this log.
type EvidenceEntry struct {
	Time  time.Time `json:"time"`
	Kind  string    `json:"kind"` // "strip" | "heal" | "budget" | "draft" | "commit"
	What  string    `json:"what"`
	Where string    `json:"where"`
	Why   string    `json:"why"`
}

// Draft is an inert agent-generated draft (classification, reply, tags, ...).
// It only becomes effective once a human approves it (agent-saga: AS2/AS3/AS5
// draft → human-approval → commit; a draft is never committed on its own).
// Raw text may enter the draft body; it never enters the evidence log.
type Draft struct {
	ID    string `json:"id"`
	Kind  string `json:"kind"`
	Text  string `json:"text"`
	Where string `json:"where"`
	// Major marks a major decision (HITL: always human-approved, flagged in
	// the audit trail). Rejected+Reason record the human's verdict when a
	// draft is turned down — the feedback signal the agent learns from.
	// Expired marks a pending draft the TTL policy retired (opt-in; safe
	// direction — nothing was ever committed).
	Major     bool      `json:"major,omitempty"`
	Approved  bool      `json:"approved"`
	Rejected  bool      `json:"rejected"`
	Reason    string    `json:"reason,omitempty"`
	CreatedAt time.Time `json:"created_at"`
	Expired   bool      `json:"expired,omitempty"`
}

// StripRecord describes one anonymizer replacement (no raw values).
type StripRecord struct {
	What  string
	Where string
	Why   string
}

// Server is the agent's HTTP surface and reconcile state.
type Server struct {
	cfg  *config.Config
	kb   *knowledge.Store
	chk  *checker.Checker
	heal *healer.Healer
	llm  *LLM
	mu   sync.Mutex
	// saveMu serializes state.json writes (used from the SIGTERM handler, the
	// reconcile ticker and /heal — concurrently) and pairs each write with an
	// atomic temp+rename so a reader never observes a torn file.
	saveMu sync.Mutex
	start  time.Time

	containers []checker.Finding
	matches    map[string][]knowledge.Match
	history    []healer.Receipt
	evidence   []EvidenceEntry
	checked    bool

	// Counters for /metrics (Prometheus format)
	reconciles           int
	healCount            int
	llmCalls             int
	llmErrors            int
	llmLatency           time.Duration
	llmBudgetTruncations int

	// draft gate (agent-saga): drafts are inert until human approval.
	drafts   map[string]*Draft
	draftSeq int

	// AH1: observability counters for /status
	llmPromptTokens   int
	llmCompletionTokens int
}

// New wires the server. It loads persisted history/evidence from the state
// dir if present.
func New(cfg *config.Config, kb *knowledge.Store, chk *checker.Checker, hl *healer.Healer, llm *LLM) (*Server, error) {
	s := &Server{cfg: cfg, kb: kb, chk: chk, heal: hl, llm: llm, start: time.Now().UTC(), matches: map[string][]knowledge.Match{}, drafts: map[string]*Draft{}}
	s.load()
	return s, nil
}

// Handler returns the HTTP mux (private, compose-network only).
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/status", s.handleStatus)
	mux.HandleFunc("/healthz", s.handleHealthz)
	mux.HandleFunc("/ready", s.handleReady)
	mux.HandleFunc("/history", s.handleHistory)
	mux.HandleFunc("/evidence", s.handleEvidence)
	mux.HandleFunc("/heal", s.handleHeal)
	mux.HandleFunc("/draft", s.handleDraft)
	mux.HandleFunc("/drafts", s.handleDrafts)
	mux.HandleFunc("/draft/{id}/approve", s.handleDraftApprove)
	mux.HandleFunc("/draft/{id}/reject", s.handleDraftReject)
	mux.HandleFunc("/feedback", s.handleFeedback)
	mux.HandleFunc("/metrics", s.handleMetrics)
	mux.HandleFunc("/knowledge", s.handleKnowledge)
	return mux
}

// Reconcile runs one check pass: classify containers, match knowledge,
// optionally send ANONYMIZED context to the LLM, persist state.
func (s *Server) Reconcile(ctx context.Context) error {
	findings, err := s.chk.Inspect(ctx)
	if err != nil {
		slog.Error("reconcile: inspect failed", "err", err)
		return err
	}
	// Log samples may contain secrets — strip BEFORE anything is stored or served.
	for i := range findings {
		if findings[i].Detail != "" {
			findings[i].Detail, _ = s.Anonymize(findings[i].Detail)
		}
	}
	s.mu.Lock()
	s.containers = findings
	s.matches = map[string][]knowledge.Match{}
	s.reconciles++
	s.mu.Unlock()
	s.expireStale(time.Now())

	budgetPaused := false
	for _, f := range findings {
		if budgetPaused {
			break
		}
		for _, sym := range f.Symptoms {
			for _, m := range s.kb.MatchSymptom(sym) {
				s.addMatch(f.Service, m)
			}
		}
		// LLM analysis: opt-in only, anonymized only. Budget pauses the
		// cycle (agent-guardrails): once DEV_AGENT_LLM_MAX_REQUESTS is spent,
		// no further dial-out and one auditable "budget" evidence entry.
		if s.llm != nil && s.llm.Enabled() {
			start := time.Now()
			analysis, err := s.llm.Analyze(ctx, s, f)
			s.mu.Lock()
			s.llmCalls++
			s.llmLatency = time.Since(start)
			if err != nil {
				s.llmErrors++
			}
			s.mu.Unlock()
			if errors.Is(err, ErrBudgetExceeded) {
				s.mu.Lock()
				s.llmBudgetTruncations++
				s.mu.Unlock()
				s.addEvidence(EvidenceEntry{Time: time.Now().UTC(), Kind: "budget",
					What:  "llm budget exhausted",
					Where: f.Container, Why: "no further outbound LLM calls this run"})
				budgetPaused = true
				break
			}
			if err != nil {
				slog.Warn("llm analysis failed", "container", f.Container, "err", err)
				// The error text can embed the backend URL/host/IP — run it
				// through the anonymizer so raw values never reach the evidence
				// log (dev-agent-privacy: strip-then-review).
				why, _ := s.Anonymize(err.Error())
				s.addEvidence(EvidenceEntry{Time: time.Now().UTC(), Kind: "strip",
					What: "llm analysis error", Where: f.Container, Why: why})
				continue
			}
			s.addMatch(f.Service, knowledge.Match{Service: f.Service, Runbook: knowledge.Runbook{
				Symptoms: f.Symptoms, Diagnosis: "llm: " + analysis,
			}})
		}
	}
	s.mu.Lock()
	s.checked = true
	s.mu.Unlock()
	slog.Info("reconcile complete", "findings", len(findings), "matches", len(s.matches))
	return s.Save()
}

func (s *Server) addMatch(service string, m knowledge.Match) {
	s.mu.Lock()
	s.matches[service] = append(s.matches[service], m)
	s.mu.Unlock()
}

func (s *Server) addEvidence(e EvidenceEntry) {
	s.mu.Lock()
	s.evidence = append(s.evidence, e)
	s.trimLocked()
	s.mu.Unlock()
}

// trimLocked caps history and evidence to cfg.HistoryMax entries (newest
// kept). Caller must hold s.mu.
func (s *Server) trimLocked() {
	max := s.cfg.HistoryMax
	if max <= 0 {
		max = 100 // safe default if misconfigured
	}
	if len(s.history) > max {
		s.history = s.history[len(s.history)-max:]
	}
	if len(s.evidence) > max {
		s.evidence = s.evidence[len(s.evidence)-max:]
	}
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func (s *Server) handleStatus(w http.ResponseWriter, _ *http.Request) {
	s.mu.Lock()
	defer s.mu.Unlock()

	// AH1: compute evidence counts by kind
	eviCounts := make(map[string]int)
	for _, e := range s.evidence {
		eviCounts[e.Kind]++
	}

	writeJSON(w, http.StatusOK, map[string]any{
		"started_at":   s.start,
		"config":       s.cfg.String(),
		"containers":   s.containers,
		"matches":      s.matches,
		"kb_services":  s.kb.Services(),
		"history_size": len(s.history),
		// AH1: observability
		"evidence_counts":      eviCounts,
		"llm_tokens": map[string]int{
			"prompt":    s.llmPromptTokens,
			"completion": s.llmCompletionTokens,
		},
	})
}

func (s *Server) handleHealthz(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func (s *Server) handleReady(w http.ResponseWriter, _ *http.Request) {
	s.mu.Lock()
	ready := s.checked
	s.mu.Unlock()
	if !ready {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"status": "no reconcile yet"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "ready"})
}

func (s *Server) handleHistory(w http.ResponseWriter, _ *http.Request) {
	s.mu.Lock()
	defer s.mu.Unlock()
	writeJSON(w, http.StatusOK, s.history)
}

func (s *Server) handleEvidence(w http.ResponseWriter, _ *http.Request) {
	s.mu.Lock()
	defer s.mu.Unlock()
	writeJSON(w, http.StatusOK, s.evidence)
}

// handleMetrics emits Prometheus-format metrics for the agent.
func (s *Server) handleMetrics(w http.ResponseWriter, _ *http.Request) {
	s.mu.Lock()
	defer s.mu.Unlock()
	var b strings.Builder
	fmt.Fprintf(&b, "# HELP dev_agent_reconciles_total Total reconcile passes\n")
	fmt.Fprintf(&b, "# TYPE dev_agent_reconciles_total counter\n")
	fmt.Fprintf(&b, "dev_agent_reconciles_total %d\n", s.reconciles)
	fmt.Fprintf(&b, "# HELP dev_agent_findings_current Current unhealthy findings\n")
	fmt.Fprintf(&b, "# TYPE dev_agent_findings_current gauge\n")
	fmt.Fprintf(&b, "dev_agent_findings_current %d\n", len(s.containers))
	fmt.Fprintf(&b, "# HELP dev_agent_heals_total Total heal actions attempted\n")
	fmt.Fprintf(&b, "# TYPE dev_agent_heals_total counter\n")
	fmt.Fprintf(&b, "dev_agent_heals_total %d\n", s.healCount)
	fmt.Fprintf(&b, "# HELP dev_agent_llm_calls_total Total LLM analysis calls\n")
	fmt.Fprintf(&b, "# TYPE dev_agent_llm_calls_total counter\n")
	fmt.Fprintf(&b, "dev_agent_llm_calls_total %d\n", s.llmCalls)
	fmt.Fprintf(&b, "# HELP dev_agent_llm_errors_total Total LLM analysis errors\n")
	fmt.Fprintf(&b, "# TYPE dev_agent_llm_errors_total counter\n")
	fmt.Fprintf(&b, "dev_agent_llm_errors_total %d\n", s.llmErrors)
	fmt.Fprintf(&b, "# HELP dev_agent_llm_budget_truncations_total Times the LLM budget stopped the cycle\n")
	fmt.Fprintf(&b, "# TYPE dev_agent_llm_budget_truncations_total counter\n")
	fmt.Fprintf(&b, "dev_agent_llm_budget_truncations_total %d\n", s.llmBudgetTruncations)
	fmt.Fprintf(&b, "# HELP dev_agent_llm_latency_seconds_last Last LLM call latency in seconds\n")
	fmt.Fprintf(&b, "# TYPE dev_agent_llm_latency_seconds_last gauge\n")
	fmt.Fprintf(&b, "dev_agent_llm_latency_seconds_last %.4f\n", s.llmLatency.Seconds())
	fmt.Fprintf(&b, "# HELP dev_agent_history_entries Current history entry count\n")
	fmt.Fprintf(&b, "# TYPE dev_agent_history_entries gauge\n")
	fmt.Fprintf(&b, "dev_agent_history_entries %d\n", len(s.history))
	// Draft loop (agent-feedback): the operator dashboard watches HITL —
	// pending decisions (especially major ones) and the feedback signal.
	pending, majorPending, approved, rejected, expiredCount := 0, 0, 0, 0, 0
	for _, d := range s.drafts {
		switch {
		case d.Approved:
			approved++
		case d.Rejected:
			rejected++
		case d.Expired:
			expiredCount++
		default:
			pending++
			if d.Major {
				majorPending++
			}
		}
	}
	fmt.Fprintf(&b, "# HELP dev_agent_drafts_pending Drafts awaiting a human decision\n")
	fmt.Fprintf(&b, "# TYPE dev_agent_drafts_pending gauge\n")
	fmt.Fprintf(&b, "dev_agent_drafts_pending %d\n", pending)
	fmt.Fprintf(&b, "# HELP dev_agent_drafts_major_pending Major drafts awaiting a human decision\n")
	fmt.Fprintf(&b, "# TYPE dev_agent_drafts_major_pending gauge\n")
	fmt.Fprintf(&b, "dev_agent_drafts_major_pending %d\n", majorPending)
	fmt.Fprintf(&b, "# HELP dev_agent_drafts_approved_total Drafts committed by human approval\n")
	fmt.Fprintf(&b, "# TYPE dev_agent_drafts_approved_total gauge\n")
	fmt.Fprintf(&b, "dev_agent_drafts_approved_total %d\n", approved)
	fmt.Fprintf(&b, "# HELP dev_agent_drafts_rejected_total Drafts rejected with feedback\n")
	fmt.Fprintf(&b, "# TYPE dev_agent_drafts_rejected_total gauge\n")
	fmt.Fprintf(&b, "dev_agent_drafts_rejected_total %d\n", rejected)
	fmt.Fprintf(&b, "# HELP dev_agent_drafts_expired_total Pending drafts retired by the TTL policy\n")
	fmt.Fprintf(&b, "# TYPE dev_agent_drafts_expired_total gauge\n")
	fmt.Fprintf(&b, "dev_agent_drafts_expired_total %d\n", expiredCount)
	w.Header().Set("Content-Type", "text/plain; version=0.0.4")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write([]byte(b.String()))
}

// handleKnowledge queries the embedded runbook KB by service or symptom.
// GET /knowledge                → list all services with runbooks
// GET /knowledge?service=xxx    → runbooks for a service
// GET /knowledge?symptom=xxx    → runbooks matching a symptom substring
func (s *Server) handleKnowledge(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	if svc := q.Get("service"); svc != "" {
		writeJSON(w, http.StatusOK, s.kb.Query(svc))
		return
	}
	if sym := q.Get("symptom"); sym != "" {
		writeJSON(w, http.StatusOK, s.kb.MatchSymptom(sym))
		return
	}
	writeJSON(w, http.StatusOK, s.kb.Services())
}

// healRequest is the POST /heal body.
type healRequest struct {
	Action string `json:"action"`
	Target string `json:"target"`
}

func (s *Server) handleHeal(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "POST required"})
		return
	}
	var req healRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid JSON"})
		return
	}
	rc := s.heal.Execute(r.Context(), req.Action, req.Target)
	s.mu.Lock()
	s.history = append(s.history, rc)
	s.healCount++
	s.evidence = append(s.evidence, EvidenceEntry{
		Time: rc.Time, Kind: "heal",
		What:  fmt.Sprintf("action=%s executed=%t", rc.Action, rc.Executed),
		Where: rc.Target, Why: rc.Output,
	})
	s.trimLocked()
	s.mu.Unlock()
	_ = s.Save()
	slog.Info("heal executed", "action", rc.Action, "target", rc.Target, "executed", rc.Executed)
	writeJSON(w, http.StatusOK, rc)
}

// draftRequest is the POST /draft body (agent-generated draft content).
type draftRequest struct {
	Kind  string `json:"kind"`
	Text  string `json:"text"`
	Where string `json:"where"`
	Major bool   `json:"major"`
}

func (s *Server) handleDraft(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "POST required"})
		return
	}
	var req draftRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid JSON"})
		return
	}
	if req.Text == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "text required"})
		return
	}
	s.mu.Lock()
	s.draftSeq++
	d := &Draft{ID: fmt.Sprintf("d%d", s.draftSeq), Kind: req.Kind, Text: req.Text,
		Where: req.Where, Major: req.Major, Approved: false, CreatedAt: time.Now().UTC()}
	s.drafts[d.ID] = d
	what := "created kind=" + req.Kind
	if req.Major {
		what += " (major)"
	}
	s.evidence = append(s.evidence, EvidenceEntry{
		Time: time.Now().UTC(), Kind: "draft",
		What:  what,
		Where: req.Where, Why: "agent draft (inert until human approval)",
	})
	s.trimLocked()
	s.mu.Unlock()
	_ = s.Save()
	slog.Info("draft created (inert)", "id", d.ID, "kind", req.Kind, "where", req.Where)
	writeJSON(w, http.StatusOK, d)
}

func (s *Server) handleDraftApprove(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "POST required"})
		return
	}
	id := r.PathValue("id")
	s.mu.Lock()
	d, ok := s.drafts[id]
	if !ok {
		s.mu.Unlock()
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "unknown draft " + id})
		return
	}
	if d.Rejected {
		s.mu.Unlock()
		writeJSON(w, http.StatusConflict, map[string]string{
			"error": "draft " + id + " was rejected; rejection is final (create a new draft)"})
		return
	}
	if d.Expired {
		s.mu.Unlock()
		writeJSON(w, http.StatusConflict, map[string]string{
			"error": "draft " + id + " expired (TTL policy); create a new draft"})
		return
	}
	if !d.Approved {
		d.Approved = true
		s.evidence = append(s.evidence, EvidenceEntry{
			Time: time.Now().UTC(), Kind: "commit",
			What:  "approved kind=" + d.Kind,
			Where: d.ID, Why: "human-approval commit (agent-saga)",
		})
		s.trimLocked()
	}
	s.mu.Unlock()
	_ = s.Save()
	slog.Info("draft approved (commit)", "id", d.ID)
	writeJSON(w, http.StatusOK, d)
}

// handleDrafts lists every draft with its lifecycle state — HITL visibility:
// a human can only decide on drafts they can actually see (pending, approved,
// rejected, major-flagged), oldest first.
func (s *Server) handleDrafts(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "GET required"})
		return
	}
	// The listing is the operator's decision surface — it must never show a
	// decision the TTL policy has already taken (opt-in policy).
	s.expireStale(time.Now())
	s.mu.Lock()
	list := make([]Draft, 0, len(s.drafts))
	for _, d := range s.drafts {
		list = append(list, *d)
	}
	s.mu.Unlock()
	sort.Slice(list, func(i, j int) bool { return draftNum(list[i].ID) < draftNum(list[j].ID) })
	writeJSON(w, http.StatusOK, list)
}

// draftNum parses the numeric part of a "d<n>" draft id; ids are always of
// that shape (draftSeq), so a parse failure cannot happen — fall back to 0.
func draftNum(id string) int {
	n, _ := strconv.Atoi(strings.TrimPrefix(id, "d"))
	return n
}

// handleDraftReject records the human's verdict WITH a reason. The reason is
// the feedback loop's signal: it stays on the draft (queryable via /feedback
// and injected into the LLM context) and never enters the evidence log —
// operator free text must not leak into the audit trail. Rejection is final:
// a rejected draft cannot be approved afterwards.
func (s *Server) handleDraftReject(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "POST required"})
		return
	}
	var req struct {
		Reason string `json:"reason"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil || strings.TrimSpace(req.Reason) == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "reason required (feedback needs the why)"})
		return
	}
	id := r.PathValue("id")
	s.mu.Lock()
	d, ok := s.drafts[id]
	if !ok {
		s.mu.Unlock()
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "unknown draft " + id})
		return
	}
	if d.Approved {
		s.mu.Unlock()
		writeJSON(w, http.StatusConflict, map[string]string{
			"error": "draft " + id + " is already committed; it cannot be rejected"})
		return
	}
	if d.Expired {
		s.mu.Unlock()
		writeJSON(w, http.StatusConflict, map[string]string{
			"error": "draft " + id + " expired (TTL policy); create a new draft"})
		return
	}
	if !d.Rejected {
		d.Rejected = true
		d.Reason = strings.TrimSpace(req.Reason)
		s.evidence = append(s.evidence, EvidenceEntry{
			Time: time.Now().UTC(), Kind: "reject",
			What:  "rejected kind=" + d.Kind,
			Where: d.ID, Why: "human feedback (agent loop; reason stays on the draft)",
		})
		s.trimLocked()
	}
	s.mu.Unlock()
	_ = s.Save()
	slog.Info("draft rejected (feedback)", "id", d.ID)
	writeJSON(w, http.StatusOK, d)
}

// handleFeedback aggregates the human decisions so the agent (or an operator
// dashboard) can query the feedback signal: counts plus every rejection with
// its reason, newest first.
func (s *Server) handleFeedback(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "GET required"})
		return
	}
	type rejectInfo struct {
		ID     string `json:"id"`
		Kind   string `json:"kind"`
		Where  string `json:"where"`
		Reason string `json:"reason"`
	}
	s.expireStale(time.Now())
	s.mu.Lock()
	ids := make([]string, 0, len(s.drafts))
	for id := range s.drafts {
		ids = append(ids, id)
	}
	sort.Slice(ids, func(i, j int) bool { return draftNum(ids[i]) > draftNum(ids[j]) })
	var fb struct {
		Total        int          `json:"total"`
		Approved     int          `json:"approved"`
		Rejected     int          `json:"rejected"`
		Expired      int          `json:"expired"`
		Pending      int          `json:"pending"`
		MajorPending int          `json:"major_pending"`
		Reasons      []rejectInfo `json:"reasons"`
	}
	fb.Reasons = []rejectInfo{}
	for _, id := range ids {
		d := s.drafts[id]
		fb.Total++
		switch {
		case d.Approved:
			fb.Approved++
		case d.Rejected:
			fb.Rejected++
			fb.Reasons = append(fb.Reasons, rejectInfo{ID: d.ID, Kind: d.Kind, Where: d.Where, Reason: d.Reason})
		case d.Expired:
			fb.Expired++
		default:
			fb.Pending++
			if d.Major {
				fb.MajorPending++
			}
		}
	}
	s.mu.Unlock()
	writeJSON(w, http.StatusOK, fb)
}

// ListenAndServe starts the private API; blocks until ctx is done.
func (s *Server) ListenAndServe(ctx context.Context) error {
	srv := &http.Server{Addr: s.cfg.APIAddr, Handler: s.Handler(), ReadHeaderTimeout: 5 * time.Second}
	errCh := make(chan error, 1)
	go func() { errCh <- srv.ListenAndServe() }()
	select {
	case <-ctx.Done():
		shutCtx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		return srv.Shutdown(shutCtx)
	case err := <-errCh:
		return err
	}
}

// --- persistence (dev-agent spec: SIGTERM persists history) ---

type persisted struct {
	History  []healer.Receipt `json:"history"`
	Evidence []EvidenceEntry  `json:"evidence"`
	// Drafts persist across restarts (agent-saga + agent-feedback): the bot
	// runs as a sidecar — compose recreate or a host reboot happens between
	// "agent drafts" and "human decides". Losing pending drafts (or the
	// rejection reasons of the feedback loop) would silently break HITL.
	Drafts []*Draft `json:"drafts,omitempty"`
}

func (s *Server) stateFile() string { return filepath.Join(s.cfg.StateDir, "state.json") }

// Save writes history+evidence to the state dir (best effort). Concurrent
// callers (reconcile ticker, /heal, SIGTERM) are serialized, and the file is
// replaced atomically so it is never observed half-written.
func (s *Server) Save() error {
	if err := os.MkdirAll(s.cfg.StateDir, 0o700); err != nil {
		return fmt.Errorf("state dir: %w", err)
	}
	s.mu.Lock()
	list := make([]*Draft, 0, len(s.drafts))
	for _, d := range s.drafts {
		list = append(list, d)
	}
	sort.Slice(list, func(i, j int) bool { return draftNum(list[i].ID) < draftNum(list[j].ID) })
	p := persisted{History: s.history, Evidence: s.evidence, Drafts: list}
	s.mu.Unlock()
	raw, err := json.MarshalIndent(p, "", "  ")
	if err != nil {
		return err
	}
	s.saveMu.Lock()
	defer s.saveMu.Unlock()
	tmp := s.stateFile() + ".tmp"
	if err := os.WriteFile(tmp, raw, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, s.stateFile())
}

func (s *Server) load() {
	raw, err := os.ReadFile(s.stateFile())
	if err != nil {
		return
	}
	var p persisted
	if json.Unmarshal(raw, &p) != nil {
		return
	}
	s.history, s.evidence = p.History, p.Evidence
	// Restore drafts and continue the id sequence past the highest restored
	// id — a fresh draft must never overwrite a restored one.
	for _, d := range p.Drafts {
		if d == nil || d.ID == "" {
			continue
		}
		s.drafts[d.ID] = d
		if n := draftNum(d.ID); n > s.draftSeq {
			s.draftSeq = n
		}
	}
}

// expireStale applies the draft TTL policy (opt-in: cfg.DraftTTL > 0):
// pending drafts older than the TTL are marked expired — the safe direction,
// nothing was ever committed — and audited (kind "expire"). Legacy drafts
// without a creation timestamp (state files from before the field existed)
// are never expired: the upgrade path must not mass-retire decisions.
// Returns the number of newly expired drafts. Must be called WITHOUT s.mu.
func (s *Server) expireStale(now time.Time) int {
	if s.cfg.DraftTTL <= 0 {
		return 0
	}
	expired := 0
	s.mu.Lock()
	for _, d := range s.drafts {
		if d.Approved || d.Rejected || d.Expired || d.CreatedAt.IsZero() {
			continue
		}
		if now.Sub(d.CreatedAt) <= s.cfg.DraftTTL {
			continue
		}
		d.Expired = true
		expired++
		s.evidence = append(s.evidence, EvidenceEntry{
			Time: now.UTC(), Kind: "expire",
			What:  "expired kind=" + d.Kind + " (ttl " + s.cfg.DraftTTL.String() + ")",
			Where: d.ID, Why: "TTL policy: pending draft, nothing committed",
		})
	}
	s.mu.Unlock()
	if expired > 0 {
		_ = s.Save()
	}
	return expired
}

// PrintStatus renders the current persisted status (used by `agent -status`).
func (s *Server) PrintStatus() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	var b strings.Builder
	fmt.Fprintf(&b, "started_at: %s\nconfig: %s\n", s.start.Format(time.RFC3339), s.cfg.String())
	fmt.Fprintf(&b, "containers: %d findings, history: %d\n", len(s.containers), len(s.history))
	for _, f := range s.containers {
		fmt.Fprintf(&b, "  UNHEALTHY %s (%s): %s\n", f.Container, f.State, strings.Join(f.Symptoms, ", "))
	}
	for svc, ms := range s.matches {
		for _, m := range ms {
			// source duty: show where the answer came from (agent-knowledge)
			fmt.Fprintf(&b, "  KB[%s@%s] %s → %s (source %s)\n", svc, m.KBVersion, strings.Join(m.Runbook.Symptoms, "|"), firstLine(m.Runbook.Diagnosis), m.Source)
		}
	}
	return b.String()
}

func firstLine(s string) string {
	if i := strings.IndexByte(s, '\n'); i >= 0 {
		return s[:i]
	}
	return s
}

// --- anonymizer (dev-agent-privacy: strip-then-review) ---

var (
	reSecret    = regexp.MustCompile(`(?i)\b((?:api[_-]?key|apikey|key|token|secret|password|passwd|authorization|bearer)["']?\s*[:=]\s*"?)[^\s"',}]+`)
	reIPv4      = regexp.MustCompile(`\b(?:\d{1,3}\.){3}\d{1,3}\b`)
	reHome      = regexp.MustCompile(`/home/[a-zA-Z0-9_.-]+|/Users/[a-zA-Z0-9_.-]+|/root\b`)
	reHost      = regexp.MustCompile(`\b[a-z0-9][a-z0-9-]{2,}\.(?:local|internal|home\.arpa|lan)\b`)
	reJWT       = regexp.MustCompile(`eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+`)
	reConnStr   = regexp.MustCompile(`(?i)\b(?:postgres|postgresql|redis|mongodb|mysql|amqp|amqps)://[^\s"']*`)
	reEmail     = regexp.MustCompile(`[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}`)
	reBasicAuth = regexp.MustCompile(`(?i)basic [A-Za-z0-9+/=]+`)
)

// Anonymize strips secrets, IPs, hostnames and user paths from raw, returning
// the cleaned string plus one StripRecord per category that fired. Raw
// values are never retained anywhere.
func (s *Server) Anonymize(raw string) (string, []StripRecord) {
	var rec []StripRecord
	add := func(what, why string) {
		rec = append(rec, StripRecord{What: what, Where: "llm-context", Why: why})
	}
	// JWT tokens (eyJ...)
	out := reJWT.ReplaceAllString(raw, "<jwt>")
	if out != raw {
		add("jwt token", "JWT tokens must not leave the host")
	}
	// Connection strings (postgres://user:pass@host, redis://...)
	prev := out
	out = reConnStr.ReplaceAllString(out, "<conn-str>")
	if out != prev {
		add("connection string", "connection strings contain credentials")
	}
	// Basic auth headers (Authorization: Basic dXNlcjpwYXNz)
	prev = out
	out = reBasicAuth.ReplaceAllString(out, "basic <redacted>")
	if out != prev {
		add("basic auth", "basic auth credentials must not leave the host")
	}
	// Secret values (api_key=..., token=..., password=...)
	prev = out
	out = reSecret.ReplaceAllString(out, `${1}***`)
	if out != prev {
		add("secret value", "credentials must not leave the host")
	}
	// Email addresses
	prev = out
	out = reEmail.ReplaceAllString(out, "<email>")
	if out != prev {
		add("email", "email addresses are private")
	}
	// Private IPs
	prev = out
	out = reIPv4.ReplaceAllString(out, "<ip>")
	if out != prev {
		add("ip address", "network topology is private")
	}
	// User paths
	prev = out
	out = reHome.ReplaceAllString(out, "/home/<user>")
	if out != prev {
		add("user path", "user identity is private")
	}
	// Internal hostnames
	prev = out
	hosts := s.cfg.Hostnames
	for _, h := range hosts {
		out = strings.ReplaceAll(out, h, "<host>")
	}
	out = reHost.ReplaceAllString(out, "<host>")
	if out != prev {
		add("hostname", "host identity is private")
	}
	for _, r := range rec {
		s.addEvidence(EvidenceEntry{Time: time.Now().UTC(), Kind: "strip", What: r.What, Where: r.Where, Why: r.Why})
	}
	return out, rec
}
