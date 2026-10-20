# tests/conftest.py — Shared utilities for the spec-contract-test harness
#
# This module provides:
#   - ComposeLoader: parses all compose files and extracts services
#   - SpecLoader: loads spec files from specs/
#   - ContractLoader: loads contract files from contracts/
#   - EnvLoader: parses .env.example for variable definitions
#   - Result: test result tracking with colored output
#
# Usage:
#   from conftest import ComposeLoader, SpecLoader, ContractLoader, EnvLoader, Result
#
# All test scripts import from here. No external dependencies beyond PyYAML.

import re
import yaml
from pathlib import Path
from typing import Optional

# Project root (parent of tests/)
ROOT = Path(__file__).resolve().parent.parent

# Register Docker Compose extension tags (!reset, !override)
# These are used in profile files to reset/override values from base compose.
yaml.SafeLoader.add_constructor('!reset', lambda loader, node: None)
yaml.SafeLoader.add_constructor('!override', lambda loader, node: loader.construct_scalar(node))


# ─── Result tracking ───────────────────────────────────────────

class Result:
    """Accumulates test results with colored output."""

    COLORS = {
        "PASS":  "\033[0;32m",
        "FAIL":  "\033[0;31m",
        "WARN":  "\033[1;33m",
        "SKIP":  "\033[0;34m",
        "INFO":  "\033[0;36m",
        "BOLD":  "\033[1m",
        "NC":    "\033[0m",
    }

    def __init__(self, name: str):
        self.name = name
        self.passed = 0
        self.failed = 0
        self.warnings = 0
        self.skipped = 0
        self.errors: list[str] = []
        self.warns: list[str] = []

    def ok(self, msg: str = ""):
        self.passed += 1
        if msg:
            print(f"  {self.C['PASS']}✓{self.C['NC']} {msg}")

    def fail(self, msg: str):
        self.failed += 1
        self.errors.append(msg)
        print(f"  {self.C['FAIL']}✗{self.C['NC']} {msg}")

    def warn(self, msg: str):
        self.warnings += 1
        self.warns.append(msg)
        print(f"  {self.C['WARN']}⚠{self.C['NC']} {msg}")

    def skip(self, msg: str = ""):
        self.skipped += 1
        if msg:
            print(f"  {self.C['SKIP']}○{self.C['NC']} {msg}")

    def info(self, msg: str):
        print(f"  {self.C['INFO']}ℹ{self.C['NC']} {msg}")

    @property
    def C(self):
        return self.COLORS

    def header(self, msg: str):
        print(f"\n{self.C['BOLD']}── {msg} ──{self.C['NC']}")

    def summary(self) -> bool:
        """Print summary. Returns True if all passed (no failures)."""
        total = self.passed + self.failed + self.warnings + self.skipped
        status = self.C["PASS"] + "PASS" if self.failed == 0 else self.C["FAIL"] + "FAIL"
        print(f"\n{self.C['BOLD']}── {self.name}: {self.passed} passed, "
              f"{self.failed} failed, {self.warnings} warnings, "
              f"{self.skipped} skipped ({total} total) {status}{self.C['NC']}")
        if self.errors:
            print(f"\n{self.C['FAIL']}Failures:{self.C['NC']}")
            for e in self.errors:
                print(f"  • {e}")
        if self.warns:
            print(f"\n{self.C['WARN']}Warnings:{self.C['NC']}")
            for w in self.warns:
                print(f"  • {w}")
        return self.failed == 0


# ─── Compose file loader ───────────────────────────────────────

