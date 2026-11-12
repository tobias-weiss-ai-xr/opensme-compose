<!--
SPDX-FileCopyrightText: 2026 Tobias Weiss
SPDX-License-Identifier: Apache-2.0
-->

# Mail scope: closed system

**Decision (2026-11-12): the bundled mail stack is a closed system.** It
delivers mail between local accounts only. It is not an internet mail server,
and it is not intended to become one by accident.

This is a deliberate scope decision, not a defect. Read it before filing or
"fixing" anything about MX, SPF, DKIM, DMARC or reverse DNS.

## What this means

- **No MX, SPF, DKIM or DMARC records are required**, and none are published
  for the deployment domain. Inbound mail from the internet is not supported.
- **The host's PTR does not have to match the mail hostname.** Forward-confirmed
  reverse DNS only matters for outbound internet mail.
- **Stalwart does not relay for unauthenticated clients** to non-local
  recipients (`550 5.1.2 Relay not allowed`). That is the intended posture: the
  only clients allowed to hand it mail are local services and authenticated
  mailbox users.

## What works

- IMAP, SMTP submission, and implicit TLS on 993/465 with a publicly trusted
  certificate. The certificate is taken from the edge ACME store by
  [`scripts/sync-mail-cert.py`](../scripts/sync-mail-cert.py), refreshed by
  `opensme-mail-cert.timer`, and mounted read-only into the container.
- Webmail and internal delivery between local accounts.
- Application notifications addressed to local mailboxes.

## Known consequence

Booking invitations from `calrs` to **external** guests are refused with
`550 Relay not allowed`, because it submits unauthenticated and the recipient
is not local. Bookings themselves still succeed. See
[`images/calrs/README.md`](../images/calrs/README.md).

## Opening it later

If internet mail becomes a requirement, it is a small project with an external
dependency (DNS and reverse-DNS control at the hosting provider):

1. Publish MX, SPF, DKIM and DMARC records for the mail domain.
2. Add a `[signature]` block to the Stalwart config and publish the selector's
   TXT record.
3. Have the provider point the host's PTR at the mail hostname.
4. Give `calrs` an authenticated submission credential instead of the
   unauthenticated `stalwart:25` path.
5. Re-run the mail E2E profile and verify with an external mailbox.

Until all five are done, mail stays internal — enabling only some of them
(relaying without SPF/DKIM/PTR) makes failures *silent* (junk folder) instead
of visible (`550`), which is strictly worse.
