#!/usr/bin/env bash
#-actions openSME Invoice Ninja Demo Provisioning
#
# Single-command bootstrap that seeds a GraphWiz AI demo tenant
# inside the running Invoice Ninja stack by sending SQL directly to the
# MariaDB database container (the FPM image ships no mysql client,
# artisan offers no database-manipulation commands, and plain `php -r`
# cannot reach the Laravel models without a booted app).
# Idempotent and safe to re-run.
#
# Schema notes (Invoice Ninja v5 as deployed here):
#   - Company profile fields (name, address, phone, email, website,
#     vat_number, colors) live inside the JSON `settings` column of the
#     `companies` table; there is no `company_settings` table.
#   - `tax_rates` has no `is_compound`/`type_id` columns.
#   - `products` has no `name` column: the display name is `product_key`,
#     the description is `notes`, the default line tax is
#     `tax_name1`/`tax_rate1`.
#   - `projects` uses `budgeted_amount`/`task_rate`/`budgeted_hours`
#     (there are no `budget`/`rate` columns).
#   - `clients` has no `email` column; contact emails live in
#     `client_contacts` (with NOT NULL company_id/client_id/user_id/
#     password).
#   - `clients`, `products`, `projects`, `tax_rates` all require
#     `company_id` (+ `user_id` on most), which raw SQL must provide.
#
# Usage (from repository root):
#   ./scripts/provision-invoice-demo.sh              # create demo data
#   ./scripts/provision-invoice-demo.sh --clean     # remove demo data
#   ./scripts/provision-invoice-demo.sh --force     # clean + recreate
#   ./scripts/provision-invoice-demo.sh --dry-run   # show commands only
#   ./scripts/provision-invoice-demo.sh --project P # use compose project name P
#
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd -P)"
cd "$REPO_ROOT"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info() { echo -e "${BLUE}[invoice-demo]${NC} $*"; }
ok()   { echo -e "${GREEN}[invoice-demo]${NC} $*"; }
warn() { echo -e "${YELLOW}[invoice-demo]${NC} $*"; }
err()  { echo -e "${RED}[invoice-demo]${NC} $*" >&2; }
die() { err "$*"; exit 1; }

FORCE=false; DRY_RUN=false; CLEAN=false; VERBOSE=false
PROJECT="${COMPOSE_PROJECT_NAME:-opensme}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --force)      FORCE=true  ;;
        --dry-run)    DRY_RUN=true ;;
        --clean)      CLEAN=true  ;;
        --verbose|-v) VERBOSE=true ;;
        --project)    PROJECT="$2"; shift ;;
        --help|-h)
            echo "Usage: $0 [OPTIONS]"
            echo "Provision a GraphWiz AI demo tenant in Invoice Ninja."
            echo "Options:"
            echo "  --force        Clean and re-provision even if already set up"
            echo "  --dry-run      Print commands but do not execute them"
            echo "  --clean        Remove demo objects instead"
            echo "  --project NAME Docker Compose project name (default: opensme)"
            echo "  --verbose, -v  Verbose output"
            echo "  --help, -h     Show this help"
            exit 0 ;;
        *) die "Unknown option: $1" ;;
    esac
    shift
done

FPM="${PROJECT}-invoiceninja"
DB="${PROJECT}-invoiceninja-db"
DB_USER="invoiceninja_user"
DB_NAME="invoiceninja_db"

# Ensure the FPM container is running
if ! docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^${FPM}\\(\|$\\)"; then
    die "Container '$FPM' is not running.
Start: docker compose --profile invoice up -d"
fi

# Ensure the MariaDB container is running.
# NOTE: the FPM image cannot run mysql/mariadb, so all SQL is executed
# inside the db container instead.
if ! docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^${DB}\\(\|$\\)"; then
    die "Container '$DB' is not running.
Start: docker compose --profile invoice up -d"
fi

# Resolve the invoiceninja DB password: environment first, then the
# deployed compose .env (read via sudo when not directly readable).
ENV_FILE="${INVOICE_DEMO_ENV_FILE:-/opt/opensme-compose/compose/.env}"
if [ "$DRY_RUN" != true ] && [ -z "${INVOICENINJA_DB_PASSWORD:-}" ]; then
    if [ -r "$ENV_FILE" ]; then
        INVOICENINJA_DB_PASSWORD="$(sed -n 's/^INVOICENINJA_DB_PASSWORD=//p' "$ENV_FILE" | head -n1 | tr -d '\r')"
    else
        INVOICENINJA_DB_PASSWORD="$(sudo -n sed -n 's/^INVOICENINJA_DB_PASSWORD=//p' "$ENV_FILE" 2>/dev/null | head -n1 | tr -d '\r' || true)"
    fi
