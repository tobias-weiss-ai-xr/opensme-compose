package config

import (
	"strings"
	"testing"
	"time"
)

func setenv(t *testing.T, kv map[string]string) {
	t.Helper()
	for k, v := range kv {
		t.Setenv(k, v)
	}
}

func TestDefaults(t *testing.T) {
	c, err := FromEnv()
	if err != nil {
		t.Fatal(err)
	}
	if c.Interval != 60*time.Second {
		t.Errorf("interval default = %s, want 60s", c.Interval)
	}
	if len(c.Watch) != 1 || c.Watch[0] != "opensme-compose" {
		t.Errorf("watch default = %v, want [opensme-compose]", c.Watch)
	}
	if c.AllowHeal {
		t.Error("allow-heal must default to false")
	}
	if c.LLM.Backend != "none" {
		t.Errorf("llm backend default = %s, want none", c.LLM.Backend)
	}
	if c.APIAddr != "0.0.0.0:8082" {
		t.Errorf("api addr default = %s", c.APIAddr)
	}
}

func TestIntervalFormats(t *testing.T) {
	setenv(t, map[string]string{"DEV_AGENT_INTERVAL": "90"})
	c, err := FromEnv()
	if err != nil || c.Interval != 90*time.Second {
		t.Fatalf("bare seconds: %v %v", c.Interval, err)
	}
	setenv(t, map[string]string{"DEV_AGENT_INTERVAL": "2m"})
	c, err = FromEnv()
	if err != nil || c.Interval != 2*time.Minute {
		t.Fatalf("duration: %v %v", c.Interval, err)
	}
	setenv(t, map[string]string{"DEV_AGENT_INTERVAL": "bogus"})
	if _, err := FromEnv(); err == nil {
		t.Fatal("bogus interval must error")
	}
}

func TestAllowHealAndWatch(t *testing.T) {
	setenv(t, map[string]string{
		"DEV_AGENT_ALLOW_HEAL": "true",
		"DEV_AGENT_WATCH":      "opensme, default",
	})
	c, err := FromEnv()
	if err != nil {
		t.Fatal(err)
	}
	if !c.AllowHeal {
		t.Error("allow-heal=true not applied")
	}
	if len(c.Watch) != 2 || c.Watch[0] != "opensme" || c.Watch[1] != "default" {
		t.Errorf("watch = %v", c.Watch)
	}
}

func TestLLMBackendValidation(t *testing.T) {
	setenv(t, map[string]string{"DEV_AGENT_LLM_BACKEND": "bogus"})
	if _, err := FromEnv(); err == nil {
		t.Fatal("unknown backend must error")
	}
	setenv(t, map[string]string{
		"DEV_AGENT_LLM_BACKEND": "ollama",
		"DEV_AGENT_LLM_URL":     "http://llm:11434",
		"DEV_AGENT_LLM_MODEL":   "m",
	})
	c, err := FromEnv()
	if err != nil {
		t.Fatal(err)
	}
	if c.LLM.URL != "http://llm:11434" || c.LLM.Model != "m" {
		t.Errorf("llm override = %+v", c.LLM)
	}
}

func TestHistoryMax(t *testing.T) {
	setenv(t, map[string]string{"DEV_AGENT_HISTORY_MAX": "50"})
	c, err := FromEnv()
	if err != nil {
		t.Fatal(err)
	}
	if c.HistoryMax != 50 {
		t.Errorf("HistoryMax = %d, want 50", c.HistoryMax)
	}
	if !strings.Contains(c.String(), "history-max=50") {
		t.Errorf("String() must include history-max: %s", c.String())
	}
	setenv(t, map[string]string{"DEV_AGENT_HISTORY_MAX": "-1"})
	if _, err := FromEnv(); err == nil {
		t.Error("negative history-max must error")
	}
}

func TestDraftTTLEnv(t *testing.T) {
	// Default: 0 = expiry off (a pending decision never disappears unless
	// the operator opts in).
	c, err := FromEnv()
	if err != nil {
		t.Fatal(err)
	}
	if c.DraftTTL != 0 {
		t.Errorf("default DraftTTL = %v, want 0 (off)", c.DraftTTL)
	}
	setenv(t, map[string]string{"DEV_AGENT_DRAFT_TTL": "168h"})
	c, err = FromEnv()
	if err != nil {
		t.Fatal(err)
	}
	if c.DraftTTL != 168*time.Hour {
		t.Errorf("DraftTTL = %v, want 168h", c.DraftTTL)
	}
	setenv(t, map[string]string{"DEV_AGENT_DRAFT_TTL": "bogus"})
	if _, err := FromEnv(); err == nil {
		t.Error("invalid draft TTL must error")
	}
}

func TestLLMBudgetEnv(t *testing.T) {
	// Default: 0 = unlimited (no behavior change when unset).
	setenv(t, map[string]string{"DEV_AGENT_LLM_BACKEND": "ollama"})
	c, err := FromEnv()
	if err != nil {
		t.Fatal(err)
	}
	if c.LLM.MaxRequests != 0 {
		t.Errorf("default MaxRequests = %d, want 0 (unlimited)", c.LLM.MaxRequests)
	}
	// Explicit limit is parsed.
	setenv(t, map[string]string{
		"DEV_AGENT_LLM_BACKEND":      "ollama",
		"DEV_AGENT_LLM_MAX_REQUESTS": "5",
	})
	c, err = FromEnv()
	if err != nil {
		t.Fatal(err)
	}
	if c.LLM.MaxRequests != 5 {
		t.Errorf("MaxRequests = %d, want 5", c.LLM.MaxRequests)
	}
	// Negative / malformed values must error.
	setenv(t, map[string]string{
		"DEV_AGENT_LLM_BACKEND":      "ollama",
		"DEV_AGENT_LLM_MAX_REQUESTS": "-3",
	})
	if _, err := FromEnv(); err == nil {
		t.Error("negative max-requests must error")
	}
	setenv(t, map[string]string{
		"DEV_AGENT_LLM_BACKEND":      "ollama",
		"DEV_AGENT_LLM_MAX_REQUESTS": "many",
	})
	if _, err := FromEnv(); err == nil {
		t.Error("malformed max-requests must error")
	}
}

func TestStringOmitsSecrets(t *testing.T) {
	setenv(t, map[string]string{
		"DEV_AGENT_LLM_BACKEND": "openai",
		"DEV_AGENT_LLM_KEY":     "sk-supersecret",
	})
	c, _ := FromEnv()
	if s := c.String(); contains(s, "sk-supersecret") {
		t.Errorf("String() leaked key: %s", s)
	}
}

func contains(s, sub string) bool {
	return len(sub) == 0 || (len(s) >= len(sub) && indexOf(s, sub) >= 0)
}

func indexOf(s, sub string) int {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return i
		}
	}
	return -1
}
