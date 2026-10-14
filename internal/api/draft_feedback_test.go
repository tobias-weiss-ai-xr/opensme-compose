package api

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"testing/fstest"

	"opensme-dev-agent/internal/checker"
	"opensme-dev-agent/internal/config"
	"opensme-dev-agent/internal/healer"
	"opensme-dev-agent/internal/knowledge"
)

// HITL (human-in-the-loop) + feedback loop on top of the agent-saga write
// gate:
//   - a human can only decide on drafts they can SEE (GET /drafts lists them)
//   - rejection is a first-class outcome and records the human's reason
//   - the reason is the feedback signal: it is queryable (GET /feedback) and
//     flows back into the agent's LLM context (the loop closes)
//   - "major" marks a draft as a major decision; it is flagged and audited
//   - a rejected draft can never be committed afterwards

func postDraftAny(t *testing.T, s *Server, body map[string]any) *httptest.ResponseRecorder {
	t.Helper()
	b, _ := json.Marshal(body)
	rec := httptest.NewRecorder()
	s.Handler().ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/draft", bytes.NewReader(b)))
	return rec
}

func rejectDraft(t *testing.T, s *Server, id, reason string) *httptest.ResponseRecorder {
	t.Helper()
	body, _ := json.Marshal(map[string]string{"reason": reason})
	rec := httptest.NewRecorder()
	s.Handler().ServeHTTP(rec,
		httptest.NewRequest(http.MethodPost, "/draft/"+id+"/reject", bytes.NewReader(body)))
	return rec
}

func getJSON(t *testing.T, s *Server, path string, out any) *httptest.ResponseRecorder {
	t.Helper()
	rec := httptest.NewRecorder()
	s.Handler().ServeHTTP(rec, httptest.NewRequest(http.MethodGet, path, nil))
	if rec.Code == http.StatusOK {
		_ = json.Unmarshal(rec.Body.Bytes(), out)
	}
	return rec
}

func evidenceKinds(t *testing.T, s *Server) []EvidenceEntry {
	t.Helper()
	var entries []EvidenceEntry
	r := httptest.NewRecorder()
	s.Handler().ServeHTTP(r, httptest.NewRequest(http.MethodGet, "/evidence", nil))
	_ = json.Unmarshal(r.Body.Bytes(), &entries)
	return entries
}

func TestHITLListsPendingDrafts(t *testing.T) {
	s, _ := testServer(t, true)
	rec := postDraft(t, s, map[string]string{
		"kind": "reply", "text": "draft answer for ticket/9", "where": "ticket/9",
	})
	var d struct {
		ID string `json:"id"`
	}
	_ = json.Unmarshal(rec.Body.Bytes(), &d)

	var drafts []Draft
	if code := getJSON(t, s, "/drafts", &drafts).Code; code != http.StatusOK {
		t.Fatalf("GET /drafts = %d (HITL needs visibility of what to approve)", code)
	}
	if len(drafts) != 1 {
		t.Fatalf("GET /drafts must list the pending draft, got %d entries", len(drafts))
	}
	got := drafts[0]
	if got.ID != d.ID || got.Approved || got.Rejected {
		t.Errorf("pending draft must be listed inert: %+v", got)
	}
	if got.Text == "" {
		t.Error("HITL: the human must see the draft content to decide on it")
	}
}

func TestMajorDraftFlaggedAndAudited(t *testing.T) {
	s, _ := testServer(t, true)
	rec := postDraftAny(t, s, map[string]any{
		"kind": "config-change", "text": "raise memory limit", "where": "app/deploy",
		"major": true,
	})
	if rec.Code != http.StatusOK {
		t.Fatalf("POST /draft (major) = %d, body=%s", rec.Code, rec.Body.String())
	}
	var d Draft
	_ = json.Unmarshal(rec.Body.Bytes(), &d)
	if !d.Major {
		t.Error("a major draft must carry major:true (major decision marker)")
	}

	var drafts []Draft
	_ = getJSON(t, s, "/drafts", &drafts)
	if len(drafts) != 1 || !drafts[0].Major {
		t.Error("HITL: a major draft must be listed with major:true")
	}

	audited := false
	for _, e := range evidenceKinds(t, s) {
		if e.Kind == "draft" && strings.Contains(e.What, "major") {
			audited = true
		}
	}
	if !audited {
		t.Error("a major decision must be flagged in the evidence log")
	}
}