fi

# Helper: run SQL against the invoiceninja MariaDB database inside the db
# container. All demo-data statements go through this helper.
sql() {
    local query="$1"
    if [ "$DRY_RUN" = true ]; then
        printf 'docker exec %q mariadb --default-character-set=utf8mb4 -u %q -p"***" %q -e %q\n' "$DB" "$DB_USER" "$DB_NAME" "$query" >&2
        return 0
    fi
    if [ -z "${INVOICENINJA_DB_PASSWORD:-}" ]; then
        die "INVOICENINJA_DB_PASSWORD is not set and could not be read from $ENV_FILE"
    fi
    if [ "$VERBOSE" = true ]; then
        ( set -x; docker exec "$DB" mariadb --default-character-set=utf8mb4 -u "$DB_USER" -p"$INVOICENINJA_DB_PASSWORD" "$DB_NAME" -e "$query" 2>&1 )
    else
        docker exec "$DB" mariadb --default-character-set=utf8mb4 -u "$DB_USER" -p"$INVOICENINJA_DB_PASSWORD" "$DB_NAME" -e "$query" 2>&1
    fi
}

# Execute a statement silently; status decides ok/warn reporting.
sqlq() { sql "$@" >/dev/null; }

# Helper: single-value SELECT. In dry-run mode the planned statement is
# printed and a placeholder value is returned so the plan still resolves.
lookup() {
    local query="$1"
    if [ "$DRY_RUN" = true ]; then
        printf 'docker exec %q mariadb --default-character-set=utf8mb4 -u %q -p"***" %q -N -B -e %q\n' "$DB" "$DB_USER" "$DB_NAME" "$query" >&2
        echo 1
        return 0
    fi
    if [ -z "${INVOICENINJA_DB_PASSWORD:-}" ]; then
        die "INVOICENINJA_DB_PASSWORD is not set and could not be read from $ENV_FILE"
    fi
    docker exec "$DB" mariadb --default-character-set=utf8mb4 -u "$DB_USER" -p"$INVOICENINJA_DB_PASSWORD" "$DB_NAME" -N -B -e "$query" 2>/dev/null || true
}

# Company constants
COMPANY_NAME="GraphWiz AI"
COMPANY_EMAIL="invoices@graphwiz.ai"
COMPANY_ADDRESS="Felix-Wankel-Straße 1
50829 Köln
Germany"
COMPANY_PHONE="+49 221 12345678"
COMPANY_WEBSITE="https://graphwiz.ai"
COMPANY_VAT="DE123456789"

# Demo clients (name|email|legal name|street|postal city|country)
CLIENTS=(
    "Acme Corporation|acme@example.com|Acme GmbH|Musterstraße 1|10115 Berlin|Germany"
    "Tech Solutions GmbH|info@tech-solutions.io|Tech Solutions GmbH|Startup Alley 42|80335 München|Germany"
    "Data Systems Inc|billing@datasystems.net|Data Systems Inc|Business Ave 123|10117 Berlin|Germany"
)

# Demo products (name|description|price)
PRODUCTS=(
    "AI Consulting|Expert AI/ML implementation consulting|150.00"
    "Cloud Infrastructure|Managed cloud infrastructure setup|2500.00"
    "Data Migration|Legacy data migration to modern systems|1800.00"
    "AI Training Workshop|2-day intensive AI training for teams|3500.00"
    "Premium Support|Priority support and maintenance|890.00"
    "E-Book: AI Guide|Comprehensive AI implementation guide|49.00"
)

# Demo tax rates (name|rate)
TAXES=(
    "Standard VAT|19.00"
    "Reduced VAT|7.00"
    "No VAT|0.00"
)

# Demo projects (client|name|budget in €)
PROJECTS=(
    "Acme Corporation|AI Readiness Assessment|12000.00"
    "Acme Corporation|Cloud Migration 2026|48000.00"
    "Tech Solutions GmbH|Data Platform Build|27500.00"
    "Data Systems Inc|AI Training Rollout|8500.00"
)

COLOR_SCHEME="#0e578d"
DE_COUNTRY_ID="(SELECT id FROM countries WHERE iso_3166_2 = 'DE' LIMIT 1)"

check_already_provisioned() {
    local name
    name=$(lookup "SELECT JSON_VALUE(settings, '$.name') FROM companies WHERE id = 1")
    [[ "$name" == "$COMPANY_NAME" ]]
}

