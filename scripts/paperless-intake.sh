#!/usr/bin/env bash
# paperless-intake.sh — invoice attachment intake from Stalwart into Paperless.
#
# WHY: openSME invoices arrive as e-mail attachments. Paperless' consumer
#      watches the host bind ./services/paperless-consume (services/
#      paperless.yml, PAPERLESS_CONSUMPTION_DIR=/consume) and ingests every
#      file dropped there. This script is the mail→consume bridge for NOW:
#      it fetches attachments from a Stalwart mailbox over IMAP and drops
#      them into the consume dir. Copy semantics only — nothing is ever
#      deleted or flagged in the mailbox (READ-ONLY IMAP select).
#
# The SIEVE side (filing invoice mail into the dedicated mailbox) is
# server-managed in Stalwart 0.15 — there is no filesystem automount for
# sieve scripts, so the rule is PRINTED for the admin to install via the
# Stalwart admin UI (or REST):
#
#   ./scripts/paperless-intake.sh --print-sieve
#
# Credentials (IMAP auth against the OIDC-backed directory — regular
# passwords are INERT, see docs/SSO.md):
#   PLAIN    : an app password. Mint once per account with
#              scripts/stalwart-app-password.sh <email> paperless-intake
#              and export PAPERLESS_INTAKE_IMAP_USER / _IMAP_PASSWORD.
#   XOAUTH2  : a live OIDC access token in PAPERLESS_INTAKE_TOKEN_FILE
#              (expires with the webmail session; handy for smoke tests).
#
# Usage:
#   ./scripts/paperless-intake.sh [--once]                  # one poll pass (default)
#   ./scripts/paperless-intake.sh --loop N                  # poll every N seconds
#   ./scripts/paperless-intake.sh --dry-run [--once]        # list, drop nothing
#   ./scripts/paperless-intake.sh --mailbox INvoices        # IMAP folder (default)
#   ./scripts/paperless-intake.sh --print-sieve             # sieve rule + install doc
#   ./scripts/paperless-intake.sh --status                  # state file summary
#
# Configuration (env, with defaults):
#   PAPERLESS_INTAKE_IMAP_HOST     127.0.0.1        (stalwart:143 works inside netns)
#   PAPERLESS_INTAKE_IMAP_PORT     143
#   PAPERLESS_INTAKE_IMAP_USER     (required)       e.g. invoices@opensme.local
#   PAPERLESS_INTAKE_IMAP_PASSWORD (required*)      app password for PLAIN auth
#   PAPERLESS_INTAKE_TOKEN_FILE    (required*)      XOAUTH2 access token file
#                                    *) exactly one auth source must be set
#   PAPERLESS_INTAKE_CONSUME_DIR   /opt/opensme-compose/compose/services/paperless-consume
#   PAPERLESS_INTAKE_STATE_FILE    <repo>/scripts/.paperless-intake.state.json
#                                    (remembers processed UIDs + attachment
#                                     hashes so nothing is re-dropped)
#
# Exit codes: 0 ok · 2 usage · 3 auth/IMAP failure · 4 consume dir unusable
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

IMAP_HOST="${PAPERLESS_INTAKE_IMAP_HOST:-127.0.0.1}"
IMAP_PORT="${PAPERLESS_INTAKE_IMAP_PORT:-143}"
IMAP_USER="${PAPERLESS_INTAKE_IMAP_USER:-}"
IMAP_PASSWORD="${PAPERLESS_INTAKE_IMAP_PASSWORD:-}"
TOKEN_FILE="${PAPERLESS_INTAKE_TOKEN_FILE:-}"
CONSUME_DIR="${PAPERLESS_INTAKE_CONSUME_DIR:-/opt/opensme-compose/compose/services/paperless-consume}"
# state lives under scripts/ (not data/ — repo has no data/ scope) and is
# gitignored; override via PAPERLESS_INTAKE_STATE_FILE
STATE_FILE="${PAPERLESS_INTAKE_STATE_FILE:-${REPO_ROOT}/scripts/.paperless-intake.state.json}"

MAILBOX="INvoices"
MODE="once"
LOOP_SECS=0
DRY_RUN=false
PRINT_SIEVE=false

usage() { grep '^#   \./scripts/paperless-intake' "${BASH_SOURCE[0]}" | sed 's/^# *//'; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --once)      MODE="once"; shift ;;
        --loop)      MODE="loop"; LOOP_SECS="${2:?--loop N needs an interval}"; shift 2 ;;
        --dry-run)   DRY_RUN=true; shift ;;
        --mailbox)   MAILBOX="${2:?--mailbox NAME needs a folder name}"; shift 2 ;;
        --print-sieve|--sieve) PRINT_SIEVE=true; shift ;;
        --status)    MODE="status"; shift ;;
        -h|--help)   usage; exit 0 ;;
        *) echo "paperless-intake: unknown option: $1" >&2; usage; exit 2 ;;
    esac
done

