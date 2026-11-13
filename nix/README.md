<!--
SPDX-FileCopyrightText: 2026 Tobias Weiss
SPDX-License-Identifier: Apache-2.0
-->

# nix/ — the opencloud image flake

Builds the OpenCloud container image with Nix (`pkgs.dockerTools`), pinned to
the newest STABLE upstream release (**v9.1.0** — prereleases like `9.1.1-rc.2`
are never used, and the `opencloudeu/opencloud-rolling:*` tags are exactly the
kind of moving target this flake exists to replace).

## What `.#opencloud` builds

`opencloud.nix` is hostPlatform-aware: it downloads the upstream **release
binary** for the building machine's architecture (amd64 asset on `x86_64-linux`,
arm64 asset on `aarch64-linux`) and verifies it against the upstream release
`.sha256` sidecar. The same expression natively builds each architecture.

Image layout (mirrors what `opencloud/opencloud.yml` expects from the upstream
image):

| Piece | Where | Why |
|---|---|---|
| `opencloud` binary | `/usr/bin/opencloud` (mode 0555) | upstream image ENTRYPOINT |
| CA bundle | `/etc/ssl/certs/ca-bundle.crt` | ocis validates the IdP over HTTPS |
| `curl` | `/bin/curl` | compose healthcheck (`/health/ready`) |
| `/bin/sh` + applets | busybox symlinks | host-mounted `opencloud-entrypoint.sh` |
| `/etc/passwd`, `/etc/group` | uid/gid 1000 `opencloud` | non-root default |
| writable dirs | `/etc/opencloud`, `/var/lib/opencloud` | config + state (compose mounts named volumes over them) |

Default user is `1000:1000`; the compose stack overrides it with
`user: "0:0"` as before. Entrypoint/Cmd mirror upstream:
`["/usr/bin/opencloud"]` + `["server"]`.

## Build + load

```sh
. /home/weiss/.nix-profile/etc/profile.d/nix.sh   # Nix 2.35.6, single-user
cd nix/                                           # this flake lives in nix/
nix build .#opencloud
docker load < result
docker tag opencloud:9.1.0-nix-flake \
  ghcr.io/tobias-weiss-ai-xr/opencloud:9.1.0-nix-flake
```

Commit everything before building — flakes only see git-tracked files.

## Image tags

- `opencloud:9.1.0-nix-flake` — as loaded by `docker load`
- `ghcr.io/tobias-weiss-ai-xr/opencloud:9.1.0-nix-flake` — local tag for the
  flake-built image (this image is **not pushed**; the *source-built*
  9.1.0 image from the parallel workstream lives under the separate
  `:9.1.0` tag and at `registry.openedusuite.org/openedusuite-nix/opencloud:9.1.0-nix`,
  and must not be overwritten)

## Deployment status (2026-11-14)

The deploy step of this task was **skipped per instructions**: the mission
requires `docker pull ghcr.io/tobias-weiss-ai-xr/opencloud:9.1.0` (the
source-built image, image ID `sha256:19eec92c…`) and forbids substituting
anything else into the deployed slot. That pull fails with
`unauthorized` (the ghcr repo is private; no credentials on this host), and
the only anonymously reachable copy — `registry.openedusuite.org/openedusuite-nix/opencloud:9.1.0-nix`
— has a different content digest (`sha256:69bcf104…` config /
`sha256:5c4170cf…` manifest), so it cannot be verified as the parallel
workstream's image. The live stack therefore stays on
`opencloudeu/opencloud-rolling:8.0.1` (healthy) until the orchestrator
reconciles the image line.
