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

import sys
import re
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result, ROOT


# Patterns that indicate hardcoded secrets.
# Negative lookahead excludes placeholders (${VAR}, CHANGEME_) and reads from
# config modules (config.X / os.environ.get / os.getenv) so variable *references*
# are not mistaken for literal secrets. printf '%s' placeholders and command
# substitutions ($(...)) are generators, not literals.
_REF = r'(?!CHANGEME|\$\{|\$?\(|%s|(?:config|os|env)\.)'
SECRET_PATTERNS = [
    (rf'password\s*[=:]\s*["\']?{_REF}[^"\'\s$]+', "hardcoded password"),
    (rf'api[_-]?key\s*[=:]\s*["\']?{_REF}[^"\'\s$]{{16,}}', "hardcoded API key"),
    (rf'secret\s*[=:]\s*["\']?{_REF}[^"\'\s$]{{16,}}', "hardcoded secret"),
    (rf'token\s*[=:]\s*["\']?{_REF}[^"\'\s$]{{16,}}', "hardcoded token"),
    (r'BEGIN\s+(RSA|EC|OPENSSH|PRIVATE)\s+KEY', "private key"),
    (r'-----BEGIN\s+PGP\s+MESSAGE-----', "PGP message"),
]

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

# CHANGEME_ values are OK in .env.example but NOT in compose files
CHANGEME_PATTERN = re.compile(r'CHANGEME_[a-z_]+', re.IGNORECASE)

# Files to scan
SCAN_DIRS = ["docker-compose.yml", "idm/", "opencloud/", "mail/", "services/",
             "monitoring/", "profiles/", "scripts/", "portal/"]
# Note: .md is included because markdown docs are where hostnames/IPs (e.g.
# comparison notes, deployment plans) most often leak — see COMPARISON.md.
SCAN_EXTS = {".yml", ".yaml", ".sh", ".py", ".md", ".env", ".env.example", ".env.demo"}


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


def scan_file(filepath: Path) -> list[tuple[int, str, str]]:
    """Scan a file for secrets. Returns list of (line_number, pattern_name, line)."""
    findings = []
    try:
        with open(filepath, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f, 1):
                # Skip comments
                stripped = line.lstrip()
                if stripped.startswith("#"):
                    continue

                # Check for CHANGEME_ in compose files (not .env.example)
                # But skip CHANGEME_ when it's a default value: ${VAR:-CHANGEME_...}
                if filepath.suffix in (".yml", ".yaml"):
                    # Remove ${VAR:-CHANGEME_...} patterns before checking
                    cleaned = re.sub(r'\$\{[^}]*:-CHANGEME_[^}]*\}', '', line)
                    if CHANGEME_PATTERN.search(cleaned):
                        findings.append((i, "CHANGEME in compose file", line.rstrip()))

                # Check for secret patterns
                for pattern, name in SECRET_PATTERNS:
                    if re.search(pattern, line, re.IGNORECASE):
                        # Exclude env var references like ${VAR}
                        if "${" not in line and "CHANGEME" not in line:
                            findings.append((i, name, line.rstrip()))

                # Check for internal network ranges / hostnames (never ship)
                for pattern, name in INTERNAL_PATTERNS:
                    if re.search(pattern, line):
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

    for item in SCAN_DIRS:
        path = scan_root / item
        if path.is_file() and path.suffix in SCAN_EXTS:
            findings = scan_file(path)
            files_scanned += 1
            if findings:
                for line_no, name, line in findings:
                    total_findings += 1
                    # Only flag CHANGEME in compose files as error, not .env.example
                    if "CHANGEME" in name and path.name == ".env.example":
                        continue
                    result.fail(f"{path.name}:{line_no} — {name}: {line.strip()}")
            else:
                result.ok(f"{path.name} clean")
        elif path.is_dir():
            for ext in SCAN_EXTS:
                for fp in path.rglob(f"*{ext}"):
                    if ".git" in fp.parts:
                        continue
                    # git submodules carry THEIR OWN tree (e.g. mailcow's
                    # upstream source with test-fixture strings) — their
                    # licensing/security review is the upstream's job, not
                    # this repo's secret scan
                    if _in_submodule(fp):
                        continue
                    findings = scan_file(fp)
                    files_scanned += 1
                    if findings:
                        for line_no, name, line in findings:
                            total_findings += 1
                            if "CHANGEME" in name and fp.name == ".env.example":
                                continue
                            result.fail(f"{fp.relative_to(scan_root)}:{line_no} — {name}: {line.strip()}")
                    else:
                        result.ok(f"{fp.relative_to(scan_root)} clean")

    # Scan .env.example separately (CHANGEME_ is OK here)
    env_example = scan_root / ".env.example"
    if env_example.exists():
        files_scanned += 1
        result.ok(f"{env_example.name} (CHANGEME_ placeholders OK)")

    result.info(f"Scanned {files_scanned} files, {total_findings} findings")

    return result.summary()


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