func TestRejectRecordsFeedback(t *testing.T) {
	s, _ := testServer(t, true)
	rec := postDraft(t, s, map[string]string{
		"kind": "classification", "text": "wrong label", "where": "ticket/7",
	})
	var d Draft
	_ = json.Unmarshal(rec.Body.Bytes(), &d)

	rj := rejectDraft(t, s, d.ID, "misclassified: this is a billing question")
	if rj.Code != http.StatusOK {
		t.Fatalf("POST /draft/%s/reject = %d, body=%s", d.ID, rj.Code, rj.Body.String())
	}
	var got Draft
	_ = json.Unmarshal(rj.Body.Bytes(), &got)
	if !got.Rejected {
		t.Error("rejection must set rejected:true")
	}
	if got.Approved {
		t.Error("a rejected draft must never be approved")
	}
	if got.Reason != "misclassified: this is a billing question" {
		t.Errorf("rejection must record the human's reason, got %q", got.Reason)
	}

	found, leaked := false, false
	for _, e := range evidenceKinds(t, s) {
		if e.Kind == "reject" && e.Where == d.ID {
			found = true
		}
		if strings.Contains(e.What, "billing question") {
			leaked = true
		}
	}
	if !found {
		t.Error("rejection must be audited (kind reject)")
	}
	if leaked {
		t.Error("privacy: the raw rejection reason must not enter the evidence log")
	}
}

func TestRejectRequiresReason(t *testing.T) {
	s, _ := testServer(t, true)
	rec := postDraft(t, s, map[string]string{"kind": "tags", "text": "x", "where": "p/1"})
	var d Draft
	_ = json.Unmarshal(rec.Body.Bytes(), &d)

	b, _ := json.Marshal(map[string]string{"reason": ""})
	r := httptest.NewRecorder()
	s.Handler().ServeHTTP(r,
		httptest.NewRequest(http.MethodPost, "/draft/"+d.ID+"/reject", bytes.NewReader(b)))
	if r.Code != http.StatusBadRequest {
		t.Errorf("reject without a reason = %d, want 400 (feedback needs the why)", r.Code)
	}
}

func TestRejectUnknownDraft404(t *testing.T) {
	s, _ := testServer(t, true)
	if code := rejectDraft(t, s, "no-such", "why").Code; code != http.StatusNotFound {
		t.Errorf("rejecting an unknown draft = %d, want 404", code)
	}
}

func TestRejectedDraftCannotBeApproved(t *testing.T) {
	s, _ := testServer(t, true)
	rec := postDraft(t, s, map[string]string{"kind": "reply", "text": "x", "where": "t/1"})
	var d Draft
	_ = json.Unmarshal(rec.Body.Bytes(), &d)
	_ = rejectDraft(t, s, d.ID, "wrong tone")

	ap := approveDraft(t, s, d.ID)
	if ap.Code != http.StatusConflict {
		t.Errorf("approving a rejected draft = %d, want 409 (rejection is final)", ap.Code)
	}
	var drafts []Draft
	_ = getJSON(t, s, "/drafts", &drafts)
	if len(drafts) != 1 || !drafts[0].Rejected || drafts[0].Approved {
		t.Error("a rejected draft must stay rejected after an approve attempt")
	}
}

