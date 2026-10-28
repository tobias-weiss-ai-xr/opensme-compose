#!/usr/bin/env python3
"""
Generate Prometheus blackbox scrape configs for an openSME deployment.

Reads the declarative target inventory (monitoring/blackbox/blueprint.yml)
and resolves every `{{ ENV_VAR }}` from the process environment plus an
optional --env-file (the stack's .env — same variables the compose chain
uses, so monitoring always matches the deployed topology).

Modes
-----
  --print        print the resolved probe list (one URL per line) and exit.
  --mode full    emit a self-contained standalone prometheus.yml
                 (scrapes itself + the blackbox exporter).
  --mode fragment  emit ONLY the openSME blackbox scrape_configs, ready to
                 append to an existing Prometheus (e.g. the homelab central
                 instance).

Environment
-----------
  OPENSME_DOMAIN           apex/marketing domain (required)
  MONITOR_SUBDOMAIN_BASE   "" or "home." — prefix for `sub:` entries
  PORTAL_DOMAIN, WEBMAIL_DOMAIN, ...   per-service overrides (see blueprint)
  BLACKBOX_ADDR            address of the blackbox exporter as seen by
                           Prometheus (default "blackbox:9115" — the
                           standalone compose network; set e.g.
                           192.168.42.20:9115 for central scraping)
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

import yaml

ENV_RE = re.compile(r"\{\{\s*([A-Z][A-Z0-9_]*)\s*\}\}")

# Intentionally-empty-capable vars (ENV_RE matches, but "" is a valid value).
EMPTY_ALLOWED = {"MONITOR_SUBDOMAIN_BASE"}


def expected_vars(bp: dict) -> set[str]:
    """The set of env vars the blueprint may legitimately read — collected
    from every `{{ VAR }}` placeholder and `env:` hint in blueprint.yml.
    Process env is filtered to ONLY these names, so an unrelated host var
    (e.g. a stray MAIL_DOMAIN from another project) can never hijack a
    target. --env-file values may still set anything."""
    names: set[str] = set()

    def scan(node: object) -> None:
        if isinstance(node, dict):
            for key, val in node.items():
                if key == "env" and isinstance(val, str):
                    names.add(val)
                else:
                    scan(val)
        elif isinstance(node, list):
            for item in node:
                scan(item)
        elif isinstance(node, str):
            names.update(ENV_RE.findall(node))

    scan(bp)
    names.update({"OPENSME_DOMAIN", "BLACKBOX_ADDR"})
    return names


def load_env_file(path: str | None) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path:
        return env
    for ln in Path(path).read_text().splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#") or "=" not in ln:
            continue
        key, _, val = ln.partition("=")
        env[key.strip()] = val.strip().strip('"').strip("'")
    return env


def process_env(blueprint_vars: set[str]) -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k in blueprint_vars}


def resolve(text: str, env: dict[str, str]) -> str:
    def sub(match: re.Match) -> str:
        name = match.group(1)
        if name in env:
            return env[name]
        if name in EMPTY_ALLOWED:
            return ""
        raise SystemExit(
            f"UNRESOLVED: {{{{{name}}}}} — set '{name}' in the environment "
            f"or in --env-file (blueprint: openSME blackbox targets)"
        )

    return ENV_RE.sub(sub, text)


def fqdn_for(target: dict, env: dict[str, str], apex: str, sub_base: str) -> str:
    if target.get("full"):
        return resolve(target["full"], env)
    sub = target.get("sub", "")
    if not sub:
        raise SystemExit(f"blueprint target '{target.get('name')}' needs 'full' or 'sub'")
    override = target.get("env")
    if override and env.get(override):
        return env[override]
    return f"{sub}.{sub_base}{apex}"


def render_jobs(bp: dict, env: dict[str, str]) -> list[dict]:
    scheme = resolve(bp.get("scheme", "https"), env)
    sub_base = resolve(bp.get("sub_base", ""), env)
    apex = env.get("OPENSME_DOMAIN")
    if not apex:
        raise SystemExit("OPENSME_DOMAIN must be set")

    by_module: dict[str, list[str]] = {}
    for target in bp.get("targets", []):
        if not target.get("enabled", True):
            continue
        module = target.get("module", "http_avail")
        fqdn = fqdn_for(target, env, apex, sub_base)
        path = target.get("path", "/")
        by_module.setdefault(module, []).append(f"{scheme}://{fqdn}{path}")

    relabel = [
        {"source_labels": ["__address__"], "target_label": "__param_target"},
        {"source_labels": ["__param_target"], "target_label": "instance"},
        {"target_label": "__address__", "replacement": env.get("BLACKBOX_ADDR", "blackbox:9115")},
    ]
    jobs: list[dict] = []
    for module, urls in sorted(by_module.items()):
        job = "opensme-blackbox" + ("" if module == "http_avail" else f"-{module}")
        jobs.append({
            "job_name": job,
            "metrics_path": "/probe",
            "scrape_interval": "60s",
            "params": {"module": [module]},
            "static_configs": [{"targets": urls, "labels": {"group": "opensme"}}],
            "relabel_configs": relabel,
        })
    tcps = bp.get("tcp", [])
    if tcps:
        tcp_targets = []
        for t in tcps:
            fqdn = fqdn_for(t, env, apex, sub_base)
            tcp_targets.append(f"{fqdn}:{t['port']}")
        jobs.append({
            "job_name": "opensme-blackbox-tcp",
            "metrics_path": "/probe",
            "scrape_interval": "60s",
            "params": {"module": ["tcp_connect"]},
            "static_configs": [{"targets": tcp_targets, "labels": {"group": "opensme"}}],
            "relabel_configs": relabel,
        })
    return jobs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--env-file", help="stack .env (KEY=VALUE lines) to resolve {{ VARS }}")
    ap.add_argument("--no-process-env", action="store_true",
                    help="resolve ONLY from --env-file (ignore process env; "
                         "guards against stray *_DOMAIN vars in the host session)")
    ap.add_argument("--blueprint", default=str(Path(__file__).with_name("blueprint.yml")))
    ap.add_argument("--mode", choices=["full", "fragment"], default="full")
    ap.add_argument("-o", "--out", help="write YAML to file instead of stdout")
    ap.add_argument("--print", action="store_true", help="print the resolved probe URLs and exit")
    args = ap.parse_args()

    bp = yaml.safe_load(Path(args.blueprint).read_text())
    env = {} if args.no_process_env else process_env(expected_vars(bp))
    env.update(load_env_file(args.env_file))

    if args.print:
        scheme = resolve(bp.get("scheme", "https"), env)
        sub_base = resolve(bp.get("sub_base", ""), env)
        apex = env.get("OPENSME_DOMAIN") or ""
        if not apex:
            raise SystemExit("OPENSME_DOMAIN must be set")
        for target in bp.get("targets", []):
            if not target.get("enabled", True):
                continue
            fqdn = fqdn_for(target, env, apex, sub_base)
            print(f"[{target.get('module', 'http_avail')}] {scheme}://{fqdn}{target.get('path', '/')}")
        for t in bp.get("tcp", []):
            fqdn = fqdn_for(t, env, apex, sub_base)
            print(f"[tcp_connect] {fqdn}:{t['port']}")
        return 0

    jobs = render_jobs(bp, env)
    if args.mode == "full":
        addr = env.get("BLACKBOX_ADDR", "blackbox:9115")
        conf = {
            "global": {"scrape_interval": "30s", "evaluation_interval": "30s"},
            "rule_files": ["/etc/prometheus/rules/*.yml"],
            "scrape_configs": [
                {"job_name": "prometheus", "static_configs": [{"targets": ["localhost:9090"]}]},
                {"job_name": "blackbox-exporter", "static_configs": [{"targets": [addr]}]},
                *jobs,
            ],
        }
    else:
        conf = {"scrape_configs": jobs}

    text = yaml.safe_dump(conf, sort_keys=False)
    if args.out:
        Path(args.out).write_text(text)
        print(f"wrote {args.out}")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