class ComposeLoader:
    """Loads and parses all compose files."""

    # Base compose files (not profiles — profiles are overrides that
    # intentionally modify resource limits, healthchecks, etc.)
    COMPOSE_FILES = [
        "docker-compose.yml",
        "idm/zitadel.yml",
        "idm/casdoor.yml",
        "opencloud/opencloud.yml",
        "opencloud/minio.yml",
        "mail/stalwart.yml",
        "mail/sogo.yml",
        "services/invoice-ninja.yml",
        "services/paperless.yml",
        "services/cryptpad.yml",
        "services/synapse.yml",
        "services/element.yml",
        "services/notes.yml",
        "services/camunda.yml",
        "services/ticketing.yml",
        "services/cms.yml",
        "services/store.yml",
        "services/ai.yml",
        "services/limesurvey.yml",
        "monitoring/dev-agent.yml",
        "monitoring/predictive-agent.yml",
        "monitoring/ollama.yml",
        "monitoring/taskfleet.yml",
    ]

    # Profile files are loaded separately (not merged into the base)
    PROFILE_FILES = [
        "profiles/soho.yml",
        "profiles/small.yml",
        "profiles/medium.yml",
        "profiles/demo.dev.yml",
        "profiles/demo.live.yml",
        "profiles/demo.coexist.yml",
        "profiles/system-traefik.yml",
    ]

    def __init__(self, root: Path = ROOT):
        self.root = root
        self.services: dict[str, dict] = {}  # service_name -> {file, data, ...}
        self.volumes: dict[str, list[str]] = {}  # file -> [volume names]
        self.networks: dict[str, list[str]] = {}  # file -> [network names]
        self._loaded = False

    def load(self, include_profiles: bool = False):
        """Parse all compose files and extract services, volumes, networks.

        Args:
            include_profiles: If True, also load profile override files.
                              Defaults to False (profiles are overrides that
                              intentionally modify resource limits, etc.)
        """
        if self._loaded:
            return

        files = self.COMPOSE_FILES + (self.PROFILE_FILES if include_profiles else [])
        for rel_path in files:
            full_path = self.root / rel_path
            if not full_path.exists():
                continue
            try:
                with open(full_path) as f:
                    data = yaml.safe_load(f)
            except yaml.YAMLError as e:
                print(f"  {Result.COLORS['FAIL']}✗{Result.COLORS['NC']} "
                      f"YAML parse error in {rel_path}: {e}")
                continue
            if not data:
                continue

            # Extract services
            for svc_name, svc_data in (data.get("services") or {}).items():
                self.services[svc_name] = {
                    "name": svc_name,
                    "file": rel_path,
                    "data": svc_data or {},
                }

            # Extract volumes
            self.volumes[rel_path] = list((data.get("volumes") or {}).keys())

            # Extract networks
            self.networks[rel_path] = list((data.get("networks") or {}).keys())

        self._loaded = True

    def get_service(self, name: str) -> Optional[dict]:
        """Get a service by name. Returns None if not found."""
        self.load()
        return self.services.get(name)

    def get_services_by_file(self, file: str) -> dict[str, dict]:
        """Get all services defined in a specific compose file."""
        self.load()
        return {n: s for n, s in self.services.items() if s["file"] == file}

    def all_service_names(self) -> list[str]:
        """Get all unique service names."""
        self.load()
        return sorted(self.services.keys())


# ─── Spec loader ───────────────────────────────────────────────

class SpecLoader:
    """Loads spec files from specs/ directory."""

    SPEC_DIR = ROOT / "specs"

    def __init__(self):
        self.specs: dict[str, dict] = {}  # service_name -> spec
        self._loaded = False

    def load(self):
        if self._loaded:
            return self.specs

        for spec_file in sorted(self.SPEC_DIR.glob("*.yml")):
            if spec_file.name == "README.md":
                continue
            try:
                with open(spec_file) as f:
                    data = yaml.safe_load(f)
            except yaml.YAMLError as e:
                print(f"  {Result.COLORS['FAIL']}✗{Result.COLORS['NC']} "
                      f"Spec parse error in {spec_file.name}: {e}")
                continue
            if not data:
                continue
            for svc_name, spec_data in (data.get("services") or {}).items():
                spec_data["_spec_file"] = spec_file.name
                self.specs[svc_name] = spec_data

        self._loaded = True
        return self.specs

    def get_spec(self, name: str) -> Optional[dict]:
        self.load()
        return self.specs.get(name)

    def all_service_names(self) -> list[str]:
        self.load()
        return sorted(self.specs.keys())


# ─── Contract loader ───────────────────────────────────────────

class ContractLoader:
    """Loads contract files from contracts/ directory."""

    CONTRACT_DIR = ROOT / "contracts"

    def __init__(self):
        self.contracts: list[dict] = []
        self._loaded = False

    def load(self) -> list[dict]:
        if self._loaded:
            return self.contracts

        for contract_file in sorted(self.CONTRACT_DIR.glob("*.yml")):
            if contract_file.name == "README.md":
                continue
            try:
                with open(contract_file) as f:
                    # YAML may contain multiple documents (---)
                    for doc in yaml.safe_load_all(f):
                        if doc:
                            doc["_contract_file"] = contract_file.name
                            self.contracts.append(doc)
            except yaml.YAMLError as e:
                print(f"  {Result.COLORS['FAIL']}✗{Result.COLORS['NC']} "
                      f"Contract parse error in {contract_file.name}: {e}")

        self._loaded = True
        return self.contracts