func TestFeedbackAggregatesDecisions(t *testing.T) {
	s, _ := testServer(t, true)
	r1 := postDraft(t, s, map[string]string{"kind": "reply", "text": "a", "where": "t/1"})
	r2 := postDraft(t, s, map[string]string{"kind": "classification", "text": "b", "where": "t/2"})
	r3 := postDraft(t, s, map[string]string{"kind": "tags", "text": "c", "where": "t/3"})
	var d1, d2, d3 Draft
	_ = json.Unmarshal(r1.Body.Bytes(), &d1)
	_ = json.Unmarshal(r2.Body.Bytes(), &d2)
	_ = json.Unmarshal(r3.Body.Bytes(), &d3)
	_ = approveDraft(t, s, d1.ID)
	_ = rejectDraft(t, s, d2.ID, "wrong classification")

	var fb struct {
		Approved int `json:"approved"`
		Rejected int `json:"rejected"`
		Pending  int `json:"pending"`
		Reasons  []struct {
			Kind   string `json:"kind"`
			Where  string `json:"where"`
			Reason string `json:"reason"`
		} `json:"reasons"`
	}
	if code := getJSON(t, s, "/feedback", &fb).Code; code != http.StatusOK {
		t.Fatalf("GET /feedback = %d (the agent needs a queryable feedback signal)", code)
	}
	if fb.Approved != 1 || fb.Rejected != 1 || fb.Pending != 1 {
		t.Errorf("feedback aggregate wrong: %+v", fb)
	}
	if len(fb.Reasons) != 1 || fb.Reasons[0].Reason != "wrong classification" ||
		fb.Reasons[0].Kind != "classification" {
		t.Errorf("feedback must carry the rejection reasons: %+v", fb.Reasons)
	}
	_ = d3
}

// The loop must CLOSE: recent human rejections are injected into the LLM
// prompt so the agent stops repeating the mistake.
func TestFeedbackInformsLLM(t *testing.T) {
	var prompt atomic.Value
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b := new(bytes.Buffer)
		_, _ = b.ReadFrom(r.Body)
		prompt.Store(b.String())
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"response":"ok"}`))
	}))
	defer srv.Close()

	kb, err := knowledge.Load(fstest.MapFS{"kb/app.json": &fstest.MapFile{Data: []byte(`{
		"schema": 1, "service": "app", "kb_version": "2026.10",
		"runbooks": [{"symptoms": ["restarting"], "diagnosis": "port collision", "remediation": ["check bind"]}]
	}`)}}, "kb")
	if err != nil {
		t.Fatal(err)
	}
	cfg := &config.Config{
		Watch: []string{"opensme"}, StateDir: t.TempDir(), HistoryMax: 100,
		LLM: config.LLMConfig{Backend: "ollama", URL: srv.URL, Model: "m", MaxRequests: 5},
	}
	s, err := New(cfg, kb, checker.New([]string{"opensme"}, dualRunner{}),
		healer.New(true, nil), NewLLM("ollama", srv.URL, "", "m", 5))
	if err != nil {
		t.Fatal(err)
	}

	const why = "never escalate disk alerts twice"
	rec := postDraft(t, s, map[string]string{
		"kind": "escalation", "text": "disk full", "where": "app/monitoring",
	})
	var d Draft
	_ = json.Unmarshal(rec.Body.Bytes(), &d)
	if code := rejectDraft(t, s, d.ID, why).Code; code != http.StatusOK {
		t.Fatalf("reject = %d", code)
	}

	f := checker.Finding{Container: "opensme-app-1", Service: "app", State: "running",
		Health: "unhealthy", Symptoms: []string{"restarting"}, Detail: "boom"}
	if _, err := s.llm.Analyze(context.Background(), s, f); err != nil {
		t.Fatalf("Analyze: %v", err)
	}
	p, _ := prompt.Load().(string)
	if !strings.Contains(p, why) {
		t.Errorf("feedback loop broken: the LLM prompt must carry the rejection reason\nprompt: %s", p)
	}
}
