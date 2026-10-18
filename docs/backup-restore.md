<!--
SPDX-FileCopyrightText: 2026 Tobias Weiss
SPDX-License-Identifier: Apache-2.0
-->

# Backup & Restore

How to back up the openSME stack, get the backup off the host, and restore it —
locally or onto a fresh machine. Tooling: [`scripts/backup.sh`](../scripts/backup.sh),
[`scripts/restore.sh`](../scripts/restore.sh) and
[`scripts/verify-backup.sh`](../scripts/verify-backup.sh); off-site pruning is
covered in [rotate-backups.md](rotate-backups.md).

> **A backup you have never restored is a hypothesis.** Run the
> [restore drill](#restore-drill) once per quarter on a throw-away host.

## What a backup set contains

`backup.sh` writes timestamped artifacts into `./backups/`:

| Artifact | Contents |
|---|---|
| `postgres_<ts>.sql.gz` | Full logical dump of all databases (`pg_dumpall`) |
| `traefik_<ts>.tar.gz` | ACME account + certificates (`traefik-data`) |
| `volumes_<ts>.tar.gz` | Named Docker volumes (`--volumes` only) |

With `--encrypt` every artifact is additionally written as `.enc` **and the
plaintext is deleted**, so only ciphertext is at rest.

Volumes covered by `--volumes` (the list is authoritative in `backup.sh`):
PostgreSQL, Redis, Portal, OpenCloud (config/data/lib), Stalwart, SOGo,
Traefik, Casdoor, Zitadel machinekey, CryptPad, Synapse, Notes, SeaweedFS,
Paperless (data/media/export), Invoice Ninja (MariaDB/public/storage), Nosdesk
uploads, CMS, Store, LimeSurvey uploads, Operaton, and the agent state volumes
(`dev-agent-state`, `dev-maintenance-bot-state`, `predictive-agent-state`,
`taskfleet-state`).

Deliberately **excluded**: `ai-models` and `ollama-models`. They are
re-downloadable model caches that would dominate the archive size;
`tests/00-static/check_backup.py` guards that every stateful volume is either
in `backup.sh` or documented as excluded — adding a stateful volume without
covering it fails CI.

## Take a backup

```sh
./scripts/backup.sh                       # PostgreSQL + Traefik
./scripts/backup.sh --volumes             # … plus all named volumes
./scripts/backup.sh --volumes --services opencloud,redis   # specific volumes only
./scripts/backup.sh --dry-run             # preview, touch nothing
./scripts/backup.sh --no-stop             # don't stop services (see caveat)
```

Make targets: `make backup`, `make backup-db`, `make backup-dry-run`.

**Consistency:** by default the stack is stopped while the volumes are
archived, which is the only way to get a crash-consistent copy of stateful
services. `--no-stop` is faster and usually fine for PostgreSQL (logical dump
is transactional) but can capture a torn volume — don't use it for the copy you
intend to rely on.

**Retention:** the local backup directory keeps the last **7 days**; older
`*.sql.gz`, `*.tar.gz` and `*.enc` files are pruned automatically. Longer
retention belongs off-site ([rotate-backups.md](rotate-backups.md)).

## Encrypted backups

`backup.sh --encrypt` wraps every artifact (PostgreSQL dump, volumes archive,
Traefik ACME/SSL archive) in AES-256-CBC with OpenSSL (PBKDF2, 100000
iterations, salted) and deletes the plaintext, so only `*.enc` files are left
at rest. `restore.sh` auto-detects these encrypted (`.enc`) artifacts and
decrypts them on the fly during a restore.

The passphrase is read **only** from `$BACKUP_PASSPHRASE` — never from a flag,
never logged, never defaulted:

```sh
export BACKUP_PASSPHRASE='your-secret'    # or read it from your secret store
./scripts/backup.sh --volumes --encrypt
```

To restore an encrypted backup, run `restore.sh` with the **same passphrase**
that was used for `backup.sh --encrypt`:

```sh
# Passphrase via flag
./scripts/restore.sh 20250815_143022 --passphrase 'your-secret'

# Passphrase via environment (the same env backup.sh --encrypt reads)
BACKUP_PASSPHRASE='your-secret' ./scripts/restore.sh 20250815_143022
```

`restore.sh` prefers the plaintext file when both a plaintext and a `.enc`
variant exist; otherwise it restores from the `.enc`. If only an encrypted
artifact exists and no passphrase is supplied, the restore aborts with an
error rather than silently restoring an empty/undecrypted stream.

Store the passphrase somewhere other than the backup host — otherwise the
encryption only protects against a leaked bucket, not against a lost machine.

## Upload off-site

`backup.sh --s3` syncs `./backups/` to `s3://$S3_BUCKET/$S3_PREFIX` via `s3cmd`
(default) or `rclone`; it never deletes on the remote (that is
`rotate-backups.sh`'s job).

| Variable | Meaning |
|---|---|
| `S3_BUCKET` | Target bucket (required for `--s3`) |
| `S3_ENDPOINT` | MinIO/OpenCloud S3 endpoint; empty = AWS S3 |
| `S3_ACCESS_KEY` / `S3_SECRET_KEY` | Credentials (or use `s3cmd`'s own config) |
| `S3_PREFIX` | Key prefix, default `backups` |
| `RCLONE_REMOTE` | Remote for `--s3-rclone`, default `s3:$S3_BUCKET` |

```sh
./scripts/backup.sh --volumes --encrypt --s3            # s3cmd
./scripts/backup.sh --volumes --encrypt --s3-rclone     # rclone only
./scripts/backup.sh --volumes --encrypt --s3-tool rclone
```

Use `--encrypt` for anything leaving the host: object storage is rarely as
private as you assume, and the PostgreSQL dump contains every service's data.

## Verify a backup set

```sh
./scripts/verify-backup.sh                        # newest set in ./backups
./scripts/verify-backup.sh --timestamp 20250815_143022
./scripts/verify-backup.sh --backup /mnt/backups --list
./scripts/verify-backup.sh --passphrase 'your-secret'   # .enc artifacts
```

It checks artifact presence, runs `gzip -t` / `tar -tzf`, and (for `.enc`
archives) proves the set decrypts with the current passphrase. Read-only — it
never modifies the backup directory. Run it as the last step of any backup job:
an upload that produced a truncated file looks successful otherwise.

## Restore

```sh
./scripts/restore.sh --list                        # show available sets (+ [encrypted])
./scripts/restore.sh --dry-run 20250815_143022     # preview
./scripts/restore.sh 20250815_143022               # PostgreSQL + volumes
./scripts/restore.sh 20250815_143022 --pg-only     # databases only
./scripts/restore.sh 20250815_143022 --volumes-only
```

The `<backup-prefix>` is the timestamp, e.g. `20250815_143022`. Restoring
volumes overwrites the current volume contents — stop the affected services
first, and expect to restart them afterwards.

### Restoring onto a fresh host

1. Clone the repo and `make bootstrap` (generates a `.env` with new secrets).
2. Copy the backup set into `./backups/` (or point `BACKUP_DIR` at it).
3. Restore databases + volumes as above.
4. **Reconcile secrets.** OIDC client secrets, DB passwords and the Zitadel
   machinekey live inside the restored state *and* in `.env`; if you deployed
   with fresh secrets, either restore the original `.env` from your secret
   store or rotate the values and re-run the dependent service
   (`./scripts/zitadel-client.sh` re-registers clients idempotently).
5. Certificates: a restored `traefik-data` volume carries the ACME account and
   certs, so Let's Encrypt will not re-issue — fine when the domain is
   unchanged, re-check after a domain move.
6. `make test` (layers 0–4) against the restored stack.

### Restore drill

Once per quarter, on a disposable host or a separate Compose project name:
restore the latest off-site set, run `make test`, log the timings and any
surprises in the team runbook. Anything you had to do by hand belongs in
`scripts/restore.sh`, not in a wiki page.

## Scheduling

```cron
# nightlies: stop, archive, encrypt, upload, verify, prune the remote
15 2 * * *  cd /opt/opensme-compose && BACKUP_PASSPHRASE="$(cat /etc/opensme/backup.pass)" \
  ./scripts/backup.sh --volumes --encrypt --s3-rclone >> logs/backup.log 2>&1 \
  && ./scripts/verify-backup.sh >> logs/backup.log 2>&1 \
  && ./scripts/rotate-backups.sh --remote "s3:$S3_BUCKET" --retention 30 >> logs/backup.log 2>&1
```

Keep `logs/` out of git (`check_artifacts.py` enforces this) and make sure the
passphrase file is `chmod 600`, owned by the job user.

## Gotchas

- **`backups/` is never committed.** It contains full database dumps and
  certificates; the static layer fails the build if it is.
- **Local retention is 7 days**, independent of the remote retention. A weekly
  local-only schedule silently loses data.
- **`.enc` files older than 7 days are pruned too** — the remote copy is the
  only long-term one.
- **Volumes are archived, not snapshotted.** `--no-stop` can produce a torn
  archive; PostgreSQL is the exception (transactional logical dump).
- **`COMPOSE_FILE` matters.** `backup.sh` uses the same compose files as the
  running stack; exporting a different `COMPOSE_FILE` may miss volumes.