# ── sieve side ────────────────────────────────────────────────────────────────
print_sieve() {
    cat <<'SIEVE'

────────────────────────────────────────────────────────────────────────────
 Stalwart sieve rule — file invoice mail into the dedicated mailbox
 (install via Stalwart admin UI → account → Sieve Scripts; server-managed
  sieve, no filesystem automount available in Stalwart 0.15)

 Account : the invoice recipient (e.g. invoices@opensme.local)
 Mailbox : INvoices  ← scripts/paperless-intake.sh fetches attachments here
────────────────────────────────────────────────────────────────────────────

require ["fileinto", "mailbox"];

# openSME invoice intake — file likely invoices into "INvoices"
# (scripts/paperless-intake.sh picks the attachments up from there).
if anyof (
    header :contains "Subject" ["invoice", "Invoice", "INVOICE",
                                "Rechnung", "INV-"],
    header :is "X-Invoice" ["1", "true", "yes"],
    address :domain :is ["from"] "graphwiz.ai"
) {
    fileinto :create "INvoices";
    stop;
}

────────────────────────────────────────────────────────────────────────────
 Notes for the admin:
 • Stalwart matches BEFORE its spam heuristics only if you also disable
   spam filing for trusted senders — trusted invoices@graphwiz.ai may
   otherwise land in "Junk Mail" (DMARC-less senders do today). Whitelist
   the sender in the Stalwart admin UI (Settings → Spam) or move misfiled
   mail into INvoices once; the intake script re-scans the whole folder.
 • App passwords (what paperless-intake.sh uses) are minted self-service:
   sudo ./scripts/stalwart-app-password.sh <email> paperless-intake
 • REST alternative to the admin UI (admin session):
     PUT /api/principal/<account>/script/<name>   (Content-Type: application/sieve)
   then activate the script for the account.
────────────────────────────────────────────────────────────────────────────
SIEVE
}

if $PRINT_SIEVE; then
    print_sieve
    exit 0
fi

# ── status ────────────────────────────────────────────────────────────────────
if [[ "$MODE" == "status" ]]; then
    if [[ ! -f "$STATE_FILE" ]]; then
        echo "paperless-intake: no state file at $STATE_FILE (nothing processed yet)"
    else
        python3 - "$STATE_FILE" <<'PY'
import json, sys
st = json.load(open(sys.argv[1]))
done = st.get("done", {})
print("state file:", sys.argv[1])
for box, uids in sorted(done.items()):
    print(f"  {box}: {len(uids)} message(s) processed")
print("  distinct attachment hashes seen:", len(st.get("seen_sha", [])))
PY
    fi
    exit 0
fi

# ── preflight ─────────────────────────────────────────────────────────────────
if [[ -z "$IMAP_USER" ]]; then
    echo "paperless-intake: PAPERLESS_INTAKE_IMAP_USER is not set" >&2
    exit 2
fi
if [[ -n "$TOKEN_FILE" && -n "$IMAP_PASSWORD" ]]; then
    echo "paperless-intake: set either PAPERLESS_INTAKE_TOKEN_FILE (XOAUTH2) or PAPERLESS_INTAKE_IMAP_PASSWORD (PLAIN), not both" >&2
    exit 2
fi
if [[ -z "$TOKEN_FILE" && -z "$IMAP_PASSWORD" ]]; then
    echo "paperless-intake: no auth source — set PAPERLESS_INTAKE_IMAP_PASSWORD (app password) or PAPERLESS_INTAKE_TOKEN_FILE (XOAUTH2)" >&2
    exit 2
fi
if [[ -n "$TOKEN_FILE" && ! -f "$TOKEN_FILE" ]]; then
    echo "paperless-intake: token file not found: $TOKEN_FILE" >&2
    exit 2
fi
if ! command -v python3 >/dev/null 2>&1; then
    echo "paperless-intake: python3 is required (host has it; netns variants too)" >&2
    exit 2
fi

