#!/bin/sh
# OpenCloud entrypoint — generate config on first run.
set -e

if [ ! -f /etc/opencloud/opencloud.yaml ]; then
  echo "opencloud-entrypoint: no config found, running init..." >&2
  opencloud init -f --insecure=true
  echo "opencloud-entrypoint: config generated." >&2
fi

# Render the CSP file from the template on every start. The upstream default
# CSP refuses to frame the IdP (needed for oidc silent-token renewal), so we
# inject the IdP origin into child-src/connect-src/frame-src. The issuer
# (OC_OIDC_ISSUER) is the bare origin; strip any trailing path.
IDP_ORIGIN="${OC_OIDC_ISSUER:-}"
case "$IDP_ORIGIN" in
  *://*)
    IDP_ORIGIN="${IDP_ORIGIN%%/*}"   # drop path (keeps scheme://host[:port])
    ;;
esac
if [ -f /etc/opencloud/csp.tpl.yaml ] && [ -n "$IDP_ORIGIN" ]; then
  sed "s|__IDP_ORIGIN__|$IDP_ORIGIN|g" \
      /etc/opencloud/csp.tpl.yaml > /etc/opencloud/csp.yaml
  echo "opencloud-entrypoint: csp.yaml rendered (idp=$IDP_ORIGIN)" >&2
elif [ -f /etc/opencloud/csp.tpl.yaml ]; then
  # No issuer configured — fall back to the upstream default (no IdP origin).
  sed "/__IDP_ORIGIN__/d" /etc/opencloud/csp.tpl.yaml > /etc/opencloud/csp.yaml
  echo "opencloud-entrypoint: csp.yaml rendered without idp origin" >&2
fi

exec opencloud server
