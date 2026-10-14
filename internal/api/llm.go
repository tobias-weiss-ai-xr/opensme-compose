// LLM analysis backends — off by default, opt-in via DEV_AGENT_LLM_BACKEND.
// Only ANONYMIZED context is ever sent (dev-agent spec).
package api

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"sort"
	"strings"
	"sync"
	"time"

	"opensme-dev-agent/internal/checker"
	"opensme-dev-agent/internal/knowledge"
)

// ErrDisabled is returned by every call while the backend is "none".
var ErrDisabled = errors.New("llm analysis disabled (DEV_AGENT_LLM_BACKEND=none)")

// ErrBudgetExceeded is returned when the per-run outbound-call budget
// (DEV_AGENT_LLM_MAX_REQUESTS, 0 = unlimited) is exhausted. The server pauses
// the rest of the reconcile cycle and records an auditable "budget" evidence
// entry instead of dialing out further.
var ErrBudgetExceeded = errors.New("llm analysis budget exceeded (DEV_AGENT_LLM_MAX_REQUESTS)")

// llmCacheMax is the maximum number of cached analyses. Entries are evicted
// by LRU when the cache exceeds this size.
const llmCacheMax = 50

// llmCacheEntry is a cached LLM analysis.
type llmCacheEntry struct {
	analysis string
	at       time.Time
}

// LLM is an opt-in analysis backend. MaxRequests caps outbound calls per
// run (0 = unlimited); cache hits never consume the budget.
type LLM struct {
	Backend     string
	URL         string
	Key         string
	Model       string
	MaxRequests int
	client      *http.Client
	cache       map[string]llmCacheEntry
	cacheMu     sync.Mutex
	budgetMu    sync.Mutex
	requests    int
}

// NewLLM returns nil when the backend is "none" (nothing dials out).
func NewLLM(backend, url, key, model string, maxRequests int) *LLM {
	if backend == "" || backend == "none" {
		return nil
	}
	return &LLM{Backend: backend, URL: url, Key: key, Model: model,
		MaxRequests: maxRequests,
		client:      &http.Client{Timeout: 180 * time.Second}, cache: map[string]llmCacheEntry{}}
}

// Enabled reports whether outbound analysis is configured.
func (l *LLM) Enabled() bool { return l != nil }

// Exceeded reports whether the per-run outbound budget is exhausted. It never
// dials out and is safe to call concurrently.
func (l *LLM) Exceeded() bool {
	if l == nil || l.MaxRequests == 0 {
		return false
	}
	l.budgetMu.Lock()
	defer l.budgetMu.Unlock()
	return l.requests >= l.MaxRequests
}

// findingContext renders one finding into a compact context blob. It runs
// through the server anonymizer BEFORE any byte leaves the host.
//
// No s.mu is taken here on purpose: Anonymize already self-synchronizes via
// addEvidence (which itself locks s.mu). Holding the lock across Anonymize
// would risk a re-entrant lock the moment a strip record fires.
func (s *Server) findingContext(f checker.Finding) string {
	raw := fmt.Sprintf("container=%s service=%s state=%s health=%s restarts=%d oom=%t mem_pct=%.1f symptoms=%s detail=%q",
		f.Container, f.Service, f.State, f.Health, f.RestartCount, f.OOMKilled, f.MemPct,
		joinSymptoms(f.Symptoms), f.Detail)
	clean, _ := s.Anonymize(raw)
	return clean
}

// kbContext renders the knowledge-base matches for a finding as a compact
// string for the LLM prompt. This gives the model the runbook diagnosis so
// it can build on it rather than starting from scratch.
func (s *Server) kbContext(f checker.Finding, matches []knowledge.Match) string {
	if len(matches) == 0 {
		return ""
	}
	var b strings.Builder
	for _, m := range matches {
		fmt.Fprintf(&b, "KB[%s] symptoms=%s diagnosis=%s remediation=%s; ",
			m.Service, strings.Join(m.Runbook.Symptoms, ","),
			firstLine(m.Runbook.Diagnosis), strings.Join(m.Runbook.Remediation, ", "))
	}
	return b.String()
}

// feedbackContext renders the most recent human rejections as anonymized
// feedback for the prompt — this is what closes the agent feedback loop: the
// model sees WHY earlier drafts were turned down and can adjust. Reasons are
// operator-authored free text, so the whole blob passes through the
// anonymizer like any other outbound byte. No s.mu is held across Anonymize
// (same reasoning as findingContext: Anonymize self-synchronizes).
func (s *Server) feedbackContext() string {
	s.mu.Lock()
	ids := make([]string, 0, len(s.drafts))
	for id, d := range s.drafts {
		if d.Rejected && d.Reason != "" {
			ids = append(ids, id)
		}
	}
	sort.Slice(ids, func(i, j int) bool { return draftNum(ids[i]) > draftNum(ids[j]) })
	if len(ids) > 3 {
		ids = ids[:3]
	}
	raws := make([]string, 0, len(ids))
	for _, id := range ids {
		d := s.drafts[id]
		raws = append(raws, fmt.Sprintf("draft %s (kind=%s where=%s) rejected: %q",
			id, d.Kind, d.Where, d.Reason))
	}
	s.mu.Unlock()
	if len(raws) == 0 {
		return ""
	}
	raw := "Recent human feedback on agent drafts (avoid repeating these mistakes): " +
		strings.Join(raws, "; ")
	clean, _ := s.Anonymize(raw)
	return clean
}

