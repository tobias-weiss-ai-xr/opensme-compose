<!--
SPDX-FileCopyrightText: 2026 Tobias Weiss
SPDX-License-Identifier: Apache-2.0
-->

# nix/ — Nix-built stack images

This directory defines four container images with Nix (`pkgs.dockerTools`,
`streamLayeredImage`):

| Package | Image / tag | Source | What it replaces |
|---|---|---|---|
| `opencloud` | `opencloud:9.1.0-nix-flake` | upstream stable release binary | `opencloudeu/opencloud-rolling:*` |
| `vc-room` | `vc-room:1.0.0` | `images/vc-room/` (Flask + vendored LiveKit SDK) | `images/vc-room/Dockerfile` build |
| `portal` | `portal:0.1.0` | `portal/` (Rust, opensme-portal crate) | `portal/Dockerfile` build |
| `calrs` | `calrs:1.6.0-openSME` | upstream `olivierlambert/calrs` v1.6.0 + `images/calrs/patches/0001..0005` | `images/calrs/Dockerfile` build |

## Building from this flake

The **canonical flake for these images is `nix/flake.nix` itself** — a
self-contained flake with `srcRoot = ./..` for the source-backed packages
(`portal/`, `images/vc-room/`). Build **from this directory** (`nix/`),
because the flake lives here:

```sh
. /home/weiss/.nix-profile/etc/profile.d/nix.sh   # Nix 2.35.6, single-user
cd nix/
nix build .#vc-room .#portal .#calrs --no-link --print-out-paths
./result | docker load                             # streamLayeredImage results are scripts
docker tag vc-room:1.0.0 ghcr.io/tobias-weiss-ai-xr/vc-room:1.0.0
```

Commit everything before building — flakes only see git-tracked files. Note
that `docker load < result` does **not** work: a `streamLayeredImage` result
is a shell script that streams the image to stdout.

## Rust packages and the MSRV pin

`portal` and `calrs` are built with a dedicated **`nixpkgs-rust` input**
(`nixpkgs-unstable`): their Cargo.lock files require rustc ≥ 1.88/1.89
(`encoding_rs` 0.8.42, `icu` 2.3, `uuid` 1.28), while `nixos-25.05` ships
rustc 1.86. `vc-room` (Python) and `opencloud` (prebuilt binary) stay on the
stable `nixos-25.05` input.

## Notes and caveats

- `nix.conf` needs `sandbox = false` on this host (OpenVZ) and
  `experimental-features = nix-command flakes`.
- Adding a new flake input and immediately building rewrites `flake.lock`
  mid-evaluation (Nix 2.35: *"store path … contents have changed"*, exit
  102). Run `nix flake lock`, commit, then build.
- **No setuid/setgid binaries** in any of the three owned images (verified by
  `docker export | tar -tv` scans) — matches the compose `cap_drop: ALL`
  posture.
- **docker >= 29 openat2 caveat**: `docker exec` and compose healthchecks
  walk `/etc/passwd` + `/etc/group` with `RESOLVE_BENEATH` and fail across
  the layer boundary when those are symlinks into `/nix/store`
  (*"openat etc/group: path escapes from parent"*). calrs therefore
  materializes them as REAL files in `fakeRootCommands` (same workaround
  as `opencloud.nix`); `portal` and `vc-room` ship no etc files, which the
  same walker tolerates.
- **Compose healthcheck parity**: the healthchecks in
  `docker-compose.yml` run in-image binaries — portal's
  `["CMD","/busybox","wget",…]` needs the busybox binary at the Dockerfile's
  exact `/busybox` path (shipped by `portal.nix`), and vc-room's
  `["CMD","python","-c",…]` needs `python` on `/bin` (symlinked by
  `vc-room.nix`). calrs already had `curl` on `/usr/local/bin`. All three
  healthchecks verified `healthy` on 2026-11-14.
