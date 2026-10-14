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
