package api

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

// agent-saga (AS2/AS3/AS5): an agent drafts content (classification, reply,
// tags), it is inert until a human approves it, and the approve is a commit
// that is audited. Approval-gate invariants:
//   - a draft starts approved:false and is never acted on without approve
//   - approving turns it into a commit and adds a "commit" evidence entry
//   - the draft is rejected with an unknown id 404

func postDraft(t *testing.T, s *Server, body map[string]string) *httptest.ResponseRecorder {
	t.Helper()
	b, _ := json.Marshal(body)
	rec := httptest.NewRecorder()
	s.Handler().ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/draft", bytes.NewReader(b)))
	return rec
}

func approveDraft(t *testing.T, s *Server, id string) *httptest.ResponseRecorder {
	t.Helper()
	rec := httptest.NewRecorder()
	s.Handler().ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/draft/"+id+"/approve", nil))
	return rec
}

func TestDraftStartsInert(t *testing.T) {
	s, _ := testServer(t, true)
	rec := postDraft(t, s, map[string]string{
		"kind": "classification", "text": "refund-request", "where": "ticket/42",
	})
	if rec.Code != http.StatusOK {
		t.Fatalf("POST /draft = %d, body=%s", rec.Code, rec.Body.String())
	}
	var d struct {
		ID       string `json:"id"`
		Approved bool   `json:"approved"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &d); err != nil {
		t.Fatalf("draft response not JSON: %v", err)
	}
	if d.ID == "" {
		t.Error("draft must get an id")
	}
	if d.Approved {
		t.Error("agent-saga: a fresh draft must be approved:false (inert until human approve)")
	}
	// an inert draft must not be committed: no commit evidence yet
	var entries []EvidenceEntry
	r := httptest.NewRecorder()
	s.Handler().ServeHTTP(r, httptest.NewRequest(http.MethodGet, "/evidence", nil))
	_ = json.Unmarshal(r.Body.Bytes(), &entries)
	for _, e := range entries {
		if e.Kind == "commit" {
			t.Errorf("agent-saga: commit evidence before any approval: %+v", e)
		}
	}
}

func TestDraftCreatedIsAudited(t *testing.T) {
	s, _ := testServer(t, true)
	postDraft(t, s, map[string]string{"kind": "reply", "text": "draft answer", "where": "ticket/9"})
	var entries []EvidenceEntry
	r := httptest.NewRecorder()
	s.Handler().ServeHTTP(r, httptest.NewRequest(http.MethodGet, "/evidence", nil))
	_ = json.Unmarshal(r.Body.Bytes(), &entries)
	found := false
	for _, e := range entries {
		if e.Kind == "draft" && e.Where == "ticket/9" {
			found = true
		}
	}
	if !found {
		t.Error("agent-saga: creating a draft must be written to the evidence log (kind draft)")
	}
}

func TestApproveCommitsAndAudits(t *testing.T) {
	s, _ := testServer(t, true)
	rec := postDraft(t, s, map[string]string{"kind": "tags", "text": "invoice", "where": "paperless/14"})
	var d struct {
		ID string `json:"id"`
	}
	_ = json.Unmarshal(rec.Body.Bytes(), &d)

	ap := approveDraft(t, s, d.ID)
	if ap.Code != http.StatusOK {
		// RED (no handler yet) or GREEN later
		t.Fatalf("POST /draft/%s/approve = %d, body=%s", d.ID, ap.Code, ap.Body.String())
	}
	var got struct {
		ID       string `json:"id"`
		Approved bool   `json:"approved"`
	}
	_ = json.Unmarshal(ap.Body.Bytes(), &got)
	if !got.Approved {
		t.Error("agent-saga: approving a draft must set approved:true")
	}

	var entries []EvidenceEntry
	r := httptest.NewRecorder()
	s.Handler().ServeHTTP(r, httptest.NewRequest(http.MethodGet, "/evidence", nil))
	_ = json.Unmarshal(r.Body.Bytes(), &entries)
	committed := false
	for _, e := range entries {
		if e.Kind == "commit" && e.Where == d.ID {
			committed = true
		}
	}
	if !committed {
		t.Error("agent-saga: approval must be audited as a commit (kind commit)")
	}
}

func TestApproveUnknownDraft404(t *testing.T) {
	s, _ := testServer(t, true)
	ap := approveDraft(t, s, "does-not-exist")
	if ap.Code != http.StatusNotFound {
		t.Errorf("approving an unknown draft = %d, want 404", ap.Code)
	}
}