- **calrs migration note**: the previously deployed calrs container ran as
  uid **999**; the flake image runs uid **1000** (`images/calrs/Dockerfile`
  creates the `calrs` user dynamically). When adopting
  `calrs:1.6.0-openSME`, chown the `calrs-data` volume once:
  `docker run --rm -v calrs-data:/d alpine chown -R 1000:1000 /d`.
- calrs patches (`images/calrs/patches/0001-oidc-zitadel-compat` …
  `0005-branding-footer`) are applied in filename order via `applyPatches`;
  they never touch `Cargo.toml`/`Cargo.lock`.
- calrs links the **system** libsqlite3 (sqlx's `libsqlite3-sys` is
  unbundled in the upstream lock), so `buildInputs = [ sqlite ]` +
  `pkg-config` are required.

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

## Build + load (opencloud)

```sh
. /home/weiss/.nix-profile/etc/profile.d/nix.sh   # Nix 2.35.6, single-user
cd nix/                                           # this flake lives in nix/
nix build .#opencloud
./result | docker load                            # streamLayeredImage result is a script
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

## Owned image layouts

Each image mirrors its Dockerfile counterpart so the compose definitions
(`read_only`, `cap_drop: ALL`, healthchecks) work unchanged.

**`vc-room:1.0.0`** (`vc-room.nix`, source `images/vc-room/`): Flask app at
`/app` (0555), Jinja templates, LiveKit client SDK vendored at
`/app/static/livekit-client.umd.min.js` (upstream 2.23.0 ships only the
non-`.min` UMD file; SRI-verified, sha256-+2RvImDYT5EjjQotw…), busybox
`/bin`, CA certs, uid 1000:1000, port 8090, `Cmd` runs the same
`app.run(host='0.0.0.0', port=8090)` as the Dockerfile.

**`portal:0.1.0`** (`portal.nix`, source `portal/`): `opensme-portal` binary
(cargoLock `portal/Cargo.lock`, checked tests disabled in-image) at
`/opensme-portal` (0555), busybox `/bin`, uid 1000:1000, writable
`/data` + `/data/attachments` (compose mounts the `portal-data` volume over
`/data`), port 8080.

**`calrs:1.6.0-openSME`** (`calrs.nix`, upstream `olivierlambert/calrs`
v1.6.0 + local patches): `calrs` binary at `/usr/local/bin/calrs`,
templates at `/opt/calrs/templates` (`WorkingDir /opt/calrs`), `/etc/passwd`
+ `/etc/group` with `calrs` uid/gid 1000, `curl` + CA certs (compose
healthcheck), `CALRS_DATA_DIR=/var/lib/calrs` pre-created writable, port
3000, `ENTRYPOINT [calrs]` + `CMD [serve --host 0.0.0.0 --port 3000]`.

Smoke-verified (2026-11-14): vc-room serves `/healthz` (`ok`) and the
vendored SDK (610302 bytes), portal serves `/` and `/health` (200), calrs
applies all 45 DB migrations and answers `/` with the login redirect (303).
All three loaded and tagged `ghcr.io/tobias-weiss-ai-xr/<name>:<version>`
(vc-room 7061b0c75112, portal 3f813bad7518, calrs fc0f5ac88cca) with their
compose healthchecks reporting `healthy` — not pushed (no registry
credentials on this host).

## Not yet nixified (deliberate TODOs)

- **paperless-oidc** — its Dockerfile is `FROM
  ghcr.io/paperless-ngx/paperless-ngx:3.3.4` plus overlay files from a
  BuildKit context (`traefik/certs` fixture). Sketch:
  `dockerTools.pullImage` for the base + `dockerTools.buildImage` with
  `fromImage`, or a fakechroot copy layer; needs the cert fixture as a
  build input.
- **portal-web** — the Next.js app under `portal/web/` (optional
  `portal-web` compose service, not deployed here).
- **monitoring agents** — intentionally stay in their own
  [`monitoring/nix/`](../monitoring/nix/) flake (independent inputs, no
  shared rust toolchain).

## Deployment status (2026-11-14) — opencloud

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