# ── one poll pass ─────────────────────────────────────────────────────────────
run_pass() {
    local pi_dry=0
    [[ $DRY_RUN == true ]] && pi_dry=1
    export PI_HOST="$IMAP_HOST" PI_PORT="$IMAP_PORT" PI_USER="$IMAP_USER" \
           PI_MAILBOX="$MAILBOX" PI_CONSUME_DIR="$CONSUME_DIR" \
           PI_STATE_FILE="$STATE_FILE" PI_DRY_RUN="$pi_dry"
    if [[ -n "$TOKEN_FILE" ]]; then
        local pi_token
        pi_token="$(cat "$TOKEN_FILE")"
        export PI_TOKEN="$pi_token"
    else
        export PI_TOKEN=""
        export PI_PASSWORD="$IMAP_PASSWORD"
    fi

    python3 <<'PY'
import email, hashlib, imaplib, json, os, re, sys, time

def die(code, msg):
    print(f"paperless-intake: {msg}", file=sys.stderr)
    sys.exit(code)

host, port = os.environ["PI_HOST"], int(os.environ["PI_PORT"])
user, mailbox = os.environ["PI_USER"], os.environ["PI_MAILBOX"]
consume, statef = os.environ["PI_CONSUME_DIR"], os.environ["PI_STATE_FILE"]
dry = os.environ["PI_DRY_RUN"] == "1"
token, password = os.environ.get("PI_TOKEN", ""), os.environ.get("PI_PASSWORD", "")

try:
    m = imaplib.IMAP4(host, port, timeout=30)
except Exception as e:
    die(3, f"cannot reach IMAP {host}:{port} — {e}")

try:
    if token:  # XOAUTH2 (live OIDC access token)
        m.authenticate("XOAUTH2",
                       lambda _: f"user={user}\x01auth=Bearer {token}\x01\x01".encode())
    else:      # PLAIN with an app password (see stalwart-app-password.sh)
        m.login(user, password)
except Exception as e:
    die(3, f"auth failed for {user} on {host}:{port} — {e}")

# READ-ONLY select: the consumer never marks mail \Seen, never deletes —
# copy semantics by construction.
typ, data = m.select(mailbox, readonly=True)
if typ != "OK":
    boxes = b", ".join(m.list()[1]).decode(errors="replace")
    m.logout()
    die(3, f"cannot select mailbox '{mailbox}' — try one of: {boxes}")

uidvalidity = None
typ, data = m.status(mailbox, "(UIDVALIDITY)")
if typ == "OK":
    mv = re.search(r"UIDVALIDITY (\d+)", data[0].decode(errors="replace"))
    uidvalidity = mv.group(1) if mv else None

typ, data = m.uid("search", None, "ALL")
uids = data[0].split() if typ == "OK" else []
uids = [u.decode() for u in uids]

os.makedirs(os.path.dirname(statef), exist_ok=True)
state = {"uidvalidity": {}, "done": {}, "seen_sha": []}
if os.path.exists(statef):
    try:
        state = json.load(open(statef))
    except Exception:
        pass  # corrupt state → start over (hash dedupe below still guards)
if uidvalidity:
    prev = state["uidvalidity"].get(mailbox)
    if prev and prev != uidvalidity:
        print(f"paperless-intake: UIDVALIDITY for {mailbox} changed "
              f"({prev} -> {uidvalidity}) — forgetting per-UID history")
        state["done"][mailbox] = []
state["uidvalidity"][mailbox] = uidvalidity or ""
done = set(state["done"].get(mailbox, []))
seen_sha = set(state["seen_sha"])

dropped, skipped = 0, 0
for uid in uids:
    if uid in done:
        skipped += 1
        continue
    typ, data = m.uid("fetch", uid, "(RFC822)")
    if typ != "OK" or not data or not data[0]:
        print(f"paperless-intake: fetch failed for uid {uid} — skipping")
        continue
    msg = email.message_from_bytes(data[0][1])
    for part in msg.walk():
        fn = part.get_filename()
        if not fn or part.get_content_maintype() == "multipart":
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        sha = hashlib.sha256(payload).hexdigest()
        if sha in seen_sha:
            print(f"  uid {uid}: {fn} — already dropped earlier (hash match)")
            continue
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", fn).lstrip(".") or "attachment.bin"
        ts = time.strftime("%Y%m%d-%H%M%S")
        final = os.path.join(consume, f"{ts}-uid{uid}-{safe}")
        if dry:
            print(f"  [dry-run] would drop uid {uid}: {fn} "
                  f"({len(payload)} bytes) -> {final}")
            continue
        tmp = final + ".part"
        with open(tmp, "wb") as fh:
            fh.write(payload)
        os.replace(tmp, final)  # atomic: paperless never sees a partial file
        seen_sha.add(sha)
        dropped += 1
        print(f"  uid {uid}: dropped {fn} ({len(payload)} bytes) -> {final}")
    if not dry:
        done.add(uid)

if not dry:
    state["done"][mailbox] = sorted(done)
    state["seen_sha"] = sorted(seen_sha)
    tmpf = statef + ".part"
    with open(tmpf, "w") as fh:
        json.dump(state, fh, indent=1)
    os.replace(tmpf, statef)

m.logout()
print(f"paperless-intake: {mailbox}@{user} — {len(uids)} message(s), "
      f"{skipped} already handled, {dropped} attachment(s) "
      f"{'would be dropped' if dry else 'dropped'}"
      f"{' [dry-run]' if dry else ''}")
PY
}

# ── consume dir sanity (fail fast, before any IMAP chatter) ───────────────────
if [[ ! -d "$CONSUME_DIR" ]]; then
    echo "paperless-intake: consume dir does not exist (or is not accessible): $CONSUME_DIR" >&2
    echo "  (deployed stack: ./services/paperless-consume next to docker-compose.yml;" >&2
    echo "   /opt paths are root-owned — run this script via sudo)" >&2
    exit 4
fi
if [[ $DRY_RUN == false && ! -w "$CONSUME_DIR" ]]; then
    echo "paperless-intake: consume dir not writable: $CONSUME_DIR" >&2
    exit 4
fi

if [[ "$MODE" == "loop" ]]; then
    trap 'echo "paperless-intake: loop stopped"; exit 0' INT TERM
    while true; do
        run_pass
        sleep "$LOOP_SECS"
    done
else
    run_pass
fi
