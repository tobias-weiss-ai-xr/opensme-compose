// Package knowledge loads and queries the embedded opensme-knowledge
// runbook knowledge base (dev-agent-knowledge spec).
package knowledge

import (
	"encoding/json"
	"fmt"
	"io/fs"
	"path"
	"sort"
	"strings"
)

// SchemaVersion is the only supported runbook schema.
const SchemaVersion = 1

// Runbook is a single symptom→diagnosis→remediation record.
type Runbook struct {
	Symptoms    []string `json:"symptoms"`
	Diagnosis   string   `json:"diagnosis"`
	Remediation []string `json:"remediation"`
	Flags       []string `json:"flags"`
}

// Entry is one service file.
type Entry struct {
	Schema    int       `json:"schema"`
	Service   string    `json:"service"`
	KBVersion string    `json:"kb_version"`
	Runbooks  []Runbook `json:"runbooks"`
}

// Match is a knowledge query hit. It carries the source file and KB version
// so the consumer can show provenance — the answer evidence SHALL always
// point at where it came from (agent-knowledge: source duty + aktualität).
type Match struct {
	Service   string
	Runbook   Runbook
	Source    string `json:"source"`
	KBVersion string `json:"kb_version"`
}

// Store is the loaded knowledge base.
type Store struct {
	byService map[string][]Runbook
	version   map[string]string // service -> kb_version (aktualität)
	source    map[string]string // service -> file name (source duty)
}

// Load reads every *.json runbook file in dir of fsys. A malformed file
// (bad JSON, wrong schema, empty/mismatched service, empty runbooks) is an
// error — the agent refuses to start and the static check fails.
func Load(fsys fs.FS, dir string) (*Store, error) {
	entries, err := fs.ReadDir(fsys, dir)
	if err != nil {
		return nil, fmt.Errorf("knowledge dir: %w", err)
	}
	s := &Store{byService: map[string][]Runbook{}, version: map[string]string{}, source: map[string]string{}}
	for _, e := range entries {
		if e.IsDir() || !strings.HasSuffix(e.Name(), ".json") {
			continue
		}
		raw, err := fs.ReadFile(fsys, path.Join(dir, e.Name()))
		if err != nil {
			return nil, fmt.Errorf("%s: %w", e.Name(), err)
		}
		var ent Entry
		if err := json.Unmarshal(raw, &ent); err != nil {
			return nil, fmt.Errorf("%s: malformed runbook: %w", e.Name(), err)
		}
		if ent.Schema != SchemaVersion {
			return nil, fmt.Errorf("%s: schema %d, want %d", e.Name(), ent.Schema, SchemaVersion)
		}
		if ent.Service == "" {
			return nil, fmt.Errorf("%s: empty service name", e.Name())
		}
		if ent.KBVersion == "" {
			return nil, fmt.Errorf("%s: missing kb_version (aktualität / source duty)", e.Name())
		}
		want := strings.TrimSuffix(e.Name(), ".json")
		if ent.Service != want {
			return nil, fmt.Errorf("%s: service %q does not match file name", e.Name(), ent.Service)
		}
		if len(ent.Runbooks) == 0 {
			return nil, fmt.Errorf("%s: no runbooks", e.Name())
		}
		s.byService[ent.Service] = ent.Runbooks
		s.version[ent.Service] = ent.KBVersion
		s.source[ent.Service] = e.Name()
	}
	if len(s.byService) == 0 {
		return nil, fmt.Errorf("no runbooks found in %s", dir)
	}
	return s, nil
}

// Query returns all runbooks for a service; unknown services return nil.
func (s *Store) Query(service string) []Runbook {
	return s.byService[service]
}

// MatchSymptom returns runbooks whose symptoms contain the given substring
// (case-insensitive), across all services.
func (s *Store) MatchSymptom(symptom string) []Match {
	q := strings.ToLower(strings.TrimSpace(symptom))
	if q == "" {
		return nil
	}
	var services []string
	for svc := range s.byService {
		services = append(services, svc)
	}
	sort.Strings(services)
	var out []Match
	for _, svc := range services {
		for _, rb := range s.byService[svc] {
			for _, sym := range rb.Symptoms {
				if strings.Contains(strings.ToLower(sym), q) {
					out = append(out, Match{Service: svc, Runbook: rb, Source: s.source[svc], KBVersion: s.version[svc]})
					break
				}
			}
		}
	}
	return out
}

// Version returns the kb_version of the file that shipped a service's
// runbooks (aktualität / provenance), "" if the service is unknown.
func (s *Store) Version(service string) string {
	return s.version[service]
}

// Services returns the sorted list of covered services.
func (s *Store) Services() []string {
	var out []string
	for svc := range s.byService {
		out = append(out, svc)
	}
	sort.Strings(out)
	return out
}
