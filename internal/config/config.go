// Package config loads DEV_AGENT_* environment variables with documented
// defaults (dev-agent spec: env-driven defaults are applied).
package config

import (
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"
)

// Config is the effective agent configuration. Secrets (LLMKey) are never
// logged; String() omits them.
type Config struct {
	Interval   time.Duration
	Watch      []string
	StateDir   string
	AllowHeal  bool
	APIAddr    string
	HistoryMax int // cap on persisted history+evidence entries
	LLM        LLMConfig
	// Hostnames is the extra hostname list the anonymizer scrubs.
	Hostnames []string
}

// LLMConfig selects the (optional) analysis backend. Backend "none" disables
// all outbound analysis requests. MaxRequests caps outbound LLM calls per
// run; 0 = unlimited.
type LLMConfig struct {
	Backend     string // none|ollama|saia|tud|openai
	URL         string
	Key         string
	Model       string
	MaxRequests int
}

// FromEnv builds a Config from the environment, applying spec defaults.
func FromEnv() (*Config, error) {
	c := &Config{
		Interval:   60 * time.Second,
		Watch:      []string{"opensme-compose"},
		StateDir:   "/var/lib/opensme/bot",
		AllowHeal:  false,
		APIAddr:    "0.0.0.0:8082",
		HistoryMax: 100,
		LLM:        LLMConfig{Backend: "none"},
	}

	if v := os.Getenv("DEV_AGENT_INTERVAL"); v != "" {
		d, err := parseDuration(v)
		if err != nil {
			return nil, fmt.Errorf("DEV_AGENT_INTERVAL: %w", err)
		}
		c.Interval = d
	}
	if v := os.Getenv("DEV_AGENT_WATCH"); v != "" {
		c.Watch = splitList(v)
	}
	if v := os.Getenv("DEV_AGENT_STATE_DIR"); v != "" {
		c.StateDir = v
	}
	if v := os.Getenv("DEV_AGENT_ALLOW_HEAL"); v != "" {
		b, err := strconv.ParseBool(v)
		if err != nil {
			return nil, fmt.Errorf("DEV_AGENT_ALLOW_HEAL: %w", err)
		}
		c.AllowHeal = b
	}
	if v := os.Getenv("DEV_AGENT_API_ADDR"); v != "" {
		c.APIAddr = v
	}
	if v := os.Getenv("DEV_AGENT_HOSTNAMES"); v != "" {
		c.Hostnames = splitList(v)
	}
	if v := os.Getenv("DEV_AGENT_HISTORY_MAX"); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil || n < 0 {
			return nil, fmt.Errorf("DEV_AGENT_HISTORY_MAX: %w", err)
		}
		c.HistoryMax = n
	}

	// LLM backend (off by default).
	backend := strings.ToLower(os.Getenv("DEV_AGENT_LLM_BACKEND"))
	if backend == "" {
		backend = "none"
	}
	c.LLM.Backend = backend
	if backend != "none" {
		switch backend {
		case "ollama", "saia", "tud", "openai":
		default:
			return nil, fmt.Errorf("DEV_AGENT_LLM_BACKEND: unknown backend %q (want none|ollama|saia|tud|openai)", backend)
		}
		c.LLM.URL = envOr("DEV_AGENT_LLM_URL", llmURLDefault(backend))
		c.LLM.Key = envOr("DEV_AGENT_LLM_KEY", llmKeyDefault(backend))
		c.LLM.Model = envOr("DEV_AGENT_LLM_MODEL", llmModelDefault(backend))
		if v := os.Getenv("DEV_AGENT_LLM_MAX_REQUESTS"); v != "" {
			n, err := strconv.Atoi(v)
			if err != nil || n < 0 {
				return nil, fmt.Errorf("DEV_AGENT_LLM_MAX_REQUESTS: %w", err)
			}
			c.LLM.MaxRequests = n
		}
	}
	return c, nil
}

// String renders the effective configuration WITHOUT secrets.
func (c *Config) String() string {
	return fmt.Sprintf("interval=%s watch=%s state-dir=%s allow-heal=%t api=%s history-max=%d llm-backend=%s llm-max-requests=%d",
		c.Interval, strings.Join(c.Watch, ","), c.StateDir, c.AllowHeal, c.APIAddr, c.HistoryMax, c.LLM.Backend, c.LLM.MaxRequests)
}

func splitList(v string) []string {
	var out []string
	for _, p := range strings.Split(v, ",") {
		if p = strings.TrimSpace(p); p != "" {
			out = append(out, p)
		}
	}
	return out
}

// parseDuration accepts Go durations ("60s") and bare seconds ("60").
func parseDuration(v string) (time.Duration, error) {
	if d, err := time.ParseDuration(v); err == nil {
		return d, nil
	}
	n, err := strconv.Atoi(v)
	if err != nil {
		return 0, fmt.Errorf("invalid duration %q", v)
	}
	return time.Duration(n) * time.Second, nil
}

func envOr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func llmURLDefault(backend string) string {
	switch backend {
	case "ollama":
		return envOr("OLLAMA_URL", "http://ollama:11434")
	case "saia":
		return os.Getenv("SAIA_API_URL")
	case "tud":
		return os.Getenv("TUD_API_URL")
	case "openai":
		return envOr("OPENAI_API_URL", "https://api.openai.com/v1")
	}
	return ""
}

func llmKeyDefault(backend string) string {
	switch backend {
	case "saia":
		return os.Getenv("SAIA_API_KEY")
	case "tud":
		return os.Getenv("TUD_API_KEY")
	case "openai":
		return os.Getenv("OPENAI_API_KEY")
	}
	return ""
}

func llmModelDefault(backend string) string {
	switch backend {
	case "ollama":
		return envOr("OLLAMA_MODEL", "qwen3-30b-a3b:latest")
	case "saia":
		return "qwen3.5-35b-a3b"
	case "tud":
		return "GLM-5.2-AWQ-INT4"
	case "openai":
		return "gpt-4o"
	}
	return ""
}
