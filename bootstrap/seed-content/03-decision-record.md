# Decision Record — Approval workflow for purchase requests

**Status:** decided · **Owner:** Operations · **Date:** (set today)

> Format: a short RFC-style decision record. Keep it to one page; a
> reviewer should be able to vote on it in minutes. This is also the shape
> of a document you would attach to a *Workflow* approval instance.

## Context

Purchase requests keep arriving in email threads, get lost, and there is
no audit trail of who approved what or when. We need a repeatable,
reviewable path from request → approval → archive.

## Decision

Send purchase requests through the **Workflow** engine:

1. The requester submits a short **Workflow** instance.
2. Below EUR 500 the request auto-approves; above that a manager approves.
3. Every approved request is archived to the **Cloud → Invoices** folder
   (Machine WebDAV) so finance always sees the same picture.
4. Rejections notify the requester once — no siloed email chains.

## Consequences

- **Good:** one source of truth, a full audit trail, no lost requests.
- **Cost:** requesters must know how to start a workflow instance
  (15-minute training).
- **Risk:** the archive folder must stay write-only for the machine account
  and human-readable for finance only.

## Rollback

Reverting is simply disabling the workflow and going back to email — the
archive keeps every decision for audit.

*Sample document — see `bootstrap/seed-content/README.md` for how to load
it into your document store.*
