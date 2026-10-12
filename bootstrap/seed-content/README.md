# Demo seed content

Human-readable sample documents you can load into openSME services to make
a fresh deployment feel real — and to demonstrate the product end-to-end.
Everything here is **plain Markdown**, deliberate and small; nothing is
consumed by the demo boot or by CI, so it never breaks the test suite.

> Unlike the BPMN files in `../bpmn/` (which are deployed to the workflow
> engine), these are *content* — the kind of files real members create.

## Documents

| File | Purpose |
|------|---------|
| `01-welcome.md` | Orientation note mapping services → what they do |
| `02-team-charter.md` | A "how we work" agreement members can adopt |
| `03-decision-record.md` | RFC-style record that mirrors the approval workflow |
| `04-onboarding-checklist.md` | Repeatable new-member checklist |

## How to load them

### Into the Cloud (OpenCloud)

Upload with the Cloud's web UI (drag & drop into `Team/`), or via WebDAV
once the admin account has logged in at least once:

```bash
# OpenCloud exposes WebDAV under /dav (Basic auth, admin login required)
curl -sk --user "admin:$OC_ADMIN_PASSWORD" -T bootstrap/seed-content/01-welcome.md \
  "https://cloud.<domain>/dav/Team/welcome.md"
```

### Into Paperless (-ngx, `--profile paperless`)

Drop the files into the Paperless **consume** watched folder; the consumer
indexes and tags them automatically:

```bash
cp bootstrap/seed-content/*.md /path/to/paperless/consume/
```

### As Portal "Team Notes"

Team Notes accept a cloud share link as an attachment. Post the link to
`01-welcome.md` as a one-line welcome note so every member sees it on the
friendly *Team Notes* card. (Notes are in-memory and per-server — post
them after a fresh boot.)

## Editorial rules

- Keep each document under ~40 lines so they stay *sample* material.
- Refer only to services that exist in this stack (`Identity`, `Cloud`,
  `Webmail`, `Office`, `Workflow`, `Support`, `Portal`).
- No real names, credentials, or internal data — these ship in the repo.
