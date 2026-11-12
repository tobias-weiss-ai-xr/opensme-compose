#!/usr/bin/env python3
"""
tests/00-static/scan_secrets.py — Secret scanner for compose files.

Scans all compose files and scripts for hardcoded secrets, passwords,
API keys, and tokens. Only .env.example may contain CHANGEME_ placeholders.

Usage:
    python3 tests/00-static/scan_secrets.py [directory]

Exit codes:
    0 = no secrets found
    1 = secrets found
"""

import shutil
import subprocess
import sys
import re
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result, ROOT


# Patterns that indicate hardcoded secrets.
# Negative lookahead excludes placeholders (${VAR}, $var, CHANGEME_), reads
# from config modules (config.X / os.environ.get / os.getenv), printf '%s'
# placeholders, command substitutions ($(...)) and JSON/YAML object openers
# ('password: { ... }' — a literal secret never starts with a spaced brace,
# while a '{SSHA512}...' hash without the space stays flagged) so variable
# *references* and generators are not mistaken for literal secrets.
_REF = r"(?!CHANGEME|\$[{(A-Za-z_]|%s|\{\s|(?:config|os|env)\.|\w+\()"
SECRET_PATTERNS = [
    (rf'password\s*[=:]\s*["\']?{_REF}[^"\'\s$]+', "hardcoded password"),
    (rf'api[_-]?key\s*[=:]\s*["\']?{_REF}[^"\'\s$]{{16,}}', "hardcoded API key"),
    (rf'secret\s*[=:]\s*["\']?{_REF}[^"\'\s$]{{16,}}', "hardcoded secret"),
    (rf'token\s*[=:]\s*["\']?{_REF}[^"\'\s$]{{16,}}', "hardcoded token"),
    (r'BEGIN\s+(RSA|EC|OPENSSH|PRIVATE)\s+KEY', "private key"),
    (r'-----BEGIN\s+PGP\s+MESSAGE-----', "PGP message"),
]

# Credential FORMAT patterns — match the token itself regardless of the
# variable name it is assigned to (REGISTRY_PROXY_PASSWORD=ghp_… slipped
# past the name-based patterns above). Word-bounded, high-entropy shapes.
# Checked separately: exemption via PLACEHOLDER_HINT (docs examples).
FORMAT_PATTERNS = [
    (r'\bgh[pousr]_[A-Za-z0-9]{20,}\b', "GitHub token (ghp_/gho_/ghs_…)"),
    (r'\bgithub_pat_[A-Za-z0-9_]{20,}\b', "GitHub fine-grained PAT"),
    (r'\bAKIA[0-9A-Z]{16}\b', "AWS access key id"),
    (r'\bxox[bpars]-[0-9A-Za-z-]{10,}\b', "Slack token"),
    (r'\bsk_live_[A-Za-z0-9]{20,}\b', "Stripe live key"),
]

# Placeholder-shaped values that LOOK like tokens but are documentation
# examples (e.g. ghp_your_read_scoped_token_here) — never fail on these.
PLACEHOLDER_HINT = re.compile(r'your[-_]|[-_]here\b|example|changeme|<[^>]+>', re.IGNORECASE)

# Internal network ranges that must never appear in a public repo.
# RFC1918 private ranges + link-local. Hostname-specific guards are deliberately
# NOT hardcoded here: naming internal hosts would re-introduce them into the
# public repo. Supply those via a private, gitignored denylist if desired.
INTERNAL_PATTERNS = [
    (r'\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b', "internal IPv4 (RFC1918 10/8)"),
    (r'\b172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}\b', "internal IPv4 (RFC1918 172.16-31/12)"),
    (r'\b192\.168\.\d{1,3}\.\d{1,3}\b', "internal IPv4 (RFC1918 192.168/16)"),
    (r'\b169\.254\.\d{1,3}\.\d{1,3}\b', "link-local IPv4 (169.254/16)"),
]

# Host-specific absolute paths that reveal the private deployment layout
# (e.g. a checkout of an internal infrastructure repo). The path itself is
# the leak, so these are matched literally in every file type.
INTERNAL_PATH_PATTERNS = [
    (r'/root/(?:git|build)/', "private root path"),
    (r'/home/[A-Za-z0-9_.-]+/(?:git|build)/', "private home path"),
]


def _denylist_patterns() -> list[tuple[str, str]]:
    """Extra literal markers from a private, gitignored denylist.

    Hostnames and non-RFC1918 addresses cannot be hardcoded here without
    re-introducing them into the public repo, so operators drop them into
    tests/00-static/.internal-denylist.txt (one marker per line).
    """
    path = ROOT / "tests/00-static/.internal-denylist.txt"
    if not path.is_file():
        return []
    out = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        marker = raw.strip()
        if marker and not marker.startswith("#"):
            out.append((re.escape(marker), "internal denylist entry"))
    return out


