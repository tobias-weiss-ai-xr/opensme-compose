# Off-site backup rotation via rclone

`scripts/rotate-backups.sh` prunes old backups from an off-site object
storage remote (S3 / MinIO / OpenCloud) with `rclone`, keeping the most
recent N days. It complements `scripts/backup.sh --s3`, which uploads
backups to the remote but never removes anything — without rotation the
remote fills up forever.

## What it does

- Removes everything older than `--retention` days under a `--prefix` on
  an rclone remote via `rclone delete --rmdirs --min-age Nd`.
- `--dry-run` previews without deleting; a summary of deleted/kept items
  is always printed.
- Safe: `--remote` is required, the remote root is never targeted, and
  only the `--prefix` subtree is touched. Exit `0` on success, `1` on error.

## Configure rclone

1. Install rclone: `curl https://rclone.org/install.sh | sudo bash`.
2. Configure a remote (e.g. 's3' against a MinIO/OpenCloud S3 endpoint):
   `rclone config`, then verify with `rclone lsd s3:`.

## Run it

```sh
./scripts/rotate-backups.sh --remote s3:opensme-backups --dry-run   # preview
./scripts/rotate-backups.sh --remote s3:opensme-backups --retention 7
./scripts/rotate-backups.sh --remote s3:opensme-backups --prefix backups/
# defaults: retention 30 days, prefix backups/
```

## Complements backup.sh --s3

`backup.sh --s3` uploads `./backups/` to `s3://$S3_BUCKET/$S3_PREFIX` and
keeps only 7 days locally. Point rotate-backups.sh at the same
bucket/prefix (start with `--dry-run`), then schedule both in cron, e.g.
`backup.sh --s3` followed by `rotate-backups.sh --remote "s3:$S3_BUCKET"
--retention 7`, so the off-site copy stays in line with local retention.

## Matching backup.sh

`scripts/backup.sh --s3-rclone` (or `--s3-tool rclone`) uploads backups
with **rclone** instead of s3cmd, so both scripts can share one tool and
one config for the same S3 bucket. It translates the existing
`S3_BUCKET`/`S3_ENDPOINT`/`S3_ACCESS_KEY`/`S3_SECRET_KEY` env into
rclone's inline `--s3-provider Minio|AWS --s3-endpoint … --s3-access-key-id
… --s3-secret-access-key …` flags, and uploads each artifact to
`$RCLONE_REMOTE/$S3_PREFIX/` where `RCLONE_REMOTE` defaults to
`s3:$S3_BUCKET` — no named rclone remote required. Point
`rotate-backups.sh --remote "$RCLONE_REMOTE:$S3_PREFIX"` (e.g.
`--remote s3:opensme-backups:backups`) at the same remote/prefix and the
two scripts align on the same rclone configuration, so an operator can use
rclone alone: `backup.sh --s3-rclone` to upload and
`rotate-backups.sh --remote s3:$S3_BUCKET --prefix $S3_PREFIX` to prune.
s3cmd remains the default uploader and is fully unchanged when
`--s3-rclone`/`--s3-tool` are not given.
