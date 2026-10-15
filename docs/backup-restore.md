# Backup & Restore

This document describes how to back up and restore the openSME stack with
`scripts/backup.sh` and `scripts/restore.sh`.

## Encrypted backups

`backup.sh --encrypt` wraps every artifact (PostgreSQL dump, volumes archive,
Traefik ACME/SSL archive) in AES-256-CBC with OpenSSL (PBKDF2, 100000
iterations, salted) and deletes the plaintext, so only `*.enc` files are left
at rest. `restore.sh` auto-detects these encrypted (`.enc`) artifacts and
decrypts them on the fly during a restore.

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

Other useful modes:

```sh
./scripts/restore.sh --list                                  # .enc sets show [encrypted]
./scripts/restore.sh --dry-run 20250815_143022 --passphrase 'your-secret'
./scripts/restore.sh 20250815_143022 --pg-only       --passphrase 'your-secret'
./scripts/restore.sh 20250815_143022 --volumes-only  --passphrase 'your-secret'
```