ALL_INTERNAL_PATTERNS = INTERNAL_PATTERNS + INTERNAL_PATH_PATTERNS + _denylist_patterns()

# CHANGEME_ values are OK in .env.example but NOT in compose files ("safe"
# defaults like ${VAR:-CHANGEME_...} are still known secrets — never allow them)
CHANGEME_PATTERN = re.compile(r'CHANGEME_[a-z_]+', re.IGNORECASE)

# Files to scan
#
# Discovery: ALL git-tracked files matching SCAN_EXTS (via `git ls-files`).
# A hardcoded dir whitelist is how registry/, docs/, .github/, contracts/,
# specs/ and postgres-init/ silently escaped scanning (bug found 2026-10:
# a planted fake PAT in the repo root scored "99 files, 0 findings, PASS").
# Tracked-file discovery keeps the same semantics as the CI leak-scan job.
SCAN_EXTS = {".yml", ".yaml", ".sh", ".py", ".md", ".env", ".env.example", ".env.demo", ".toml", ".cfg", ".ini", ".conf"}

# Test fixtures MAY deliberately embed fake secrets/canaries (see CI leak-scan
# excludes) — never scan them.
_SCAN_SKIP_NAME = re.compile(r'(^|_)(test)\.(py|go)$|_test\.(py|go)$|(^|/)node_modules/|(^|/)scan_secrets\.py$')


def _submodule_paths() -> set[Path]:
    """Absolute paths of registered git submodules (from .gitmodules)."""
    paths = set()
    gm = ROOT / ".gitmodules"
    if gm.exists():
        for line in gm.read_text().splitlines():
            line = line.strip()
            if line.startswith("path = "):
                paths.add((ROOT / line[len("path = "):].strip()).resolve())
    return paths


_SUBMODULES: set[Path] | None = None


def _in_submodule(fp: Path) -> bool:
    global _SUBMODULES
    if _SUBMODULES is None:
        _SUBMODULES = _submodule_paths()
    try:
        rel = fp.resolve()
        return any(rel.is_relative_to(sm) for sm in _SUBMODULES)
    except (ValueError, OSError):
        return False


# Values that are NOT secrets even when a name-based pattern matches:
# - boolean/config toggles shown in docs/envelopes (`.env` feature flags)
# - Rust API signatures in README docs (Option<String>, Result<..> …)
_NON_SECRET_VALUE = {
    "true", "false", "yes", "no", "on", "off",
    "option", "string", "vec", "result", "bool", "some", "none",
}


def _compose_static_ips(candidates: list[Path]) -> set[str]:
    """IPs the compose tree itself declares as container static addresses
    (`networks: … ipv4_address:`). Those values are overlay topology, not
    host infrastructure: referencing them elsewhere (e.g. a NO_PROXY default
    for the pinned OpenCloud address) is not an internal-IP leak."""
    out: set[str] = set()
    for fp in candidates:
        if not fp.is_file() or fp.suffix not in (".yml", ".yaml"):
            continue
        try:
            txt = fp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in re.finditer(r"ipv4_address\s*[:=]\s*([0-9.]+)", txt):
            out.add(m.group(1))
        # The compose network's own ipam subnet is topology as well: pinning
        # 172.19.0.0/16 is what makes the static addresses above resolvable.
        for m in re.finditer(r"subnet\s*[:=]\s*([0-9.]+)/\d+", txt):
            out.add(m.group(1))
    return out


