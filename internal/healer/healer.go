// Package healer executes remediation actions with dry-run default and
// explicit consent (dev-agent spec: heal is rejected without consent).
//
// Consent is global (DEV_AGENT_ALLOW_HEAL). Per-runbook consent is carried
// by the requires-consent flag on knowledge.Runbook (Runbook.RequiresConsent)
// and surfaced in operator output; the healer's only executable action today
// is `restart`, which is already Allow-gated. A future mutating action (e.g.
// per-volume prune) MUST gate on the runbook flag, not only on Allow.
package healer

import (
	"context"
	"fmt"
	"os/exec"
	"regexp"
	"time"
)

// Actions supported by the healer.
const (
	ActionRestart = "restart"
	ActionPrune   = "prune" // dry-run only — never deletes
	ActionWait    = "wait"
)

// Receipt is the audit record of one heal attempt (dry-run or real).
type Receipt struct {
	Time     time.Time `json:"time"`
	Action   string    `json:"action"`
	Target   string    `json:"target"`
	Executed bool      `json:"executed"`
	Output   string    `json:"output,omitempty"`
	Error    string    `json:"error,omitempty"`
}

// Runner executes a healing docker command. Implementations enforce the
// action allowlist.
type Runner interface {
	Run(ctx context.Context, args ...string) (string, error)
}

// validTarget constrains container names to docker's charset — this value is
// interpolated into a CLI argument, never a shell.
var validTarget = regexp.MustCompile(`^[a-zA-Z0-9][a-zA-Z0-9_.-]*$`)

// Healer gates remediation behind AllowHeal.
type Healer struct {
	Allow  bool
	runner Runner
}

// New creates a Healer; nil runner uses the real docker CLI.
func New(allow bool, r Runner) *Healer {
	if r == nil {
		r = cliRunner{}
	}
	return &Healer{Allow: allow, runner: r}
}

type cliRunner struct{}

// Run executes only the allowlisted healing command (docker restart).
func (cliRunner) Run(ctx context.Context, args ...string) (string, error) {
	if len(args) < 2 || args[0] != "restart" {
		return "", fmt.Errorf("healer: command %v rejected (only 'docker restart <name>' allowed)", args)
	}
	out, err := exec.CommandContext(ctx, "docker", args...).CombinedOutput()
	return string(out), err
}

// Execute performs the action. Without consent it returns a dry-run receipt
// and executes nothing. Prune is ALWAYS dry-run (design decision 4).
func (h *Healer) Execute(ctx context.Context, action, target string) Receipt {
	rc := Receipt{Time: time.Now().UTC(), Action: action, Target: target}
	if !validTarget.MatchString(target) && action != ActionPrune && action != ActionWait {
		rc.Error = fmt.Sprintf("invalid target %q", target)
		return rc
	}
	switch action {
	case ActionRestart:
		if !h.Allow {
			rc.Output = fmt.Sprintf("dry-run: would run `docker restart %s`", target)
			return rc
		}
		out, err := h.runner.Run(ctx, "restart", target)
		rc.Output, rc.Error = out, errString(err)
		rc.Executed = err == nil
	case ActionPrune:
		// ponytail: prune stays dry-run forever; real prune needs its own consent class if ever wanted
		rc.Output = "dry-run: prune lists dangling volumes/networks but never deletes (policy)"
	case ActionWait:
		rc.Output = "wait: cooldown 10s before reassessment"
		if h.Allow {
			select {
			case <-ctx.Done():
			case <-time.After(10 * time.Second):
			}
		}
		rc.Executed = true
	default:
		rc.Error = fmt.Sprintf("unknown action %q (want restart|prune|wait)", action)
	}
	return rc
}

func errString(err error) string {
	if err == nil {
		return ""
	}
	return err.Error()
}
