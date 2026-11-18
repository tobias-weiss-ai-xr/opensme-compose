#!/usr/bin/env python3
"""
tests/00-static/check_monitoring.py — Layer 0 validation of the suite
monitoring contract (R16).

Two live incidents on 2026-11-18 went UNDETECTED because the only host-side
watchdog (/opt/opensme-host-services-watch.sh) merely relaunches HOST-side
portal ports and nothing watched the suite itself:

  1. Opaque-token regression (R15): the zitadel-client.sh registrar
     self-clobbered accessTokenType via split oidc_config PUTs. openCloud
     then failed every login with
     'failed to verify access token: token contains an invalid number of
     segments' — for DAYS, with zero alerts. A single grep of the opencloud
     proxy logs or a check of the Zitadel event store would have caught it.
  2. Latency regression: opencloud PROPFIND /dav/spaces requests ran 12-14s
     and /graph photo fetches 10s+. The external uptime prober only checks
     status codes — durations never surfaced.

Additionally the alert channel itself was broken: exim4 was configured as
'local', so `mail -s ...` (AIDE alerts!) never left the host
('Mailing to remote domains not supported').

R16 pins the monitoring contract statically:

  M1 — scripts/suite-monitor.sh exists (the cron-driven live monitor).
  M2 — it verifies OIDC access-token type via the Zitadel admin event store
       (accessTokenType must be JWT/1 for every JWKS-validating backend).
  M3 — it scans opencloud logs for the opaque-token error signature.
  M4 — it enforces request latency bounds (edge probe durations + opencloud
       access-log 'duration=' values).
  M5 — it checks core container health (docker ps healthy states).
  M6 — it alerts via mail with an anti-spam cooldown state file.
  M7 — the static suite runner (run.sh) actually executes the OIDC contract
       checks (check_oidc) — the R1-R15 guard existed but was never wired
       into `make test-static`.
  M8 — the deployment docs (docs/SSO.md or scripts header) carry the cron
       line so a fresh host gets the monitor wired.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import re

from conftest import ROOT, Result


def main() -> bool:
    result = Result("suite-monitoring")

    monitor = ROOT / "scripts" / "suite-monitor.sh"

    # M1 — the monitor script exists
    if monitor.exists():
        text = monitor.read_text()
        result.ok(f"scripts/suite-monitor.sh exists ({len(text.splitlines())} lines)")
    else:
        result.fail(
            "scripts/suite-monitor.sh missing — the 2026-11-18 openCloud "
            "opaque-token outage ran for days with no detection because "
            "nothing watched the suite (R15 incident)"
        )
        print(result.summary())
        return result.failed == 0

    # M2 — OIDC token-type guard via Zitadel event store
    if "events/_search" in text and "accessTokenType" in text:
        result.ok("M2: OIDC access-token type verified via Zitadel event store")
    else:
        result.fail(
            "suite-monitor.sh lacks the accessTokenType event-store guard "
            "(M2) — the exact R15 failure mode (JWT flipped back to OPAQUE "
            "by a follow-up oidc_config PUT) would go unnoticed again"
        )

    # M3 — opaque-token log signature scan
    if ("invalid number of segments" in text
            or "failed to verify access token" in text):
        result.ok("M3: opencloud opaque-token log signature scanned")
    else:
        result.fail(
            "suite-monitor.sh does not scan opencloud logs for the "
            "'failed to verify access token' signature (M3) — the R15 "
            "incident emitted exactly this error on every login"
        )

    # M4 — latency guard (edge probes + traefik edge-log durations)
    has_edge = "time_total" in text or "%{time_total}" in text
    has_edge_log = "docker logs traefik" in text
    if has_edge and has_edge_log:
        result.ok("M4: latency guarded (edge probe timing + traefik edge-log durations)")
    else:
        result.fail(
            "suite-monitor.sh lacks latency guards (M4) — PROPFIND /dav "
            "requests ran 12-14s on 2026-11-18 with no signal (external "
            "prober only checks status codes)"
        )

    # M9 — request-latency signal must come from the TRAEFIK EDGE LOG,
    # never from opencloud's own access-log durations.
    #
    # Why: the opencloud proxy accesslog (accesslog.go:40) reports
    # duration ≈ AGE OF THE UPSTREAM TCP CONNECTION (traefik keep-alive
    # pool), not request latency. Proven 2026-11-18 21:07Z: 30 parallel
    # curl requests all answered in 0.03-0.35s (client) were logged by
    # opencloud with duration=7.4-53.6s; traefik showed 0 entries ≥10s in
    # every window where opencloud claimed 15-70s requests; debug traces
    # show all internal processing in ms at the completion second.
    # Every "slow burst" (17:17/17:26/17:47 + the C5 alerts built on
    # them) was this artifact — there is no server-side cold start.
    uses_opencloud_duration = re.search(
        r"docker logs opensme-opencloud.*duration=", text, re.S
    )
    parses_edge_ms = bool(re.search(r"docker logs traefik", text)) and "ms" in text
    if uses_opencloud_duration:
        result.fail(
            "suite-monitor.sh measures request latency from the opencloud "
            "access-log duration= field (M9) — that field reports the AGE "
            "OF THE UPSTREAM TCP CONNECTION (traefik keep-alive pool), not "
            "request latency (2026-11-18 proof: client 0.03-0.35s vs "
            "logged 7.4-53.6s). Use the traefik edge log instead"
        )
    elif parses_edge_ms:
        result.ok("M9: request-latency signal = traefik edge log (not the "
                  "opencloud connection-age artifact)")
    else:
        result.fail(
            "suite-monitor.sh does not parse traefik edge-log request "
            "durations (M9) — the edge log is the only truthful client-"
            "perceived latency signal"
        )

    # M5 — container health
    if "docker ps" in text and ("health" in text or "healthy" in text):
        result.ok("M5: core container health checked")
    else:
        result.fail(
            "suite-monitor.sh lacks a container-health check (M5) — "
            "unhealthy/restarting core containers must alert"
        )

    # M6 — alerting with cooldown
    if "mail " in text and "STATE_DIR" in text:
        result.ok("M6: mail alerting with cooldown state present")
    else:
        result.fail(
            "suite-monitor.sh lacks mail alerting + cooldown state (M6) — "
            "note: exim on the host must be an 'internet' site, the 2026-11-18 "
            "'local' config silently swallowed AIDE alerts too"
        )

    # M7 — run.sh executes the OIDC contract checks
    runsh = (ROOT / "tests" / "00-static" / "run.sh").read_text()
    if "check_oidc" in runsh:
        result.ok("M7: run.sh executes check_oidc (R1-R15 wired into make test-static)")
    else:
        result.fail(
            "tests/00-static/run.sh never runs check_oidc.py — the R1-R15 "
            "OIDC contract guards were disconnected from the default suite"
        )

    # M8 — cron wiring documented
    if ("crontab" in text or "cron" in text.lower()) and "suite-monitor" in text:
        result.ok("M8: cron wiring documented in the script header")
    else:
        result.fail(
            "suite-monitor.sh header lacks the cron install line (M8) — "
            "the monitor must be schedulable on a fresh host"
        )

    return result.failed == 0


if __name__ == "__main__":
    ok = main()
    sys.exit(0 if ok else 1)
