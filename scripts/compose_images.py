#!/usr/bin/env python3
"""
compose_images.py — canonical image inventory for the openSME compose tree.

Emits one TSV row per service image:
    service<TAB>image<TAB>digest<TAB>source

- `image` is the compose-file default image reference (environment defaults
  are expanded, not the caller's environment).
- `digest` is a sha256 digest when the reference contains one, or when a
  local image is available and `--resolve-local` is used; otherwise "-".
- `source` is `local-build`, `owned`, or `upstream`.

Options:
    --live              fail when a running container's image is not in the
                        canonical inventory
    --resolve-local     ask docker for local image digests
    --json              emit JSON instead of TSV

The script never edits compose files.
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("compose_images.py: PyYAML is required (pip install pyyaml)")

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_GLOBS = [
    "docker-compose.yml",
    "idm/*.yml",
    "opencloud/*.yml",
    "mail/*.yml",
    "services/*.yml",
    "monitoring/*.yml",
    "profiles/*.yml",
]

VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(:-([^}]*))?\}")


class ComposeLoader(yaml.SafeLoader):
    pass

def _construct_reset(loader, node):
    return []

def _construct_override(loader, node):
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    return loader.construct_scalar(node)

ComposeLoader.add_constructor("!reset", _construct_reset)
ComposeLoader.add_constructor("!override", _construct_override)


def expand_defaults(value: str) -> str:
    """Expand compose `${VAR:-default}` slices using the default, not env."""
    while True:
        m = VAR_RE.search(value)
        if not m:
            return value
        default = m.group(3) if m.group(2) else ""
        value = value[:m.start()] + default + value[m.end():]


def normalize_image(value: str) -> str:
    return expand_defaults(str(value).strip().strip('"').strip("'"))


def source_for(service: dict, image: str) -> str:
    if service.get("build"):
        return "local-build"
    if image.startswith("registry.openedusuite.org/openedusuite-nix/") \
       or image.startswith("ghcr.io/tobias-weiss-ai-xr/"):
        return "owned"
    return "upstream"


def image_digest(image: str, resolve_local: bool) -> str:
    if "@sha256:" in image:
        return image.split("@sha256:", 1)[1]
    if not resolve_local:
        return "-"
    try:
        out = subprocess.check_output(
            ["docker", "image", "inspect", "--format", "{{json .RepoDigests}}", image],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
        ).strip()
        values = json.loads(out) if out else []
        for ref in values:
            if "@sha256:" in ref:
                return ref.split("@sha256:", 1)[1]
    except Exception:
        pass
    return "-"


def collect(resolve_local: bool = False) -> list[dict]:
    rows: list[dict] = []
    seen = set()
    for pattern in COMPOSE_GLOBS:
        for path in sorted(ROOT.glob(pattern)):
            try:
                doc = yaml.load(path.read_text(), Loader=ComposeLoader)
            except Exception as exc:
                print(f"compose_images: {path}: unparseable ({exc})", file=sys.stderr)
                continue
            services = (doc or {}).get("services") or {}
            if not isinstance(services, dict):
                continue
            for service_name, service in services.items():
                if not isinstance(service, dict) or not service.get("image"):
                    continue
                image = normalize_image(service["image"])
                if not image:
                    continue
                key = (service_name, image)
                if key in seen:
                    continue
                seen.add(key)
                rows.append({
                    "service": service_name,
                    "image": image,
                    "digest": image_digest(image, resolve_local),
                    "source": source_for(service, image),
                    "file": str(path.relative_to(ROOT)),
                })
    rows.sort(key=lambda r: (r["service"], r["image"]))
    return rows


def live_images() -> list[str]:
    try:
        out = subprocess.check_output(
            ["docker", "ps", "--format", "{{.Image}}"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=15,
        )
    except Exception:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def strip_digest(image: str) -> str:
    return image.split("@sha256:", 1)[0]


def live_gate(rows: list[dict]) -> int:
    canonical = {strip_digest(r["image"]) for r in rows}
    live = live_images()
    if not live:
        print("live gate: no running containers; canonical inventory only")
        return 0
    missing = sorted({strip_digest(i) for i in live if strip_digest(i) not in canonical})
    if missing:
        print("live gate: running images not in canonical inventory:", file=sys.stderr)
        for image in missing:
            print(f"  ✗ {image}", file=sys.stderr)
        return 1
    print(f"live gate: {len(live)} running container image(s) all canonical")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="canonical compose image inventory")
    parser.add_argument("--live", action="store_true", help="check running images")
    parser.add_argument("--resolve-local", action="store_true",
                        help="ask docker for local sha256 digests")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args()

    rows = collect(resolve_local=args.resolve_local)
    if not rows:
        print("compose_images: no images found", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps({"schema": 1, "images": rows}, indent=2))
    else:
        print("service\timage\tdigest\tsource")
        for r in rows:
            print(f'{r["service"]}\t{r["image"]}\t{r["digest"]}\t{r["source"]}')

    if args.live:
        return live_gate(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
