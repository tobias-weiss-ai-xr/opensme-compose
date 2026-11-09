#!/usr/bin/env bash
#-actions openSME Invoice Ninja Demo Provisioning
#
# Single-command bootstrap that seeds a GraphWiz AI demo tenant
# inside the running Invoice Ninja service via in-container artisan calls.
# Idempotent and safe to re-run.
#
# Usage (from repository root):
#   ./scripts/provision-invoice-demo.sh              # create demo data
#   ./scripts/provision-invoice-demo.sh --clean     # remove demo data
#   ./scripts/provision-invoice-demo.sh --force     # recreate even if already present
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
            echo "  --force        Re-provision even if already set up"
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

# Ensure the FPM container is running
if ! docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^${FPM}\\(\|$\\)"; then
    die "Container '$FPM' is not running.
Start: docker compose --profile invoice up -d"
fi

# Helper: execute artisan command inside FPM container
art() {
    docker exec $DRY_RUN $VERBOSE $FPM php /var/www/app/artisan "$@" 2>&1
}

# Helper: run PHP one-liner inside FPM container
php_exec() {
    docker exec $DRY_RUN $VERBOSE $FPM php -r "$1" 2>&1
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

# Demo clients
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

COLOR_SCHEME="0e578d"

check_already_provisioned() {
    local name
    name=$(php_exec "\$c = \\App\\Models\\Company::first(); echo trim(\$c ? \$c->name : '');" 2>/dev/null | tr -d '\n')
    [[ "$name" == "$COMPANY_NAME" ]]
}

###############################################################################
do_clean() {
    info "Removing demo data…"
    
    # Delete products
    for p in "${PRODUCTS[@]}"; do
        local name
        name=$(echo "$p" | cut -d'|' -f1)
        art db:delete "DELETE FROM products WHERE name = '$name'" 2>/dev/null | grep -q . && ok "Deleted product: $name" || true
    done
    
    # Delete projects and clients
    for c in "${CLIENTS[@]}"; do
        local cname
        cname=$(echo "$c" | cut -d'|' -f1)
        local cid
        cid=$(php_exec "\$cl = \\App\\Models\\Client::where('name','$cname')->first(); echo \$cl ? \$cl->id : 0;" 2>/dev/null | tr -d '\n')
        [[ "$cid" != "0" ]] && art db:delete "DELETE FROM projects WHERE client_id = $cid" 2>/dev/null
        art db:delete "DELETE FROM clients WHERE name = '$cname'" 2>/dev/null | grep -q . && ok "Deleted client: $cname" || true
    done
    
    # Reset company name
    art db:update "UPDATE companies SET name='My Company', email='admin@example.com' WHERE id=1" 2>/dev/null
    ok "Demo data removed"
}

###############################################################################
do_provision() {

    if ! $FORCE && check_already_provisioned; then
        info "Company '$COMPANY_NAME' already exists – use --force to recreate."
        exit 0
    fi

    info "Provisioning demo tenant: $COMPANY_NAME"

    ###########################################################################
    # 1. Company
    ###########################################################################
    info "  → Company profile…"
    art db:update "
        UPDATE companies 
        SET 
            name = '$COMPANY_NAME',
            email = '$COMPANY_EMAIL',
            address = '${COMPANY_ADDRESS//$'\n'/\\n}',
            phone = '$COMPANY_PHONE',
            website = '$COMPANY_WEBSITE',
            vat_number = '$COMPANY_VAT',
            custom_value1 = '',
            custom_value2 = ''
        WHERE id = 1
    " 2>/dev/null
    ok "Company configured"

    ###########################################################################
    # 2. Company Settings / Branding
    ###########################################################################
    info "  → Company settings…"
    # Set color scheme and logo via settings
    art db:insert "
        INSERT INTO company_settings (company_id, settings_key, settings_value)
        VALUES (1, 'invoice_color', '$COLOR_SCHEME')
        ON CONFLICT (company_id, settings_key) DO UPDATE SET settings_value = EXCLUDED.settings_value
    " 2>/dev/null || true
    ok "Settings saved"

    ###########################################################################
    # 3. Tax Rates (ensure EUR taxes exist)
    ###########################################################################
    info "  → Tax rates…"
    art db:insert "
        INSERT INTO tax_rates (name, rate, is_compound, type_id)
        VALUES ('Standard VAT', 19.00, false, 1),
               ('Reduced VAT', 7.00, false, 1),
               ('No VAT', 0.00, false, 1)
        ON CONFLICT (name) DO NOTHING
    " 2>/dev/null || true
    ok "Tax rates configured"

    ###########################################################################
    # 4. Clients
    ###########################################################################
    info "  → Clients…"
    for c in "${CLIENTS[@]}"; do
        local name email org street city postal country
        name=$(echo "$c" | cut -d'|' -f1)
        email=$(echo "$c" | cut -d'|' -f2)
        org=$(echo "$c" | cut -d'|' -f3)
        street=$(echo "$c" | cut -d'|' -f4)
        city=$(echo "$c" | cut -d'|' -f5)
        postal=$(echo "$c" | cut -d'|' -f6)
        country=$(echo "$c" | cut -d'|' -f7)
        address="$street\n$postal $city\n$country"

        art db:insert "
            INSERT INTO clients (id, user_id, assigned_user_id, name, email, address1, address2, city, state, postal_code, country_id, currency_id, custom_value1, custom_value2, custom_value3, custom_value4, is_deleted, balance, paid_to_date, last_login, size_id, industry_id, private_notes, public_notes, website, phone, created_at, updated_at)
            VALUES (
                DEFAULT, NULL, NULL, '$name', '$email', '${address//$'\n'/\\n}', '', '$city', '', '$postal', (SELECT id FROM countries WHERE iso_3166_2 = 'DE' LIMIT 1), 1, NULL, NULL, NULL, NULL, 0, 0.00, 0.00, NULL, NULL, NULL, NULL, NULL, NULL, NOW(), NOW()
            )
            ON CONFLICT (email) DO UPDATE
               SET name = EXCLUDED.name,
                   email = EXCLUDED.email,
                   address1 = EXCLUDED.address1,
                   city = EXCLUDED.city,
                   postal_code = EXCLUDED.postal_code,
                   country_id = EXCLUDED.country_id,
                   updated_at = NOW()
            RETURNING id
        " 2>/dev/null | while read -r cid; do
            [[ -n "$cid" && "$cid" != "0" ]] && ok "  ✓ $name"
        done
    done

    ###########################################################################
    # 5. Products
    ###########################################################################
    info "  → Products…"
    local tax_id_standard
    tax_id_standard=$(php_exec "\$t = \\App\\Models\\TaxRate::where('rate', 19.00)->first(); echo \$t ? \$t->id : 1;" 2>/dev/null | tr -d '\n')
    tax_id_standard=${tax_id_standard:-1}

    local tax_id_reduced
    tax_id_reduced=$(php_exec "\$t = \\App\\Models\\TaxRate::where('rate', 7.00)->first(); echo \$t ? \$t->id : 2;" 2>/dev/null | tr -d '\n')
    tax_id_reduced=${tax_id_reduced:-2}

    local tax_id_none
    tax_id_none=$(php_exec "\$t = \\App\\Models\\TaxRate::where('rate', 0.00)->first(); echo \$t ? \$t->id : 3;" 2>/dev/null | tr -d '\n')
    tax_id_none=${tax_id_none:-3}

    for p in "${PRODUCTS[@]}"; do
        local name desc price tax_id
        name=$(echo "$p" | cut -d'|' -f1)
        desc=$(echo "$p" | cut -d'|' -f2)
        price=$(echo "$p" | cut -d'|' -f3)
        
        # Use standard VAT for most, reduced for books
        if echo "$name" | grep -qi "ebook\|book\|guide"; then
            tax_id=$tax_id_reduced
        else
            tax_id=$tax_id_standard
        fi

        art db:insert "
            INSERT INTO products (id, user_id, name, description, product_key, notes, cost, price, quantity, tax_id, tax_name, created_at, updated_at, product_type_id)
            VALUES (
                DEFAULT, NULL, '$name', '$desc', LOWER(REGEXP_REPLACE('${name// /_}', '[^a-zA-Z0-9_\-]', '', 'g')), '', 0.00, $price, 9999, $tax_id, (SELECT name FROM tax_rates WHERE id = $tax_id LIMIT 1), NOW(), NOW(), 1
            )
            ON CONFLICT (product_key) DO UPDATE
               SET name = EXCLUDED.name,
                   description = EXCLUDED.description,
                   price = EXCLUDED.price,
                   tax_id = EXCLUDED.tax_id,
                   updated_at = NOW()
            RETURNING id
        " 2>/dev/null | while read -r pid; do
            [[ -n "$pid" ]] && ok "  ✓ $name (€$price)"
        done
    done

    ###########################################################################
    # 6. Projects
    ###########################################################################
    info "  → Projects…"
    for c in "${CLIENTS[@]}"; do
        local cname
        cname=$(echo "$c" | cut -d'|' -f1)
        local cid
        cid=$(php_exec "\$cl = \\App\\Models\\Client::where('name','$cname')->first(); echo \$cl ? \$cl->id : 0;" 2>/dev/null | tr -d '\n')
        [[ "$cid" == "0" ]] && continue

        # Get matching project def
        for p in "${PROJECTS[@]:-${PROJECTS[0]}}"; do
            local p_client p_name p_budget
            p_client=$(echo "$p" | cut -d'|' -f1)
            p_name=$(echo "$p" | cut -d'|' -f2)
            p_budget=$(echo "$p" | cut -d'|' -f3)
            
            if [[ "$p_client" == "$cname" ]]; then
                art db:insert "
                    INSERT INTO projects (id, user_id, client_id, name, private_notes, public_notes, budget, rate, created_at, updated_at, is_deleted)
                    VALUES (
                        DEFAULT, NULL, $cid, '$p_name', '', '', $p_budget, 150.00, NOW(), NOW(), 0
                    )
                    ON CONFLICT (client_id, name) DO UPDATE
                       SET budget = EXCLUDED.budget,
                           rate = EXCLUDED.rate,
                           updated_at = NOW()
                    RETURNING id
                " 2>/dev/null | while read -r proj_id; do
                    [[ -n "$proj_id" ]] && ok "  ✓ $p_name (€$p_budget)"
                done
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
elif $FORCE || ! check_already_provisioned; then
    do_provision
else
    info "Company '$COMPANY_NAME' already provisioned – use --force to recreate or --clean to remove."
fi