###############################################################################
do_clean() {
    info "Removing demo data…"

    # Delete products (display name lives in product_key)
    for p in "${PRODUCTS[@]}"; do
        local name
        name=$(echo "$p" | cut -d'|' -f1)
        if sqlq "DELETE FROM products WHERE company_id = 1 AND product_key = '$name'"; then
            ok "Deleted product: $name"
        fi
    done

    # Delete projects, contacts and clients
    for c in "${CLIENTS[@]}"; do
        local cname cid
        cname=$(echo "$c" | cut -d'|' -f1)
        cid=$(lookup "SELECT id FROM clients WHERE company_id = 1 AND name = '$cname' LIMIT 1")
        if [ -n "$cid" ] && [ "$cid" != "0" ]; then
            sqlq "DELETE FROM client_contacts WHERE company_id = 1 AND client_id = $cid" || true
            sqlq "DELETE FROM projects WHERE company_id = 1 AND client_id = $cid" || true
        fi
        if sqlq "DELETE FROM clients WHERE company_id = 1 AND name = '$cname'"; then
            ok "Deleted client: $cname"
        fi
    done

    # Delete demo tax rates
    if sqlq "DELETE FROM tax_rates WHERE company_id = 1 AND name IN ('Standard VAT', 'Reduced VAT', 'No VAT')"; then
        ok "Deleted demo tax rates"
    fi

    # Reset company profile (fields live in the settings JSON column)
    if sqlq "
        UPDATE companies
        SET settings = JSON_SET(
                settings,
                '$.name', 'My Company',
                '$.email', 'admin@example.com',
                '$.phone', '',
                '$.website', '',
                '$.vat_number', '',
                '$.address1', '',
                '$.address2', '',
                '$.city', '',
                '$.state', '',
                '$.postal_code', '',
                '$.primary_color', '#298AAB'
            ),
            updated_at = NOW()
        WHERE id = 1
    "; then
        ok "Demo data removed"
    fi
}

