# opensme/paperless-ngx — upstream paperless-ngx image with the django-allauth
# generic OIDC provider app enabled.
#
# WHY THIS EXISTS (2026-11-01): paperless-ngx 3.3.4 ships allauth (account,
# socialaccount, mfa, headless) and reads PAPERLESS_SOCIALACCOUNT_PROVIDERS,
# but its INSTALLED_APPS does NOT include allauth.socialaccount.providers.openid_connect.
# In allauth ≥65 the provider registry is populated only from INSTALLED_APPS
# app modules (allauth/socialaccount/providers/registry.py `load()`), so with
# the stock image the registry is empty, /accounts/oidc/... resolves to a base
# route and SSO is dead (discovered live: 302→login until the provider class
# was registered). There is no env hook in paperless settings.py to add apps,
# so we patch INSTALLED_APPS at image build time — one line, upstream-following.
#
# Pin the upstream tag explicitly: SSO depends on the allauth wiring of a
# specific paperless release; :latest moves under us.
#
# Build:  make paperless-oidc-image
#         (docker build -t opensme/paperless-ngx:3.3.4-oidc -f images/paperless-oidc/Dockerfile images/paperless-oidc)

FROM ghcr.io/paperless-ngx/paperless-ngx:3.3.4

# Trust anchor for the OIDC backchannel: allauth fetches discovery/token/
# userinfo against https://auth.<domain> (extra_hosts -> host-gateway ->
# traefik), which serves the self-signed *.opensme.local lab wildcard. Bake
# it into the SYSTEM bundle (update-ca-certificates) so requests' default
# verify path trusts lab AND public CAs — a REQUESTS_CA_BUNDLE override
# would REPLACE the bundle and break Let's-Encrypt deployments.
# BuildKit named context (see make paperless-oidc-image) pulls the committed
# fixture in without widening the build context. Rotation: regenerate via
# scripts/gen-lab-cert.sh, then REBUILD this image.
COPY --from=lab-ca selfsigned.crt /usr/local/share/ca-certificates/opensme-lab.crt
RUN update-ca-certificates

# Enable the generic OIDC provider app (allauth >=65 loads providers INSTALLED_APPS).
# chr(10) avoids literal-newline escaping problems in the build shell.
RUN python3 -c "p='/usr/src/paperless/src/paperless/settings/__init__.py'; s=open(p).read(); old='    \"allauth.socialaccount\",'; new=old+chr(10)+'    \"allauth.socialaccount.providers.openid_connect\",'; assert old in s, 'anchor missing'; open(p,'w').write(s.replace(old,new,1))" && grep -n "allauth.socialaccount.providers.openid_connect" /usr/src/paperless/src/paperless/settings/__init__.py
