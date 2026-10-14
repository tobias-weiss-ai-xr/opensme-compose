package api

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// HITL durability: a draft decision must survive a bot restart. The bot runs
// as a compose sidecar — compose recreate / host reboot / SIGTERM happen
// between "agent drafts" and "human decides". Losing pending drafts (or the
// rejection reasons of the feedback loop) across restarts silently breaks
// the loop. Also: draftSeq must continue past restored ids, or a fresh draft
// would collide with a restored one and overwrite it.

func restartServer(t *testing.T, s *Server) *Server {
	t.Helper()
	r, err := New(s.cfg, s.kb, s.chk, s.heal, s.llm)
	if err != nil {
		t.Fatalf("restart: %v", err)
	}
	return r
}

func TestDraftsSurviveRestart(t *testing.T) {
	s, _ := testServer(t, true)
	r1 := postDraft(t, s, map[string]string{"kind": "reply", "text": "a", "where": "t/1"})
	r2 := postDraft(t, s, map[string]string{"kind": "classification", "text": "b", "where": "t/2"})
	r3 := postDraftAny(t, s, map[string]any{"kind": "config-change", "text": "c", "where": "t/3", "major": true})
	var d1, d2, d3 Draft
	_ = json.Unmarshal(r1.Body.Bytes(), &d1)
	_ = json.Unmarshal(r2.Body.Bytes(), &d2)
	_ = json.Unmarshal(r3.Body.Bytes(), &d3)
	_ = approveDraft(t, s, d1.ID)
	const why = "wrong classification: billing question"
	if code := rejectDraft(t, s, d2.ID, why).Code; code != http.StatusOK {
		t.Fatalf("reject = %d", code)
	}

	// ── the restart ──
	s2 := restartServer(t, s)

	var drafts []Draft
	if code := getJSON(t, s2, "/drafts", &drafts).Code; code != http.StatusOK {
		t.Fatalf("GET /drafts after restart = %d", code)
	}
	if len(drafts) != 3 {
		t.Fatalf("drafts must survive a restart: got %d, want 3 (%+v)", len(drafts), drafts)
	}
	byID := map[string]Draft{}
	for _, d := range drafts {
		byID[d.ID] = d
	}
	if got := byID[d1.ID]; !got.Approved || got.Rejected {
		t.Errorf("approved draft lost its state across restart: %+v", got)
	}
	if got := byID[d2.ID]; !got.Rejected || got.Reason != why {
		t.Errorf("rejection reason (feedback) lost across restart: %+v", got)
	}
	if got := byID[d3.ID]; !got.Major || got.Approved || got.Rejected {
		t.Errorf("pending major draft lost its state across restart: %+v", got)
	}

	// the loop still carries the reason into the aggregate
	var fb struct {
		Rejected int `json:"rejected"`
		Reasons  []struct {
			Reason string `json:"reason"`
		} `json:"reasons"`
	}
	_ = getJSON(t, s2, "/feedback", &fb)
	if fb.Rejected != 1 || len(fb.Reasons) != 1 || fb.Reasons[0].Reason != why {
		t.Errorf("feedback aggregate lost across restart: %+v", fb)
	}

	// draftSeq continues: a fresh draft must NOT collide with a restored id
	r4 := postDraft(t, s2, map[string]string{"kind": "tags", "text": "d", "where": "t/4"})
	var d4 Draft
	_ = json.Unmarshal(r4.Body.Bytes(), &d4)
	if d4.ID == d1.ID || d4.ID == d2.ID || d4.ID == d3.ID {
		t.Fatalf("fresh draft id %q collides with a restored draft (seq not continued)", d4.ID)
	}
	var after []Draft
	_ = getJSON(t, s2, "/drafts", &after)
	if len(after) != 4 {
		t.Errorf("fresh draft overwrote a restored draft: %d entries, want 4", len(after))
	}
}

func TestDraftMetricsExposeTheLoop(t *testing.T) {
	s, _ := testServer(t, true)
	r1 := postDraft(t, s, map[string]string{"kind": "reply", "text": "a", "where": "t/1"})
	r2 := postDraft(t, s, map[string]string{"kind": "classification", "text": "b", "where": "t/2"})
	var d1, d2 Draft
	_ = json.Unmarshal(r1.Body.Bytes(), &d1)
	_ = json.Unmarshal(r2.Body.Bytes(), &d2)
	_ = approveDraft(t, s, d1.ID)
	_ = rejectDraft(t, s, d2.ID, "nope")
	r3 := postDraftAny(t, s, map[string]any{"kind": "config-change", "text": "c", "where": "t/3", "major": true})
	var d3 Draft
	_ = json.Unmarshal(r3.Body.Bytes(), &d3)

	rec := httptest.NewRecorder()
	s.Handler().ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/metrics", nil))
	m := rec.Body.String()
	for _, want := range []string{
		"dev_agent_drafts_pending 1",
		"dev_agent_drafts_major_pending 1",
		"dev_agent_drafts_approved_total 1",
		"dev_agent_drafts_rejected_total 1",
	} {
		if !strings.Contains(m, want) {
			t.Errorf("metrics must expose %s (operator dashboard cannot see the loop)\nmetrics:\n%s", want, m)
		}
	}
}
