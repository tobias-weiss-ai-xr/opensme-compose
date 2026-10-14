package api

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// Draft TTL: a pending draft that no human ever decides on must not pollute
// the HITL queue forever. Expiry is opt-in (DraftTTL 0 = off, the default —
// consistent with the repo's "opt-in, not default" philosophy) and always
// takes the safe direction: expiring can never commit anything. Expired is
// final like rejected: neither approve nor reject may flip it afterwards.

func TestStalePendingDraftExpires(t *testing.T) {
	s, _ := testServer(t, true)
	s.cfg.DraftTTL = time.Hour
	r1 := postDraft(t, s, map[string]string{"kind": "reply", "text": "a", "where": "t/1"})
	r2 := postDraft(t, s, map[string]string{"kind": "classification", "text": "b", "where": "t/2"})
	r3 := postDraftAny(t, s, map[string]any{"kind": "config-change", "text": "c", "where": "t/3", "major": true})
	var d1, d2, d3 Draft
	_ = json.Unmarshal(r1.Body.Bytes(), &d1)
	_ = json.Unmarshal(r2.Body.Bytes(), &d2)
	_ = json.Unmarshal(r3.Body.Bytes(), &d3)
	_ = approveDraft(t, s, d2.ID)
	_ = rejectDraft(t, s, d3.ID, "nope")

	// the policy pass: 2h later, only the still-pending draft expires
	if n := s.expireStale(time.Now().Add(2 * time.Hour)); n != 1 {
		t.Fatalf("expireStale = %d, want 1", n)
	}
	s.mu.Lock()
	got1, got2, got3 := *s.drafts[d1.ID], *s.drafts[d2.ID], *s.drafts[d3.ID]
	s.mu.Unlock()
	if !got1.Expired {
		t.Errorf("stale pending draft must be expired: %+v", got1)
	}
	if got2.Expired || !got2.Approved {
		t.Errorf("approved draft must never be expired: %+v", got2)
	}
	if got3.Expired || !got3.Rejected {
		t.Errorf("rejected draft must never be expired: %+v", got3)
	}

	// expiry is audited
	s.mu.Lock()
	kinds := map[string]int{}
	for _, e := range s.evidence {
		kinds[e.Kind]++
	}
	s.mu.Unlock()
	if kinds["expire"] != 1 {
		t.Errorf("expiry must be audited (kind expire), evidence kinds: %v", kinds)
	}

	// expired is final: neither approve nor reject may flip it
	if code := approveDraft(t, s, d1.ID).Code; code != http.StatusConflict {
		t.Errorf("approve on expired draft = %d, want 409", code)
	}
	if code := rejectDraft(t, s, d1.ID, "late").Code; code != http.StatusConflict {
		t.Errorf("reject on expired draft = %d, want 409", code)
	}

	// the aggregate: expired leaves the pending queue, counted separately
	var fb struct {
		Pending int `json:"pending"`
		Expired int `json:"expired"`
		Total   int `json:"total"`
	}
	_ = getJSON(t, s, "/feedback", &fb)
	if fb.Pending != 0 || fb.Expired != 1 || fb.Total != 3 {
		t.Errorf("feedback aggregate wrong after expiry: %+v", fb)
	}

	// metrics expose the policy outcome
	m := getMetrics(t, s)
	for _, want := range []string{
		"dev_agent_drafts_pending 0",
		"dev_agent_drafts_expired_total 1",
	} {
		if !strings.Contains(m, want) {
			t.Errorf("metrics missing %s\nmetrics:\n%s", want, m)
		}
	}
}

func TestDraftTTLDisabledByDefault(t *testing.T) {
	s, _ := testServer(t, true) // DraftTTL zero = policy off
	postDraft(t, s, map[string]string{"kind": "tags", "text": "x", "where": "p/1"})
	if n := s.expireStale(time.Now().Add(100 * time.Hour)); n != 0 {
		t.Fatalf("TTL off must never expire: expired %d", n)
	}
	var drafts []Draft
	_ = getJSON(t, s, "/drafts", &drafts)
	if len(drafts) != 1 || drafts[0].Expired {
		t.Errorf("TTL off must leave the draft pending: %+v", drafts)
	}
}

func TestLegacyDraftWithoutCreatedAtNeverExpires(t *testing.T) {
	// Drafts persisted before the created_at field existed carry a zero
	// timestamp — the upgrade path must not mass-expire them.
	s, _ := testServer(t, true)
	s.cfg.DraftTTL = time.Hour
	s.mu.Lock()
	s.drafts["d9"] = &Draft{ID: "d9", Kind: "tags", Text: "legacy", Where: "p/9"}
	s.mu.Unlock()
	if n := s.expireStale(time.Now().Add(2 * time.Hour)); n != 0 {
		t.Fatalf("legacy draft (no created_at) must not expire: %d", n)
	}
	var drafts []Draft
	_ = getJSON(t, s, "/drafts", &drafts)
	if len(drafts) != 1 || drafts[0].Expired {
		t.Errorf("legacy draft must stay pending: %+v", drafts)
	}
}

func TestFreshListAppliesTTL(t *testing.T) {
	// GET /drafts is the operator's decision surface — it must never show a
	// decision that the policy has already taken.
	s, _ := testServer(t, true)
	s.cfg.DraftTTL = time.Hour
	postDraft(t, s, map[string]string{"kind": "tags", "text": "x", "where": "p/1"})
	var drafts []Draft
	_ = getJSON(t, s, "/drafts", &drafts)
	if len(drafts) != 1 || drafts[0].Expired {
		t.Fatalf("fresh draft must be pending: %+v", drafts)
	}
	// age it past the TTL by rewinding its creation time, then re-list
	s.mu.Lock()
	*s.drafts["d1"] = Draft{ID: "d1", Kind: "tags", Text: "x", Where: "p/1",
		CreatedAt: time.Now().Add(-2 * time.Hour)}
	s.mu.Unlock()
	_ = getJSON(t, s, "/drafts", &drafts)
	if len(drafts) != 1 || !drafts[0].Expired {
		t.Errorf("listing must apply the TTL: %+v", drafts)
	}
}

// getMetrics is the local metrics helper (the budget tests use their own).
func getMetrics(t *testing.T, s *Server) string {
	t.Helper()
	rec := httptest.NewRecorder()
	s.Handler().ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/metrics", nil))
	return rec.Body.String()
}
