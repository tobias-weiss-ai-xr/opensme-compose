# AI deeper integration — design (SOGo, Paperless, booking, Element)

**Date:** 2026-11-15
**Status:** approved (user "ok" on 2026-11-15)
**Scope:** 4 tasks, one agentflow campaign `ai-deeper`

## Goal

Wire AI into the tools that don't have it yet: SOGo webmail (draft reply + inbox brief), Paperless (auto-classify), the booking flow (meeting-prep brief), and Element rooms beyond #support. Approach: reuse the proven LiteLLM gateway (`litellm:4000`, 72 models) and the existing Matrix bot — no new services, no SOGo hacks.

## Non-goals (YAGNI)

- Invoice Ninja / Camunda text generation — low value, deferred invoice-BPMN loop doesn't need an LLM.
- OpenWebUI OIDC via Zitadel — admin-API dance, deferred.
- SOGo UI injection via traefik — fragile, rejected.

## Architecture

All four tasks extend existing proven components; no new containers:

```
Element #support (and others)  ←  ai-room-bot.py (extended: !ai, !entwurf, !brief)
                                    │
                                    ▼
                            LiteLLM gateway :4000  (72 models, glm-5.3-flash default)
                                    │
                    ┌───────────────┼───────────────┐
                    ▼               ▼               ▼
            SOGo Drafts (IMAP APPEND)   Paperless REST API   booking-notify POST
```

## Task breakdown

### T1 — SOGo AI drafts + inbox brief (`scripts/`, `docs/`)
- Extend `scripts/ai-room-bot.py` (bind-mounted `:ro` into `opensme-ai-bot`) with:
  - `!entwurf <empfänger>: <stichpunkte>` → LLM composes a German reply draft → IMAP APPEND to the bot user's `Drafts` mailbox with `\Draft` flag → appears in SOGo under „Entwürfe"; user reviews and sends. Zero SOGo modification.
  - `!brief` → inbox brief (unread + key items) posted back to the room.
- Foundation: stdlib `imaplib` + Stalwart IMAP (`stalwart:143` inside netns, host `127.0.0.1:143`); bot account `testuser`/`MATRIX_PASSWORD` baseline `eval2026`; LLM via gateway.
- Gate: `py_compile` + files + report `docs/af-ai-T4-sogo.txt` (repo-root only).
- Deploy: update script file on host + `rsync` to deployed `scripts/`, `up -d ai-bot` (no rebuild — bind mount).

### T2 — Paperless auto-classification (`scripts/`, `docs/`)
- `scripts/paperless-classify.py`: list untagged documents via Paperless REST API → LLM suggests Belegart/Korrespondent/Zusammenfassung → set tags + metadata via API.
- One-time: create an API token in Paperless admin UI → store as `PAPERLESS_API_TOKEN` in `.env` (documented, never printed).
- Optional hook: invoke classifier on the new `document_id` from `paperless-intake.sh` (documented, not auto-wired in v1).
- Gate: `py_compile` + files + report `docs/af-ai-T5-paperless.txt`.

### T3 — Meeting-prep brief (`scripts/`, `docs/`)
- Extend `booking-notify.sh`: when a booking is confirmed, attach an AI-generated agenda + one-line guest context to the existing Matrix post.
- Gate: `bash -n` + `shellcheck` + report `docs/af-ai-T6-booking.txt`.

### T4 — Bot join-on-invite + Notes discovery (`scripts/`, `docs/`)
- `ai-room-bot.py` `--serve`: handle `m.room.member` invite events → join room → serve `!ai` there. Zero new config.
- Notes (La Suite impress) discovery spike: read `services/notes.yml` + impress backend env for native AI hooks; document findings (no code change if native hooks exist — wire portal instead).
- Gate: `py_compile` + files + report `docs/af-ai-T7-bot-notes.txt`.

## Constraints (baked into task contexts)

- Report to EXACT repo-root `docs/` path only (CWD-leak caused scope rejections before).
- Worktrees have no `node_modules` — gates exit 127; workers commit + report, orchestrator hand-runs gates in main repo (`PATH=/opt/node-host/bin`).
- Never print `.env`/`LITELLM`/`MATRIX`/`PAPERLESS_API_TOKEN` secret values.
- `sudo` NOPASSWD; `numiptent` ~1747/2000 (watch headroom).
- Main at `ef98531`, pushed, clean; portal suite 251 + static 26.

## Execution

Agentflow campaign `ai-deeper` (root `/home/weiss/af-camps/ai-deeper`), 1 worker (`oc`, glm-5.3-flash), `AF_MAX_PARALLEL=1`, recovery playbook validated 6/6 in the prior campaign.

## Open questions (resolved)

- SOGo drafts trigger: **Element bot command first** (`!entwurf`), portal card as v2.
- Notes: discovery spike inside T4, no commitment either way.