# ─── Env loader ────────────────────────────────────────────────

class EnvLoader:
    """Parses .env.example for variable definitions."""

    def __init__(self, path: Path = ROOT / ".env.example"):
        self.path = path
        self.vars: set[str] = set()
        self._loaded = False

    def load(self) -> set[str]:
        if self._loaded:
            return self.vars

        if not self.path.exists():
            return self.vars

        with open(self.path) as f:
            for line in f:
                line = line.strip()
                # Skip comments and empty lines
                if not line or line.startswith("#"):
                    continue
                # Extract VAR=VALUE or VAR=
                match = re.match(r"^([A-Z][A-Z0-9_]*)=", line)
                if match:
                    self.vars.add(match.group(1))

        self._loaded = True
        return self.vars

    def has(self, var: str) -> bool:
        return var in self.load()

    def all_vars(self) -> set[str]:
        return self.load()


# ─── Helpers ───────────────────────────────────────────────────

def extract_env_vars_from_compose(svc_data: dict) -> set[str]:
    """Extract env var names that MUST be in .env.example.

    Only extracts vars referenced via ${VAR} (without default).
    Vars with hardcoded values (KEY: value) or defaults (${VAR:-default})
    are skipped because they don't need .env.example.
    """
    env_vars = set()
    env = svc_data.get("environment") or {}

    if isinstance(env, dict):
        for key, val in env.items():
            if isinstance(val, str) and "${" in val:
                # Value references env vars — extract them
                for match in re.finditer(r'\$\{([^}]+)\}', val):
                    var_content = match.group(1)
                    if ':-' in var_content or ':' in var_content:
                        # Has default — skip
                        pass
                    elif var_content.isupper():
                        env_vars.add(var_content)
            # If value is hardcoded (not ${...}), the key itself doesn't
            # need to be in .env.example — it's set directly in compose.
            # But if the key is also referenced via ${KEY} elsewhere, it
            # will be caught by the text scan below.
    elif isinstance(env, list):
        for item in env:
            if isinstance(item, str):
                if "=" in item:
                    key, val = item.split("=", 1)
                    if "${" in val:
                        for match in re.finditer(r'\$\{([^}]+)\}', val):
                            var_content = match.group(1)
                            if ':-' in var_content or ':' in var_content:
                                pass
                            elif var_content.isupper():
                                env_vars.add(var_content)
                else:
                    # Bare KEY (no =) — must be in .env.example
                    if item.isupper():
                        env_vars.add(item)

    # Also extract ${VAR} references from command, labels, etc.
    # But skip ${VAR:-default} (has a default value)
    text = yaml.dump(svc_data)
    for match in re.finditer(r'\$\{([^}]+)\}', text):
        var_content = match.group(1)
        if ':-' in var_content or ':' in var_content:
            # Has default — skip
            pass
        elif var_content.isupper():
            env_vars.add(var_content)

    return env_vars


def extract_host_ports(svc_data: dict) -> list[int]:
    """Extract host port numbers from a service's ports block."""
    ports = svc_data.get("ports") or []
    host_ports = []
    for port in ports:
        if isinstance(port, str):
            # "8080:8080" or "80:80" or "127.0.0.1:8080:8080"
            parts = port.split(":")
            if len(parts) >= 2:
                try:
                    host_ports.append(int(parts[0]) if not parts[0].startswith("127") else int(parts[1]))
                except ValueError:
                    pass
            elif len(parts) == 1:
                try:
                    host_ports.append(int(parts[0]))
                except ValueError:
                    pass
        elif isinstance(port, int):
            host_ports.append(port)
        elif isinstance(port, dict):
            if "published" in port:
                host_ports.append(int(port["published"]))
    return host_ports


def has_healthcheck(svc_data: dict) -> bool:
    """Check if a service defines a healthcheck."""
    hc = svc_data.get("healthcheck")
    if not hc:
        return False
    if isinstance(hc, dict):
        return "test" in hc
    if isinstance(hc, str):
        return bool(hc)
    if isinstance(hc, list):
        return len(hc) > 0
    return False


