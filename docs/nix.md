<!--
SPDX-FileCopyrightText: 2026 Tobias Weiss
SPDX-License-Identifier: Apache-2.0
-->

# Nix-built images

Selected stack images are built with Nix for reproducibility, instead of
pulling upstream rolling tags. The flake patterns live in
[`monitoring/nix/`](../monitoring/nix/) (monitoring agents) and
[`nix/`](../nix/) (opencloud, vc-room, portal, calrs — a self-contained
flake; run `nix build .#<package>` from `nix/`).

## Owned stack images (vc-room, portal, calrs)

The three self-hosted images whose sources live in this repository are now
also flake-built, mirroring their Dockerfiles byte-for-byte in behavior:

| Package | Tag | Replaces |
|---|---|---|
| `vc-room` | `vc-room:1.0.0` | `images/vc-room/Dockerfile` (Flask, vendored LiveKit SDK) |
| `portal` | `portal:0.1.0` | `portal/Dockerfile` (Rust `opensme-portal`) |
| `calrs` | `calrs:1.6.0-openSME` | `images/calrs/Dockerfile` (upstream v1.6.0 + `images/calrs/patches/`) |

Same contract as the Docker builds: uid 1000:1000, the compose healthcheck
binaries (`python`, `curl`) present, CA certs, writable data dirs, no
setuid/setgid binaries (verified via `docker export` scans), so the
`read_only` + `cap_drop: ALL` compose posture holds. Loading requires
`./result | docker load` because every image is a `streamLayeredImage`.

Migration caveat for calrs: previously deployed builds ran as uid 999, the
flake image uses uid 1000 — chown the `calrs-data` volume once when
switching (`docker run --rm -v calrs-data:/d alpine chown -R 1000:1000 /d`).

Details, tags, and caveats: [`nix/README.md`](../nix/README.md).

## The opencloud flake

[`nix/`](../nix/) builds the OpenCloud image from the upstream **stable
release binary** (v9.1.0), pinned by the release's own `.sha256` sidecar.
It replaces `opencloudeu/opencloud-rolling:*` — a moving tag that once
shipped a broken build with zero notice. Key properties:

- **Architecture-native**: `nix build .#opencloud` selects the amd64 or arm64
  release asset from `stdenv.hostPlatform`, so the same expression builds on
  x86 and arm CI runners.
- **Drop-in compatible** with `opencloud/opencloud.yml`: binary at
  `/usr/bin/opencloud`, `curl` for the healthcheck, CA certs at the standard
  path, a POSIX shell for the mounted entrypoint, writable
  `/etc/opencloud` + `/var/lib/opencloud`, non-root default user (the
  compose stack keeps `user: "0:0"`).
- Local image tags: `opencloud:9.1.0-nix-flake` and
  `ghcr.io/tobias-weiss-ai-xr/opencloud:9.1.0-nix-flake` (not pushed; the
  deployed `:9.1.0` tag belongs to the separate source-built image).

Build it:

```sh
. /home/weiss/.nix-profile/etc/profile.d/nix.sh
cd nix/ && nix build .#opencloud && ./result | docker load
```

Details and deployment status: [`nix/README.md`](../nix/README.md). See also
[`pin-images.md`](pin-images.md) for the general pinning policy and
[`monitoring/nix/`](../monitoring/nix/) for the same pattern applied to the
monitoring agents.
