<!--
SPDX-FileCopyrightText: 2026 Tobias Weiss
SPDX-License-Identifier: Apache-2.0
-->

# Nix-built images

Selected stack images are built with Nix for reproducibility, instead of
pulling upstream rolling tags. The flake patterns live in
[`monitoring/nix/`](../monitoring/nix/) (monitoring agents) and
[`nix/`](../nix/) (opencloud).

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
cd nix/ && nix build .#opencloud && docker load < result
```

Details and deployment status: [`nix/README.md`](../nix/README.md). See also
[`pin-images.md`](pin-images.md) for the general pinning policy and
[`monitoring/nix/`](../monitoring/nix/) for the same pattern applied to the
monitoring agents.