def has_traefik_labels(svc_data: dict) -> bool:
    """Check if a service has traefik.enable=true label."""
    labels = svc_data.get("labels") or []
    for label in labels:
        if isinstance(label, str) and "traefik.enable=true" in label:
            return True
    return False


def get_image(svc_data: dict) -> Optional[str]:
    """Get the image for a service, or None if it uses build."""
    return svc_data.get("image")


def has_resource_limits(svc_data: dict) -> bool:
    """Check if a service has deploy.resources.limits."""
    deploy = svc_data.get("deploy") or {}
    resources = deploy.get("resources") or {}
    limits = resources.get("limits") or {}
    return bool(limits.get("memory") or limits.get("cpus"))


def get_networks(svc_data: dict) -> list[str]:
    """Get list of network names for a service."""
    networks = svc_data.get("networks")
    if isinstance(networks, list):
        return networks
    if isinstance(networks, dict):
        return list(networks.keys())
    return []


def get_volumes(svc_data: dict) -> list[str]:
    """Get list of volume mappings for a service."""
    vols = svc_data.get("volumes") or []
    result = []
    for v in vols:
        if isinstance(v, str):
            result.append(v)
        elif isinstance(v, dict):
            result.append(v.get("source", "") + ":" + v.get("target", ""))
    return result


def is_host_network(svc_data: dict) -> bool:
    """Check if a service uses network_mode: host."""
    return svc_data.get("network_mode") == "host"


# ─── Portal routing self-heal ─────────────────────────────────────────────

def wait_http_ok(session, url: str, deadline_s: float = 60.0, interval: float = 2.0,
                 stable: int = 1) -> bool:
    """Poll until `url` answers HTTP 200 `stable` times in a row.

    `stable=1` (default) keeps the historic single-success behaviour; higher
    values confirm a recovered route is actually stable, not mid-flap.
    """
    import time as _time
    import requests as _requests
    deadline = _time.time() + deadline_s
    streak = 0
    while _time.time() < deadline:
        try:
            if session.get(url, timeout=15).status_code == 200:
                streak += 1
                if streak >= stable:
                    return True
            else:
                streak = 0
        except _requests.RequestException:
            streak = 0
        _time.sleep(interval)
    return False


def ensure_portal_routed(session, portal_base: str, log=print) -> bool:
    """Make sure Traefik actually routes the portal after a recreate.

    Traefik's docker provider subscribes to container events; when many
    fast `up -d portal` recreates happen (journey suites do), the
    start-event of the new container can get lost against the stop-event
    of the old one — the router disappears and every request answers
    Traefik's bare "404 page not found" until the provider resyncs.

    Self-heal ladder:
      1. wait up to 75s for /health to turn 200 (benign boot delay —
         and every traefik (re)start fires a storm of LE ACME lookups
         for the .local demo routers that delays readiness)
      2. `docker restart opensme-traefik` (forced provider resync) and
         wait again up to 90s (covers the post-restart ACME storm)
    Returns True only when /health is 200 via the public route.
    """
    if wait_http_ok(session, portal_base + "/health", deadline_s=75, stable=2):
        return True
    log("portal not routed via traefik — forcing provider resync (traefik restart)")
    import subprocess as _sp
    _sp.run(["docker", "restart", "opensme-traefik"],
            capture_output=True, timeout=120)
    return wait_http_ok(session, portal_base + "/health", deadline_s=90, stable=2)


def print_boot_failure(proc, label="boot", tail=30):
    """Print the last non-empty lines of a failed subprocess boot.

    Journeys run boot scripts with capture_output=True (quiet success, no
    interleaving with the journey's own output). But that also means a
    FAILED boot shows only "rc=1" in the CI log — the actual error is in
    the swallowed streams and the flake is undiagnosable. Call this right
    after reporting the failure so the CI log shows WHY the boot diverged.
    """
    for name, stream in (("stdout", proc.stdout or ""), ("stderr", proc.stderr or "")):
        lines = [line for line in stream.splitlines() if line.strip()]
        if not lines:
            continue
        print(f"--- {label} {name} (last {min(len(lines), tail)} of {len(lines)} lines)")
        for line in lines[-tail:]:
            print(f"    {line}")
