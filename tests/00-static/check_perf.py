#!/usr/bin/env python3
"""
tests/00-static/check_perf.py — Layer 0 performance & efficiency invariants.

Enforces (specs/perf-budgets, specs/perf-ops):
  1. Every service in every compose file declares deploy.resources.limits
     (cpus AND memory).
  2. Every service declares a logging cap (json-file with max-size + max-file).
  3. Every long-running service declares a healthcheck.
  4. Per-tier reservation budgets:
       soho   <= 6GB   (8G host)
       small  <= 20GB  (24G host)
       medium <= 40GB  (48G host)
  5. .env.example documents *_IMAGE_DIGEST pinning vars for the pinned set.

Usage:
    python3 tests/00-static/check_perf.py              # check only
    python3 tests/00-static/check_perf.py --write-baselines  # + docs/perf/baselines.md

Exit codes:
    0 = all checks passed
    1 = violations found
"""

import sys
import yaml
import re
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result, ROOT

GB = 1024


def mb(v):
    if v is None:
        return 0
    s = str(v).strip().lower()
    m = re.match(r"^([0-9.]+)\s*(b|k|kb|m|mb|g|gb)?$", s)
    if not m:
        return 0
    val = float(m.group(1))
    unit = m.group(2) or "b"
    mul = {"b": 1 / 1048576, "k": 1 / 1024, "kb": 1 / 1024,
          "m": 1, "mb": 1, "g": 1024, "gb": 1024}[unit]
    return val * mul


def cp(v):
    try:
        return float(str(v))
    except Exception:
        return 0.0


def parse(path: str) -> dict:
    try:
        return yaml.safe_load(open(ROOT / path)) or {}
    except Exception as e:
        print(f"  !! parse error {path}: {e}")
        return {}


def compose_merge(a: dict, b: dict) -> dict:
    """Compose-style overlay merge: dicts deep-merge, lists/scalars override."""
    out = dict(a)
    for k, v in b.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = compose_merge(out[k], v)
        else:
            out[k] = v
    return out


# ---------------------------------------------------------------------------
# Tier model (mirrors README tier composition; budgets assume profiles ON)
# ---------------------------------------------------------------------------
TIERS = {
    "soho": {
        "files": ["docker-compose.yml", "idm/zitadel.yml", "profiles/soho.yml"],
        "budget_gb": 6,
        "host": "4c/8G",
    },
    "small": {
        "files": [
            "docker-compose.yml", "idm/zitadel.yml",
            "opencloud/opencloud.yml",
            "services/invoice-ninja.yml", "services/paperless.yml",
            "profiles/small.yml",
        ],
        "budget_gb": 20,
        "host": "8c/24G",
    },
    "medium": {
        "files": [
            "docker-compose.yml", "idm/zitadel.yml",
            "opencloud/opencloud.yml", "opencloud/minio.yml",
            "mail/stalwart.yml", "mail/sogo.yml",
            "services/invoice-ninja.yml", "services/paperless.yml",
            "services/synapse.yml", "services/notes.yml",
            "profiles/medium.yml",
        ],
        "budget_gb": 40,
        "host": "16c/48G",
    },
}
# Final budget target per tier (GB, reservations)
TIER_BUDGETS_GB = {"soho": 6, "small": 20, "medium": 40}

# Images recommended for digest pinning (documented in .env.example)
PINNED_IMAGES = ["traefik", "postgres", "redis", "memcached", "stalwart", "sogo"]

# One-shot / never-run services exempt from the healthcheck invariant
HEALTHCHECK_EXEMPT = {"taskfleet", "collabora"}  # collabora/code 26.04 ships
# no curl/wget/shell — an in-container healthcheck is impossible (see
# opencloud/opencloud.yml); a dead coolwsd surfaces as Traefik 502.


def all_compose_files():
    base = [
        "docker-compose.yml",
        "idm/zitadel.yml", "idm/casdoor.yml",
        "opencloud/opencloud.yml", "opencloud/minio.yml",
        "mail/stalwart.yml", "mail/sogo.yml",
        "services/invoice-ninja.yml", "services/paperless.yml",
        "services/cryptpad.yml", "services/synapse.yml",
        "services/element.yml", "services/notes.yml",
        "services/ticketing.yml", "services/cms.yml",
        "services/store.yml", "services/ai.yml",
        "services/camunda.yml",
        "monitoring/dev-agent.yml", "monitoring/predictive-agent.yml",
        "monitoring/ollama.yml", "monitoring/taskfleet.yml",
    ]
    profiles = [
        "profiles/soho.yml", "profiles/small.yml", "profiles/medium.yml",
        "profiles/demo.dev.yml", "profiles/demo.live.yml",
        "profiles/demo.coexist.yml", "profiles/system-traefik.yml",
    ]
    return base, profiles


