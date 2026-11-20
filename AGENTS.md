# AGENTS.md — Conventions for coding agents

## Build policy

- **The Rust portal (`portal/`) is built on the remote build host `vhrz2392`
  (ssh alias, user `ansible`), never locally** on the workstation. Do not run
  `cargo build` for `portal/` here — it only fills the disk.
- The host toolchain lives in `~/.cargo/bin` (cargo/rustc 1.97); run builds
  over ssh, e.g. `ssh vhrz2392 'cd <src> && cargo build --release'`.
- Local `portal/target/` is throwaway build cache and may be deleted freely.
