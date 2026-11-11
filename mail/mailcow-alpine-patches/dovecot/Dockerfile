# Dovecot mailcow CVE-Patch: alp upgrade innerhalb Basis (3.21.6 -> 3.21.9)
FROM ghcr.io/mailcow/dovecot:2.3.21.1-2

# apk-deps: ohne Internet im Build? -> Bei dovecot erreicht die Basis dl-cdn (simulate zeigte Upgrades)
# Build-Proxy: Docker forwards the predefined http_proxy/https_proxy build
# args to RUN steps automatically, e.g.
#   docker build --build-arg http_proxy=$HTTP_PROXY --build-arg https_proxy=$HTTPS_PROXY .
RUN apk update && apk upgrade --available