def check_invariants(result: Result):
    # Invariants must run on MERGED models (compose-merge semantics), not on
    # individual files: profiles override only subsets and inherit the rest.
    base, profiles = all_compose_files()
    models = {}
    # 1. base model (all base compose files merged)
    merged = {}
    for rel in base:
        merged = compose_merge(merged, parse(rel))
    models["base"] = merged.get("services") or {}
    # 2. each tier render
    for tier in TIERS:
        models[tier] = render_tier(tier, TIERS[tier]["files"])
    # 3. each demo / system profile merged onto base
    for prof in [p for p in profiles if p.startswith("profiles/")]:
        if prof in [f"profiles/{t}.yml" for t in TIERS]:
            continue
        pm = compose_merge(dict(merged), parse(prof))
        models[prof] = pm.get("services") or {}

    seen = 0
    for model_name, svcs in models.items():
        for name, svc in svcs.items():
            if "donotstart" in (svc.get("profiles") or []):
                continue
            seen += 1
            svc = svc or {}
            tag = f"{model_name} :: {name}"
            # --- 1. resource limits ---
            deploy = svc.get("deploy") or {}
            limits = (deploy.get("resources") or {}).get("limits") or {}
            if not (limits.get("cpus") and limits.get("memory")):
                result.fail(f"{tag}: missing deploy.resources.limits "
                            f"(cpus+memory)")
            # --- 2. logging cap ---
            log = svc.get("logging") or {}
            opts = log.get("options") or {}
            driver = log.get("driver", "")
            if not (driver and opts.get("max-size") and opts.get("max-file")):
                result.fail(f"{tag}: no logging cap "
                            f"(driver={driver!r} max-size={opts.get('max-size')!r})")
            # --- 3. healthcheck for long-running services ---
            if name in HEALTHCHECK_EXEMPT:
                continue
            restart = (svc.get("restart") or "no").lower()
            one_shot = restart == "no" or restart == "on-failure"
            if one_shot:
                continue
            hc = svc.get("healthcheck")
            has_hc = bool(hc) and (len(hc) if isinstance(hc, (list, str)) else
                                   bool(hc.get("test")))
            if not has_hc:
                result.fail(f"{tag}: long-running service has no healthcheck")
    result.info(f"Checked merged models ({len(models)} models, {seen} services)")
    return seen


def render_tier(name: str, files: list) -> dict:
    merged = {}
    for f in files:
        merged = compose_merge(merged, parse(f))
    return merged.get("services") or {}


def check_budgets(result: Result) -> dict:
    tier_services = {}
    for tier, spec in TIERS.items():
        svcs = render_tier(tier, spec["files"])
        running = {n: s for n, s in svcs.items()
                   if "donotstart" not in (s.get("profiles") or [])}
        total_res = sum(mb((((s.get("deploy") or {}).get("resources") or {})
                            .get("reservations") or {}).get("memory"))
                        for s in running.values())
        total_lim = sum(mb((((s.get("deploy") or {}).get("resources") or {})
                            .get("limits") or {}).get("memory"))
                        for s in running.values())
        budget = TIER_BUDGETS_GB[tier] * GB
        gb_res = total_res / GB
        gb_lim = total_lim / GB
        status = "OK" if gb_res <= budget else "FAIL"
        if gb_res <= budget:
            result.ok(f"{tier}: Σreservations {gb_res:.2f}G ≤ "
                      f"{TIER_BUDGETS_GB[tier]}G budget ({status})")
        else:
            result.fail(f"{tier}: Σreservations {gb_res:.2f}G EXCEEDS "
                        f"{TIER_BUDGETS_GB[tier]}G budget")
        tier_services[tier] = {
            "running": running, "res_gb": gb_res, "lim_gb": gb_lim,
        }
    return tier_services


def check_pins(result: Result):
    env = ROOT / ".env.example"
    if not env.exists():
        result.fail(".env.example missing — cannot verify image pin docs")
        return
    txt = env.read_text()
    missing = []
    for img in PINNED_IMAGES:
        var = f"{img.upper().replace('-', '_')}_IMAGE_DIGEST"
        if var not in txt:
            missing.append(var)
    if missing:
        result.fail(f".env.example missing digest-pin vars: {', '.join(missing)}")
    else:
        result.ok(f".env.example documents digest-pin vars for all "
                  f"{len(PINNED_IMAGES)} pinned images")


def write_baselines(tier_services: dict):
    out_dir = ROOT / "docs" / "perf"
    out_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        "# openSME — Resource Baselines",
        "",
        (f"_Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_ "
        "by `tests/00-static/check_perf.py --write-baselines`."),
        "",
        ("Merged `docker compose` models (pure-YAML, no engine needed). "
        "Budgets are **reservation** sums (what the scheduler guarantees); "
        "limits cap spikes. Profile-gated overlays are included per tier."),
        "",
        "| Tier | Host | Σlimits | Σreservations | Budget (res) | Status |",
        "|------|------|---------|---------------|--------------|--------|",
    ]
    for tier, spec in TIERS.items():
        ts = tier_services[tier]
        status = "✓" if ts["res_gb"] <= TIER_BUDGETS_GB[tier] else "✗"
        lines.append(f"| {tier} | {spec['host']} | {ts['lim_gb']:.2f}G | "
                     f"{ts['res_gb']:.2f}G | ≤ {TIER_BUDGETS_GB[tier]}G | {status} |")
    lines += ["", "## Per-tier services", ""]
    for tier in TIERS:
        ts = tier_services[tier]
        lines.append(f"### {tier}")
        lines.append("")
        lines.append("| Service | Image | Limits | Reservations |")
        lines.append("|---------|-------|--------|--------------|")
        for name, s in sorted(ts["running"].items()):
            img = s.get("image", "(build)") or "(build)"
            r = (s.get("deploy") or {}).get("resources") or {}
            lim = r.get("limits") or {}
            res = r.get("reservations") or {}
            lines.append(f"| {name} | `{img}` | "
                         f"{lim.get('cpus','-')}c/{lim.get('memory','-')} | "
                         f"{res.get('cpus','-')}c/{res.get('memory','-')} |")
        lines.append("")
    (out_dir / "baselines.md").write_text("\n".join(lines))
    print("  Wrote docs/perf/baselines.md")


def main():
    result = Result("perf-check")
    result.header("Layer 0: performance & efficiency invariants")
    check_invariants(result)
    tier_services = check_budgets(result)
    check_pins(result)
    if "--write-baselines" in sys.argv:
        write_baselines(tier_services)
    return result.summary()


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