###############################################################################
do_provision() {

    if ! $FORCE && check_already_provisioned; then
        info "Company '$COMPANY_NAME' already exists – use --force to recreate."
        exit 0
    fi

    info "Provisioning demo tenant: $COMPANY_NAME"

    # Owning user (first account user, usually the admin)
    local user_id
    user_id=$(lookup "SELECT id FROM users ORDER BY id LIMIT 1")

    # Company address split (street / postal city / country lines)
    local c_addr1 c_postal c_city
    c_addr1=$(printf '%s\n' "$COMPANY_ADDRESS" | sed -n '1p')
    c_postal=$(printf '%s\n' "$COMPANY_ADDRESS" | sed -n '2p' | cut -d' ' -f1)
    c_city=$(printf '%s\n' "$COMPANY_ADDRESS" | sed -n '2p' | cut -d' ' -f2-)

    ###########################################################################
    # 1. Company profile & branding
    #    Invoice Ninja v5 keeps these fields in the companies.settings JSON.
    ###########################################################################
    info "  → Company profile…"
    sql "
        UPDATE companies
        SET settings = JSON_SET(
                settings,
                '$.name', '$COMPANY_NAME',
                '$.email', '$COMPANY_EMAIL',
                '$.address1', '$c_addr1',
                '$.address2', '',
                '$.city', '$c_city',
                '$.state', '',
                '$.postal_code', '$c_postal',
                '$.country_id', '276',
                '$.phone', '$COMPANY_PHONE',
                '$.website', '$COMPANY_WEBSITE',
                '$.vat_number', '$COMPANY_VAT',
                '$.primary_color', '$COLOR_SCHEME'
            ),
            updated_at = NOW()
        WHERE id = 1
    "
    ok "Company configured"

    ###########################################################################
    # 2. Tax Rates (ensure EUR taxes exist)
    ###########################################################################
    info "  → Tax rates…"
    for t in "${TAXES[@]}"; do
        local tname trate
        tname=$(echo "$t" | cut -d'|' -f1)
        trate=$(echo "$t" | cut -d'|' -f2)
        sqlq "
            INSERT INTO tax_rates (company_id, user_id, name, rate, created_at, updated_at)
            SELECT 1, $user_id, '$tname', $trate, NOW(), NOW()
            WHERE NOT EXISTS (SELECT 1 FROM tax_rates WHERE company_id = 1 AND name = '$tname')
        " || true
    done
    ok "Tax rates configured"

    ###########################################################################
    # 3. Clients (+ primary contact carrying the email)
    ###########################################################################
    info "  → Clients…"
    for c in "${CLIENTS[@]}"; do
        local name email street pcity city postal cid
        name=$(echo "$c" | cut -d'|' -f1)
        email=$(echo "$c" | cut -d'|' -f2)
        street=$(echo "$c" | cut -d'|' -f4)
        pcity=$(echo "$c" | cut -d'|' -f5)
        postal=$(echo "$pcity" | cut -d' ' -f1)
        city=$(echo "$pcity" | cut -d' ' -f2-)

        if sqlq "
            INSERT INTO clients (company_id, user_id, name, address1, address2, city, state, postal_code, country_id, is_deleted, balance, paid_to_date, created_at, updated_at)
            SELECT 1, $user_id, '$name', '$street', '', '$city', '', '$postal', ${DE_COUNTRY_ID}, 0, 0, 0, NOW(), NOW()
            WHERE NOT EXISTS (SELECT 1 FROM clients WHERE company_id = 1 AND name = '$name')
        "; then
            cid=$(lookup "SELECT id FROM clients WHERE company_id = 1 AND name = '$name' LIMIT 1")
            if sqlq "
                INSERT INTO client_contacts (company_id, user_id, client_id, first_name, email, is_primary, send_email, password, contact_key, created_at, updated_at)
                SELECT 1, $user_id, $cid, '$name', '$email', 1, 1, '', MD5(CONCAT('contact-', $cid, '-$email')), NOW(), NOW()
                WHERE NOT EXISTS (SELECT 1 FROM client_contacts WHERE client_id = $cid AND email = '$email')
            "; then
                ok "  ✓ $name <$email>"
            else
                warn "  ✗ $name (contact)"
            fi
        else
            warn "  ✗ $name"
        fi
    done

    ###########################################################################
    # 4. Products (product_key is the display name in Invoice Ninja v5)
    ###########################################################################
    info "  → Products…"
    for p in "${PRODUCTS[@]}"; do
        local name desc price tax_name tax_rate
        name=$(echo "$p" | cut -d'|' -f1)
        desc=$(echo "$p" | cut -d'|' -f2)
        price=$(echo "$p" | cut -d'|' -f3)

        # Use reduced VAT for books, standard VAT for everything else
        if echo "$name" | grep -qi "ebook\|book\|guide"; then
            tax_name='Reduced VAT'
            tax_rate=7.00
        else
            tax_name='Standard VAT'
            tax_rate=19.00
        fi

        if sqlq "
            INSERT INTO products (company_id, user_id, product_key, notes, cost, price, quantity, tax_name1, tax_rate1, is_deleted, created_at, updated_at)
            SELECT 1, $user_id, '$name', '$desc', 0.00, $price, 9999, '$tax_name', $tax_rate, 0, NOW(), NOW()
            WHERE NOT EXISTS (SELECT 1 FROM products WHERE company_id = 1 AND product_key = '$name')
        "; then
            ok "  ✓ $name (€$price)"
        else
            warn "  ✗ $name"
        fi
    done

    ###########################################################################
    # 5. Projects
    ###########################################################################
    info "  → Projects…"
    for c in "${CLIENTS[@]}"; do
        local cname cid
        cname=$(echo "$c" | cut -d'|' -f1)
        cid=$(lookup "SELECT id FROM clients WHERE company_id = 1 AND name = '$cname' LIMIT 1")
        if [ -z "$cid" ] || [ "$cid" = "0" ]; then
            continue
        fi

        # Get matching project def
        for p in "${PROJECTS[@]}"; do
            local p_client p_name p_budget
            p_client=$(echo "$p" | cut -d'|' -f1)
            p_name=$(echo "$p" | cut -d'|' -f2)
            p_budget=$(echo "$p" | cut -d'|' -f3)

            if [[ "$p_client" == "$cname" ]]; then
                if sqlq "
                    INSERT INTO projects (company_id, user_id, client_id, name, task_rate, budgeted_hours, budgeted_amount, public_notes, private_notes, is_deleted, created_at, updated_at)
                    SELECT 1, $user_id, $cid, '$p_name', 150.00, 0, $p_budget, '', '', 0, NOW(), NOW()
                    WHERE NOT EXISTS (SELECT 1 FROM projects WHERE company_id = 1 AND name = '$p_name')
                "; then
                    ok "  ✓ $p_name (€$p_budget)"
                else
                    warn "  ✗ $p_name"
                fi
            fi
        done
    done

    ###########################################################################
    # Summary
    ###########################################################################
    info ""
    info "═══════════════════════════════════"
    info "  Demonstration tenant configured:"
    info "  Company : $COMPANY_NAME <$COMPANY_EMAIL>"
    info "  Clients : ${#CLIENTS[@]}"
    info "  Products: ${#PRODUCTS[@]}"
    info "  Projects: ${#PROJECTS[@]}"
    info "═══════════════════════════════════"
    ok "Provisioning complete!"
}

###############################################################################
# Main
###############################################################################

if $CLEAN; then
    do_clean
elif $FORCE; then
    do_clean
    do_provision
else
    do_provision
fi
