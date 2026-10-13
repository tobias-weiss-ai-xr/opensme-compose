package knowledge

import (
	"os"
	"strings"
	"testing"
	"testing/fstest"
)

func testFS() fstest.MapFS {
	return fstest.MapFS{
		"kb/stalwart.json": &fstest.MapFile{Data: []byte(`{
			"schema": 1, "service": "stalwart", "kb_version": "2026.10",
			"runbooks": [{"symptoms": ["listener flapping"], "diagnosis": "port collision", "remediation": ["check bind error"]}]
		}`)},
		"kb/postgres.json": &fstest.MapFile{Data: []byte(`{
			"schema": 1, "service": "postgres", "kb_version": "2026.10",
			"runbooks": [
				{"symptoms": ["out of memory"], "diagnosis": "work_mem too high", "remediation": ["lower work_mem"]},
				{"symptoms": ["too many clients"], "diagnosis": "pool exhausted", "remediation": ["find leaker"]}
			]
		}`)},
	}
}

func TestLoad(t *testing.T) {
	s, err := Load(testFS(), "kb")
	if err != nil {
		t.Fatal(err)
	}
	if got := s.Services(); len(got) != 2 || got[0] != "postgres" || got[1] != "stalwart" {
		t.Errorf("services = %v", got)
	}
}

func TestQueryByService(t *testing.T) {
	s, _ := Load(testFS(), "kb")
	if rbs := s.Query("postgres"); len(rbs) != 2 {
		t.Errorf("postgres runbooks = %d, want 2", len(rbs))
	}
	if rbs := s.Query("nope"); rbs != nil {
		t.Errorf("unknown service must return nil, got %v", rbs)
	}
}

func TestQueryBySymptom(t *testing.T) {
	s, _ := Load(testFS(), "kb")
	// Case-insensitive substring match.
	if m := s.MatchSymptom("FLAP"); len(m) != 1 || m[0].Service != "stalwart" {
		t.Errorf("FLAP match = %+v", m)
	}
	if m := s.MatchSymptom("memory"); len(m) != 1 || m[0].Service != "postgres" {
		t.Errorf("memory match = %+v", m)
	}
	if m := s.MatchSymptom("zzz-unknown"); len(m) != 0 {
		t.Errorf("unknown symptom must return empty, got %+v", m)
	}
	if m := s.MatchSymptom("  "); m != nil {
		t.Errorf("blank symptom must return nil, got %+v", m)
	}
}

func TestMalformedFails(t *testing.T) {
	cases := map[string]string{
		"bad json":      `{"schema": 1, "service":`,
		"wrong schema":  `{"schema": 2, "service": "x", "runbooks": [{"symptoms": ["s"], "diagnosis": "d", "remediation": ["r"]}]}`,
		"empty svc":     `{"schema": 1, "service": "", "runbooks": [{"symptoms": ["s"], "diagnosis": "d", "remediation": ["r"]}]}`,
		"name mismatch": `{"schema": 1, "service": "other", "runbooks": [{"symptoms": ["s"], "diagnosis": "d", "remediation": ["r"]}]}`,
		"no runbooks":   `{"schema": 1, "service": "x", "runbooks": []}`,
	}
	for name, data := range cases {
		fsys := fstest.MapFS{"kb/broken.json": &fstest.MapFile{Data: []byte(data)}}
		if _, err := Load(fsys, "kb"); err == nil {
			t.Errorf("%s: malformed runbook must fail", name)
		}
	}
}

// TestRealKB validates the shipped opensme-knowledge directory: every core
// service has a runbook and all files parse (dev-agent-knowledge spec).
func TestRealKB(t *testing.T) {
	fsys := os.DirFS("../..")
	s, err := Load(fsys, "opensme-knowledge")
	if err != nil {
		t.Fatal(err)
	}
	core := []string{"traefik", "postgres", "zitadel", "stalwart", "sogo", "opencloud", "invoice-ninja", "paperless", "operaton", "common"}
	have := map[string]bool{}
	for _, svc := range s.Services() {
		have[svc] = true
	}
	var missing []string
	for _, svc := range core {
		if !have[svc] {
			missing = append(missing, svc)
		}
	}
	if len(missing) > 0 {
		t.Errorf("core services without runbook: %s", strings.Join(missing, ","))
	}
}

// TestSourceDuty — agent-knowledge: every answer evidence (Match) must carry
// its source file and KB version so the consumer can show provenance.
func TestSourceDuty(t *testing.T) {
	s, err := Load(testFS(), "kb")
	if err != nil {
		t.Fatal(err)
	}
	ms := s.MatchSymptom("listener flapping")
	if len(ms) == 0 {
		t.Fatal("expected a match for 'listener flapping'")
	}
	m := ms[0]
	if m.Source != "stalwart.json" {
		t.Errorf("source duty: got source %q, want stalwart.json", m.Source)
	}
	if m.KBVersion == "" {
		t.Error("source duty: kb_version must be set on the answer evidence")
	}
	if s.Version("stalwart") != m.KBVersion {
		t.Errorf("version mismatch: Store=%q Match=%q", s.Version("stalwart"), m.KBVersion)
	}
}

// TestLoadRequiresKBVersion — agent-knowledge aktualität: a runbook file
// without kb_version must be rejected (source duty enforced at load).
func TestLoadRequiresKBVersion(t *testing.T) {
	badFS := fstest.MapFS{
		"kb/x.json": &fstest.MapFile{Data: []byte(`{"schema":1,"service":"x","runbooks":[{"symptoms":["s"],"diagnosis":"d","remediation":["r"]}]}`)},
	}
	if _, err := Load(badFS, "kb"); err == nil {
		t.Error("expected error when kb_version missing (aktualität/source duty)")
	}
}
