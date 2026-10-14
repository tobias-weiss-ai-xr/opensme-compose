# Digest-pinned image manifests (operator opt-in)

An operator opt-in way to make openSME deploys byte-for-byte reproducible:
`scripts/pin-images.sh` resolves every `image:` tag in the compose files to
its current sha256 manifest digest and emits `manifests/pinned-images.env`,
an env-file that `docker compose` reads as image overrides.

## What it does

The compose files ship with tags — many of them floating (`:latest`,
`redis:alpine`, `postgres:17`). The same tag can point at a *different*
image tomorrow, so two deploys a day apart are not the same software.
Running the pin script bakes today's digest into a manifest; deploys that
use the manifest can never drift from that exact image.

The script is read-only with respect to the compose files: it never edits
them, it only produces/validates the manifest.

## Usage

```sh
# 1. Generate the manifest (needs `docker` with buildx, or `skopeo`)
scripts/pin-images.sh --generate

# 2. Inspect what got pinned
cat manifests/pinned-images.env

# 3. Validate a manifest at any time (works without docker/network)
scripts/pin-images.sh --check

# 4. Apply it to a deployment
docker compose --env-file manifests/pinned-images.env up -d
#   or, if you prefer shell exports over --env-file:
#   set -a; source manifests/pinned-images.env; set +a
```

The manifest line format is docker-compose's env-override convention: each
line is `VAR=repo@sha256:hex`, e.g. `PAPERLESS_IMAGE=ghcr.io/paperless-ngx/
paperless-ngx@sha256:abc123…`. `--check` verifies every value matches
`NAME=repo@sha256:hex` (comments and blank lines are ignored) and exits 1
on a malformed line; it warns and skips if the manifest does not exist yet.

## How the override is wired

- **Active pins** use variable names the compose files already honour via
  `image: ${VAR:-…}` (e.g. `TRAEFIK_IMAGE`, `POSTGRES_IMAGE`); feeding the
  manifest to compose immediately pins those services.
- **Images without an env-var override** in the compose files (e.g.
  Paperless, Synapse) are emitted as commented lines `# VAR=repo@sha256:…`.
  To activate one, add an override in the compose file
  (`image: ${PAPERLESS_IMAGE:-ghcr.io/paperless-ngx/paperless-ngx:latest}`)
  and uncomment the line — a deliberate, small code change, which is exactly
  why pinning stays operator-opt-in instead of silently changing images.
- Local-build images (no registry image exists) cannot be pinned — the
  script warns and skips them.

## Why digest pinning matters

- **Reproducible deploys**: a pinned digest is immutable, so the same
  manifest deploys the same image, this month or next.
- **Supply-chain hygiene**: an image reference can only be trusted if the
  exact digest is known; a rotated or compromised `:latest` is invisible
  until it runs.
- **Rollback**: with each deploy's manifest committed/snapshotted, you can
  reproduce the exact previous image set instead of approximating it by tag.

Refresh pins deliberately: re-run `--generate` when you *want* new images,
review the diff in `manifests/pinned-images.env`, then redeploy. Generation
is idempotent — the same registry state always produces the same manifest.
