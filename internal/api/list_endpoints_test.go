package api

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
)

// TestEmptyListEndpointsSerializeAsArrays guards the JSON shape of /history and
// /evidence: a nil slice marshals to `null`, which breaks every client that
// iterates the response (the AE2 injection e2e crashed on exactly this).
func TestEmptyListEndpointsSerializeAsArrays(t *testing.T) {
	s, _ := testServer(t, false)
	h := s.Handler()
	for _, path := range []string{"/history", "/evidence"} {
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, path, nil))
		if got := rec.Body.String(); got != "[]\n" && got != "[]" {
			t.Errorf("GET %s on a fresh agent = %q, want an empty JSON array", path, got)
		}
	}
}

// TestLoadedNullStateStillSerializesAsArrays covers a state.json written with
// `"history": null` / `"evidence": null`: restoring it must not reintroduce the
// null shape.
func TestLoadedNullStateStillSerializesAsArrays(t *testing.T) {
	s, dir := testServer(t, false)
	state := filepath.Join(dir, "state.json")
	if err := os.WriteFile(state, []byte(`{"history":null,"evidence":null,"drafts":{}}`), 0o600); err != nil {
		t.Fatal(err)
	}
	s.load()

	h := s.Handler()
	for _, path := range []string{"/history", "/evidence"} {
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, path, nil))
		if got := rec.Body.String(); got == "null\n" || got == "null" {
			t.Errorf("GET %s after loading a null state = %q, want an empty JSON array", path, got)
		}
		var out []json.RawMessage
		if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
			t.Errorf("GET %s is not a JSON array: %v", path, err)
		}
	}
}
