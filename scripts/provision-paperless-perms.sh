#!/usr/bin/env bash
# provision-paperless-perms.sh — idempotent paperless permission bootstrap.
#
# Why: paperless-ngx creates OIDC SSO users with ZERO groups and zero model
# permissions. Every API call for a fresh SSO user answers
#   403 {"detail":"You do not have permission to perform this action."}
# even though the login itself succeeds. Upstream (paperless-ngx ≥2.5)
# mitigates FUTURE users via PAPERLESS_SOCIAL_ACCOUNT_DEFAULT_GROUPS — but
# the group still has to EXIST with sensible permissions, and users created
# before the env var was set need a backfill.
#
# This script (safe to re-run):
#   1. get_or_create the group named ${1:-oidc-users}
#   2. grants it view/add/change on every documents-app model except
#      workflow objects (view-only there — workflow edits are admin work)
#   3. backfills: assigns the group to all existing users (demo stance:
#      every seeded/OIDC member gets archive access)
#
# Usage:
#   scripts/provision-paperless-perms.sh [group-name]
# Env:
#   PAPERLESS_CONTAINER (default opensme-paperless)
#
# The compose service additionally sets
#   PAPERLESS_SOCIAL_ACCOUNT_DEFAULT_GROUPS=<group>
# so users created AFTER this run land in the group automatically.
set -euo pipefail

GROUP="${1:-oidc-users}"
CONTAINER="${PAPERLESS_CONTAINER:-opensme-paperless}"

docker exec "$CONTAINER" python3 manage.py shell -c "
from django.contrib.auth.models import Group, Permission, User
from django.contrib.contenttypes.models import ContentType

GROUP_NAME = '${GROUP}'
ADMIN_MODELS = {'workflow', 'workflowtrigger', 'workflowaction', 'workflowtriggerfilename'}

group, created = Group.objects.get_or_create(name=GROUP_NAME)
cts = list(ContentType.objects.filter(app_label='documents'))
perms = Permission.objects.filter(content_type__in=cts)
keep = [p for p in perms
        if p.codename.startswith('view_')
        or (p.content_type.model not in ADMIN_MODELS
            and (p.codename.startswith('add_') or p.codename.startswith('change_')))]
group.permissions.set(keep)
group.save()
n_users = 0
for u in User.objects.all():
    if not u.groups.filter(name=GROUP_NAME).exists():
        u.groups.add(group)
        n_users += 1
print('paperless perms: group %r %s, %d perms, backfilled %d user(s)'
     % (GROUP_NAME, 'created' if created else 'updated', len(keep), n_users))
"