def scan_file(filepath: Path, static_ips: set[str] | None = None) -> list[tuple[int, str, str]]:
    """Scan a file for secrets. Returns list of (line_number, pattern_name, line)."""
    findings = []
    # Scope rules (calibrated against the real tree):
    #  - NAME-based secret patterns (password=, token=, …) fire on non-test
    #    code only: python signatures/unpacking in tests/ are noise, and
    #    .env.example/.env.demo intentionally carry reproducible demo values.
    #  - FORMAT-based credential patterns + internal-range patterns fire on
    #    EVERY scanned file: real tokens must never exist anywhere, and demo
    #    files must still be free of private-range IPs.
    #    NOTE for future canary fixtures: fake tokens shaped like ghp_… in
    #    tests must carry a scanner exclusion or this layer will (correctly)
    #    fail.
    rel_str = str(filepath)
    is_env_sample = filepath.name in (".env.example", ".env.demo", ".env")
    in_tests = "/tests/" in rel_str
    try:
        with open(filepath, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f, 1):
                # Skip comments
                stripped = line.lstrip()
                if stripped.startswith("#"):
                    continue

                # Check for CHANGEME_ in compose files (not .env.example). This
                # INCLUDES ${VAR:-CHANGEME_...} inline defaults: those are
                # universally-known secrets that get rendered into running
                # containers whenever an operator forgets the .env entry (hit
                # paperless/invoice-ninja/notes, 2026-11-01 — scanner previously
                # stripped the "${..:-CHANGEME_..}" shape, so CI stayed green
                # while known fallbacks silently shipped). Prefer fail-fast
                # ${VAR:?must go in .env} or an inherently-safe empty default
                # ${VAR:-} — never a real value as a default.
                if filepath.suffix in (".yml", ".yaml"):
                    if CHANGEME_PATTERN.search(line):
                        findings.append((i, "CHANGEME in compose file (incl. :-defaults)", line.rstrip()))

                # Check for secret patterns
                if not in_tests and not is_env_sample:
                    for pattern, name in SECRET_PATTERNS:
                        if re.search(pattern, line, re.IGNORECASE):
                            # Exclude env var references like ${VAR}
                            if "${" not in line and "CHANGEME" not in line:
                                # Boolean/flag defaults (`…PASSWORD=false`,
                                # OC_SHARING_…_MUST_HAVE_PASSWORD=true) and Rust
                                # API signatures (`password: Option<String>`) are
                                # config/docs, not secrets — a truthful password
                                # is never the bare words true/false/yes/no or a
                                # type token.
                                value = re.search(
                                    r"password\s*[=:]\s*[\"']?([^\"'\s$<`]+)",
                                    line, re.IGNORECASE)
                                if value and (
                                    value.group(1).lower() in _NON_SECRET_VALUE
                                    or not value.group(1).strip('.')
                                ):
                                    # boolean flag value, type token, or a
                                    # ``…``-style placeholder — not a secret
                                    continue
                                findings.append((i, name, line.rstrip()))

                # Credential FORMAT patterns: match regardless of variable
                # name, but allow documented placeholder examples in the
                # env sample files.
                for pattern, name in FORMAT_PATTERNS:
                    if is_env_sample:
                        break
                    m = re.search(pattern, line)
                    if m and not PLACEHOLDER_HINT.search(line):
                        findings.append((i, name, line.rstrip()))

                # Check for internal network ranges / hostnames (never ship)
                for pattern, name in ALL_INTERNAL_PATTERNS:
                    if re.search(pattern, line):
                        # Compose-net static addressing (`networks: … ipv4_address:`
                        # — opencloud pins 172.19.0.17 so the edge file-provider
                        # CSP route cloud.home→IP survives recreates, 660804a) is
                        # container-internal topology, not host infra: the value
                        # can only ever be a private container IP inside an
                        # opensme-net overlay, so it carries no leak risk. Covers
                        # the declaring line AND any other line in this tree that
                        # references a compose-pinned address (e.g. NO_PROXY
                        # defaults that name the OpenCloud static IP).
                        if "ipv4_address:" in line:
                            continue
                        m = re.search(pattern, line)
                        if m and static_ips and m.group(0) in static_ips:
                            continue
                        # Documentation is published with the repo: internal
                        # addresses and private paths are leaks there too.
                        findings.append((i, name, line.rstrip()))
                        break
    except (OSError, UnicodeDecodeError):
        pass
    return findings


def main():
    result = Result("secret-scan")
    result.header("Layer 0: Secret scanning")

    scan_root = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT

    files_scanned = 0
    total_findings = 0

    # Gather candidate files: git-tracked (falls back to rglob outside a
    # repo, e.g. when pointed at an extracted tree).
    candidates: list[Path] = []
    git = shutil.which("git")
    ran_git = False
    if git and (scan_root / ".git").exists():
        try:
            out = subprocess.run([git, "ls-files", "-z"], cwd=scan_root,
                                 capture_output=True, timeout=60, check=True)
            candidates = [scan_root / p for p in out.stdout.decode().split("\0") if p]
            ran_git = True
        except (subprocess.SubprocessError, UnicodeDecodeError):
            candidates = []
    if not ran_git:
        for ext in SCAN_EXTS:
            candidates.extend(scan_root.rglob(f"*{ext}"))

    # IPs the compose tree pins as container static addresses — referencing
    # one of these is overlay topology, not an internal-IP leak.
    static_ips = _compose_static_ips(candidates)

    for fp in candidates:
        if ".git" in fp.parts or _SCAN_SKIP_NAME.search(str(fp)):
            continue
        if _in_submodule(fp):
            continue
        keep = fp.name in ("Dockerfile",) or fp.suffix in SCAN_EXTS or fp.name.endswith((".env.example", ".env.demo", ".env"))
        if not keep:
            continue
        if not fp.is_file():
            continue
        findings = scan_file(fp, static_ips)
        files_scanned += 1
        if findings:
            for line_no, name, line in findings:
                total_findings += 1
                # Only flag CHANGEME in compose files as error, not .env.example
                if "CHANGEME" in name and fp.name == ".env.example":
                    continue
                result.fail(f"{fp}:{line_no} — {name}: {line.strip()}")
        else:
            result.ok(f"{fp} clean")

    result.info(f"Scanned {files_scanned} files, {total_findings} findings")

    return result.summary()


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
