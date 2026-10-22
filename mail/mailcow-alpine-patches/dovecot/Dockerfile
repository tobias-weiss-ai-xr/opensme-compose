# Dovecot mailcow CVE-Patch: alp upgrade innerhalb Basis (3.21.6 -> 3.21.9)
FROM ghcr.io/mailcow/dovecot:2.3.21.1-2

# apk-deps: ohne Internet im Build? -> Bei dovecot erreicht die Basis dl-cdn (simulate zeigte Upgrades)
# Proxy ENV fuer fallback
ENV http_proxy=http://137.248.1.2:3128 https_proxy=http://137.248.1.2:3128
RUN apk update && apk upgrade --available
