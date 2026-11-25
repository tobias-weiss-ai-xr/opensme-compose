#!/usr/bin/env python3
"""Layer 0 pins for opt-in WebDAV archival to OpenCloud (A1/B1/B2).

A1 — the shared config pieces exist: the S3 anchor include and the Rust
     WebDAV client crate, plus the limesurvey rclone mirror pattern.
B1 — Invoice Ninja has an opt-in `invoice` profile sidecar that mirrors the
     read-only public volume to `cloud:Rechnungen/InvoiceNinja`.
B2 — Paperless has an opt-in `paperless` profile sidecar that mirrors the
     read-only export volume to `cloud:Dokumente/Paperless`.
Both sidecars idle when the WebDAV credentials are missing: archival must
never be enabled implicitly by the mere presence of the profile.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result, ROOT

INVOICE = ROOT / "services" / "invoice-ninja.yml"
PAPERLESS = ROOT / "services" / "paperless.yml"
S3_INCLUDES = ROOT / "docker-compose.s3-includes.yml"
WEBDAV_CRATE = ROOT / "services" / "rust-webdav-client" / "Cargo.toml"
SURVEYS = ROOT / "services" / "limesurvey.yml"


def section(text: str, name: str) -> str:
    match = re.search(rf"^  {re.escape(name)}:\n", text, re.MULTILINE)
    if match is None:
        return ""
    rest = text[match.end():]
    following = re.search(r"^  \S", rest, re.MULTILINE)
    return rest[:following.start()] if following else rest


def main():
    result = Result("webdav-archive")
    result.header("Layer 0: WebDAV archival contract validation")
    invoice = INVOICE.read_text() if INVOICE.exists() else ""
    paperless = PAPERLESS.read_text() if PAPERLESS.exists() else ""
    surveys = SURVEYS.read_text() if SURVEYS.exists() else ""

    def check(cond, msg):
        (result.ok if cond else result.fail)(msg)

    # A1 — shared building blocks are present.
    check(S3_INCLUDES.exists() and "x-s3-defaults: &s3-defaults" in S3_INCLUDES.read_text(),
          "A1 shared S3 anchor include exists")
    check(WEBDAV_CRATE.exists() and "webdav" in WEBDAV_CRATE.read_text().lower(),
          "A1 shared Rust WebDAV client crate exists")
    check("limesurvey-cloud-sync" in surveys and "rclone copy" in surveys,
          "A1 rclone WebDAV mirror pattern is established")

    for label, source, name, profile, mount, folder in (
        ("B1", invoice, "invoiceninja-cloud-sync", "invoice",
         "invoiceninja-public:/data:ro", "cloud:Rechnungen/InvoiceNinja"),
        ("B2", paperless, "paperless-cloud-sync", "paperless",
         "paperless-export:/data:ro", "cloud:Dokumente/Paperless"),
    ):
        block = section(source, name)
        check(bool(block), f"{label} {name} sidecar is defined")
        check('image: rclone/rclone:${RCLONE_TAG:-latest}' in block,
              f"{label} sidecar uses the standard rclone image")
        check(f'profiles: ["{profile}"]' in block and "restart: unless-stopped" in block,
              f"{label} sidecar is opt-in via the {profile!r} profile")
        check(mount in block,
              f"{label} sidecar mounts {mount} (source data is never writable)")
        check(folder in block and "rclone copy /data" in block,
              f"{label} sidecar mirrors /data to {folder}")
        check('if [ -z "$$CLOUD_WEBDAV_URL" ] || [ -z "$$CLOUD_WEBDAV_PASS" ]' in block
              and "exec sleep infinity" in block,
              f"{label} sidecar idles without WebDAV credentials")
        check(all(t in block for t in (
            "CLOUD_WEBDAV_URL: ${CLOUD_WEBDAV_URL:-}",
            "CLOUD_WEBDAV_USER: ${CLOUD_WEBDAV_USER:-}",
            "CLOUD_WEBDAV_PASS: ${CLOUD_WEBDAV_PASS:-}",
            "rclone obscure",
        )), f"{label} sidecar takes credentials from .env and obscures the password")
        check('healthcheck:' in block and "pidof rclone" in block and "pidof sleep" in block,
              f"{label} sidecar healthcheck covers both sync and idle loops")

    # A1/B1/B2 — backlog reflects the delivered state.
    backlog = (ROOT / "docs" / "OPENCLOUD-CAMPAIGNS-SUMMARY.md").read_text()
    for item in ("A1", "B1", "B2"):
        line = next((line for line in backlog.splitlines() if f"**{item}**:" in line), "")
        check(line.startswith(f"- [x] **{item}**:"),
              f"{item} backlog item is closed with evidence")

    return 0 if result.summary() else 1


if __name__ == "__main__":
    sys.exit(main())