// cacheKey hashes the finding context to a stable key for LLM response caching.
// Same finding → same analysis (avoids redundant LLM calls on repeated cycles).
func cacheKey(ctx string) string {
	h := sha256.Sum256([]byte(ctx))
	return hex.EncodeToString(h[:])
}

// Analyze asks the backend for a root-cause hypothesis for the finding.
// Results are cached by finding-context hash. If the same context was seen
// within the last 10 minutes, the cached result is returned without an
// outbound call. A single retry with 2s backoff handles transient failures.
func (l *LLM) Analyze(ctx context.Context, s *Server, f checker.Finding) (string, error) {
	if !l.Enabled() {
		return "", ErrDisabled
	}
	if s == nil {
		return "", errors.New("analyze requires a server context")
	}
	findingCtx := s.findingContext(f)
	fbCtx := s.feedbackContext()
	// The feedback context is part of the cache key: new human feedback must
	// invalidate a cached analysis, or the loop would feed on stale answers.
	key := cacheKey(findingCtx + "\x00" + fbCtx)
	// Cache lookup
	l.cacheMu.Lock()
	if entry, ok := l.cache[key]; ok && time.Since(entry.at) < 10*time.Minute {
		l.cacheMu.Unlock()
		return entry.analysis, nil
	}
	l.cacheMu.Unlock()
	// Budget gate — reserve one outbound call before dialing (agent-guardrails
	// spec). Cache hits above never consume the budget.
	if l.MaxRequests > 0 {
		l.budgetMu.Lock()
		if l.requests >= l.MaxRequests {
			l.budgetMu.Unlock()
			return "", ErrBudgetExceeded
		}
		l.requests++
		l.budgetMu.Unlock()
	}
	// Include KB matches in the prompt so the LLM can build on the runbook.
	var matches []knowledge.Match
	for _, sym := range f.Symptoms {
		matches = append(matches, s.kb.MatchSymptom(sym)...)
	}
	kbCtx := s.kbContext(f, matches)
	prompt := "You are a maintenance assistant for an openSME docker-compose stack. " +
		"Give a one-sentence root-cause hypothesis and one remediation step."
	if kbCtx != "" {
		prompt += " Existing runbook knowledge: " + kbCtx
	}
	if fbCtx != "" {
		prompt += " " + fbCtx
	}
	prompt += " Context: " + findingCtx
	analysis, err := l.callWithRetry(ctx, prompt)
	if err != nil {
		return "", err
	}
	// Cache the result
	l.cacheMu.Lock()
	l.cache[key] = llmCacheEntry{analysis: analysis, at: time.Now()}
	// Evict oldest entries if cache exceeds max size
	if len(l.cache) > llmCacheMax {
		var oldestKey string
		var oldestTime time.Time
		for k, v := range l.cache {
			if oldestKey == "" || v.at.Before(oldestTime) {
				oldestKey = k
				oldestTime = v.at
			}
		}
		delete(l.cache, oldestKey)
	}
	l.cacheMu.Unlock()
	return analysis, nil
}

// callWithRetry makes one retry with 2s backoff on transient errors.
func (l *LLM) callWithRetry(ctx context.Context, prompt string) (string, error) {
	analysis, err := l.dispatch(ctx, prompt)
	if err == nil {
		return analysis, nil
	}
	// Retry once after 2s for transient errors
	select {
	case <-ctx.Done():
		return "", err
	case <-time.After(2 * time.Second):
	}
	return l.dispatch(ctx, prompt)
}

func (l *LLM) dispatch(ctx context.Context, prompt string) (string, error) {
	switch l.Backend {
	case "ollama":
		return l.ollama(ctx, prompt)
	case "saia", "tud", "openai":
		return l.openaiCompatible(ctx, prompt)
	default:
		return "", fmt.Errorf("unknown backend %q", l.Backend)
	}
}

func (l *LLM) ollama(ctx context.Context, prompt string) (string, error) {
	body, _ := json.Marshal(map[string]any{"model": l.Model, "prompt": prompt, "stream": false})
	return l.post(ctx, l.URL+"/api/generate", body, func(raw []byte) (string, error) {
		var out struct{ Response string }
		if err := json.Unmarshal(raw, &out); err != nil {
			return "", err
		}
		return out.Response, nil
	})
}

func (l *LLM) openaiCompatible(ctx context.Context, prompt string) (string, error) {
	body, _ := json.Marshal(map[string]any{
		"model":    l.Model,
		"messages": []map[string]string{{"role": "user", "content": prompt}},
	})
	return l.post(ctx, l.URL+"/chat/completions", body, func(raw []byte) (string, error) {
		var out struct {
			Choices []struct{ Message struct{ Content string } }
		}
		if err := json.Unmarshal(raw, &out); err != nil {
			return "", err
		}
		if len(out.Choices) == 0 {
			return "", fmt.Errorf("empty completion")
		}
		return out.Choices[0].Message.Content, nil
	})
}

func (l *LLM) post(ctx context.Context, url string, body []byte, parse func([]byte) (string, error)) (string, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		return "", err
	}
	req.Header.Set("Content-Type", "application/json")
	if l.Key != "" {
		req.Header.Set("Authorization", "Bearer "+l.Key) // sent, never logged
	}
	resp, err := l.client.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	var buf bytes.Buffer
	if _, err := buf.ReadFrom(resp.Body); err != nil {
		return "", err
	}
	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("llm %s: status %d", l.Backend, resp.StatusCode)
	}
	return parse(buf.Bytes())
}

func joinSymptoms(list []string) string {
	out := ""
	for i, s := range list {
		if i > 0 {
			out += ","
		}
		out += s
	}
	return out
}
