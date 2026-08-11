#!/bin/bash
set -euo pipefail

BENCH="/home/frappe/bench"
DB_HOST="${DB_HOST:-mariadb}"
DB_PORT="${DB_PORT:-3306}"
DB_ROOT_PASSWORD="${DB_ROOT_PASSWORD:-root}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-admin}"
REDIS_CACHE="${REDIS_CACHE:-redis://redis-cache:6379}"
REDIS_QUEUE="${REDIS_QUEUE:-redis://redis-queue:6379}"
VENDOR_SITES="${VENDOR_SITES:-vendor1.localhost vendor2.localhost vendor3.localhost}"
PORT="${VENDOR_PORT:-8000}"
# Shared secret vendor sites sign their hub pushes with (X-SM-Secret header).
# Must match the hub's Settings.webhook_secret — see saathimart/docker/init.sh.
WEBHOOK_SECRET="${WEBHOOK_SECRET:-saathimart-webhook-secret}"

wait_for() {
  local host=$1 port=$2 label=$3
  echo "Waiting for $label..."
  for i in $(seq 1 90); do
    if nc -z "$host" "$port" 2>/dev/null; then echo "$label ready."; return; fi
    [ "$i" -eq 90 ] && echo "ERROR: $label not ready after 90s" && exit 1
    sleep 2
  done
}

wait_for "$DB_HOST"    "$DB_PORT" "MariaDB"
wait_for "redis-cache" "6379"     "Redis Cache"
wait_for "redis-queue" "6379"     "Redis Queue"

cd "$BENCH"

# Ensure all mounted directories are writable by the frappe user.
# Docker named volumes are created as root; bench runs as frappe.
mkdir -p "$BENCH/sites" "$BENCH/sites/assets" "$BENCH/logs" "/home/frappe/logs"
chown -R frappe:frappe "$BENCH/sites" "$BENCH/logs" "/home/frappe/logs"

# Create logs directory for each vendor site
for SITE in $VENDOR_SITES; do
  mkdir -p "$BENCH/sites/$SITE/logs"
  chown -R frappe:frappe "$BENCH/sites/$SITE/logs"
done

# Init bench only if a previous attempt fully completed (partial/failed
# attempts leave apps/frappe on disk but must not be mistaken for done,
# or bench's interactive "overwrite?" prompt aborts with no stdin attached)
if [ ! -f "$BENCH/.frappe_installed" ]; then
  rm -rf "$BENCH/apps/frappe"
  echo "Initialising bench with Frappe v16..."
  bench init "$BENCH" \
    --frappe-branch version-16 \
    --no-procfile \
    --skip-redis-config-generation \
    --ignore-exist
  touch "$BENCH/.frappe_installed"
fi

if [ ! -f "$BENCH/.erpnext_installed" ]; then
  rm -rf "$BENCH/apps/erpnext"
  echo "Getting ERPNext v16..."
  bench get-app erpnext --branch version-16
  touch "$BENCH/.erpnext_installed"
fi

# Link saathimart_vendor app and register it as an editable install, same
# as bench does for apps it clones itself (symlinking alone never puts the
# app on the venv's Python path)
if [ ! -d "$BENCH/apps/saathimart_vendor" ]; then
  ln -s /home/frappe/saathimart_vendor "$BENCH/apps/saathimart_vendor"
  echo "saathimart_vendor" >> "$BENCH/apps/apps.txt"
fi
if ! "$BENCH/env/bin/python" -c "import saathimart_vendor" >/dev/null 2>&1; then
  uv pip install --quiet --no-deps -e "$BENCH/apps/saathimart_vendor" --python "$BENCH/env/bin/python"
fi
# frappe.get_all_apps() (used by install-app) reads sites/apps.txt, not
# apps/apps.txt — bench get-app maintains this automatically, but a
# manually symlinked local app needs it added explicitly
if ! grep -qx "saathimart_vendor" "$BENCH/sites/apps.txt" 2>/dev/null; then
  # apps.txt may not end in a newline — appending blindly would merge
  # onto the last line (e.g. "erpnextsaathimart_vendor")
  [ -s "$BENCH/sites/apps.txt" ] && [ -n "$(tail -c1 "$BENCH/sites/apps.txt")" ] && echo >> "$BENCH/sites/apps.txt"
  echo "saathimart_vendor" >> "$BENCH/sites/apps.txt"
fi

bench set-config -g redis_cache    "$REDIS_CACHE"
bench set-config -g redis_queue    "$REDIS_QUEUE"
bench set-config -g redis_socketio "$REDIS_CACHE"
bench set-config -g gunicorn_bind "0.0.0.0:8000"
bench set-config -g db_host "$DB_HOST"

# Create / migrate each vendor site. install-app is idempotent (no-op if
# already installed), so it always runs to recover sites left with a
# missing app install by a prior failed attempt.
SITE_INDEX=0
for SITE in $VENDOR_SITES; do
  SITE_INDEX=$((SITE_INDEX + 1))
  if [ ! -d "$BENCH/sites/$SITE" ] || [ ! -f "$BENCH/sites/$SITE/site_config.json" ]; then
    echo "Creating site $SITE..."
    rm -rf "$BENCH/sites/$SITE"
    bench new-site "$SITE" \
      --mariadb-root-password "$DB_ROOT_PASSWORD" \
      --admin-password "$ADMIN_PASSWORD" \
      --db-host "$DB_HOST" \
      --no-mariadb-socket
    touch "$BENCH/sites/$SITE/.installed"
  fi

  bench --site "$SITE" install-app erpnext
  bench --site "$SITE" install-app saathimart_vendor || true
  # Migrate on every start, not just the first — app code (new doctypes,
  # fields, fixtures) changes far more often than the site itself, and
  # `bench migrate` is safe/idempotent to re-run with nothing new to do.
  echo "Running migrations for $SITE..."
  bench --site "$SITE" migrate

  # Configure Vendor Config for this site
  #
  # Must run with cwd = $BENCH/sites: Frappe's per-site log handler builds
  # its file path as a *relative* join of site + "logs" + logfile (see
  # frappe/utils/logger.py:create_handler), so frappe.connect() throws
  # FileNotFoundError from anywhere else instead of writing to the real
  # sites/<site>/logs directory.
  VENDOR_ID="${SITE}"
  echo "Configuring Vendor Config for $VENDOR_ID..."
  cd "$BENCH/sites" && "$BENCH/env/bin/python" - "$SITE" "$VENDOR_ID" "$SITE_INDEX" "$WEBHOOK_SECRET" <<'PYEOF'
import sys
import frappe
site = sys.argv[1]
vendor_id = sys.argv[2]
site_index = int(sys.argv[3])
webhook_secret = sys.argv[4]
frappe.init(site, sites_path='/home/frappe/bench/sites')
frappe.connect()
config = frappe.get_single('Vendor Config')
config.hub_url = 'http://hub:8000'
config.hub_site = 'saathimart.localhost'
config.vendor_id = vendor_id
config.api_key = f'vendor-api-key-{vendor_id}'
config.api_secret = f'vendor-api-secret-{vendor_id}'
config.webhook_secret = webhook_secret
config.sync_enabled = 1
config.reconciliation_enabled = 1

# Set vendor location based on site name when it names a known area (for
# demo/testing); otherwise round-robin a small set of distinct Kathmandu
# points keyed by this site's position in VENDOR_SITES, so the default
# vendor1/vendor2/vendor3 names don't all collapse onto the same coordinates
# and defeat nearest-vendor distance selection on the hub.
# In production, these would be set by the vendor via desk or API instead.
KNOWN_AREAS = {
    'baneshwor': (27.7172, 85.3240, 'Baneshwor, Kathmandu'),
    'koteshwor': (27.7042, 85.3434, 'Koteshwor, Kathmandu'),
    'thamel':    (27.7172, 85.3106, 'Thamel, Kathmandu'),
}
ROUND_ROBIN_POINTS = [
    (27.7172, 85.3240, 'Baneshwor, Kathmandu'),
    (27.7042, 85.3434, 'Koteshwor, Kathmandu'),
    (27.7172, 85.3106, 'Thamel, Kathmandu'),
    (27.6939, 85.2827, 'Kalanki, Kathmandu'),
    (27.6710, 85.4298, 'Bhaktapur'),
]

matched = next((v for k, v in KNOWN_AREAS.items() if k in vendor_id.lower()), None)
if matched is None:
    matched = ROUND_ROBIN_POINTS[(site_index - 1) % len(ROUND_ROBIN_POINTS)]

config.lat, config.lng, config.address = matched
config.service_radius_km = 5

# default_warehouse is the single warehouse Sales Orders get fulfilled
# from and stock levels get read from (see utils.py/tasks.py/vendor_order.py)
# — ERPNext requires exactly one warehouse per order line, so "all of the
# vendor's stock" isn't a valid substitute. If this vendor already has a
# real warehouse (a pre-existing site with its own Company), use that
# instead of ever touching/creating our own.
existing_warehouse = frappe.db.get_value(
    'Warehouse', {'disabled': 0}, 'name', order_by='creation asc'
)

if existing_warehouse:
    config.default_warehouse = existing_warehouse
else:
    # Nothing exists yet (fresh demo site: no Setup Wizard has ever run
    # here, so there's no Company/Warehouse to point at at all). Bootstrap
    # the two base fixtures actually needed — Company.on_update's
    # create_default_warehouses() needs "Warehouse Type: Transit", and
    # accept_order()'s Sales Order needs a Price List ("Standard Selling").
    # erpnext.setup.setup_wizard.operations.install_fixtures.install()
    # would normally provide both (it's what the Setup Wizard calls), but
    # it hits a NestedSetRecursionError on its Sales Person fixture on this
    # ERPNext version — create just what's needed instead of pulling in
    # that whole (currently broken) installer.
    if not frappe.db.exists('Warehouse Type', 'Transit'):
        frappe.get_doc({'doctype': 'Warehouse Type', 'name': 'Transit'}).insert(ignore_permissions=True)
    for pl_name, buying, selling in [('Standard Buying', 1, 0), ('Standard Selling', 0, 1)]:
        if not frappe.db.exists('Price List', pl_name):
            frappe.get_doc({
                'doctype': 'Price List', 'price_list_name': pl_name, 'enabled': 1,
                'buying': buying, 'selling': selling, 'currency': 'NPR',
            }).insert(ignore_permissions=True)
    frappe.db.commit()

    company_name = 'SaathiMart Vendor'
    abbr = 'SM'
    if not frappe.db.exists('Company', company_name):
        frappe.get_doc({
            'doctype': 'Company',
            'company_name': company_name,
            'abbr': abbr,
            'default_currency': 'NPR',
            'country': 'Nepal',
        }).insert(ignore_permissions=True)
        frappe.db.commit()

    # accept_order() submits a Sales Order, which requires an active Fiscal
    # Year covering today — Company.insert() doesn't create one on its own,
    # unlike the standard warehouses.
    import datetime
    year = datetime.date.today().year
    if not frappe.db.exists('Fiscal Year', {
        'year_start_date': ['<=', frappe.utils.today()], 'year_end_date': ['>=', frappe.utils.today()],
    }):
        frappe.get_doc({
            'doctype': 'Fiscal Year',
            'year': str(year),
            'year_start_date': f'{year}-01-01',
            'year_end_date': f'{year}-12-31',
        }).insert(ignore_permissions=True)
        frappe.db.commit()

    config.default_warehouse = f'Stores - {abbr}'

config.save(ignore_permissions=True)
frappe.db.commit()
print(f'  Vendor Config saved for {vendor_id}')
PYEOF
  cd "$BENCH"
done

# Sync vendor location to hub. Runs once, after every site above has been
# created/configured — nesting this inside the site-provisioning loop
# would (and used to) try to sync not-yet-created later sites too early.
echo "Syncing vendor locations to hub..."
for SITE in $VENDOR_SITES; do
  cd "$BENCH" && "$BENCH/env/bin/python" - "$SITE" <<'PYEOF'
import sys
import frappe
site = sys.argv[1]
frappe.init(site, sites_path='/home/frappe/bench/sites')
frappe.connect()
from saathimart_vendor.api.mapping import sync_vendor_location
try:
    result = sync_vendor_location()
    print(f'  Location synced for {site}: {result}')
except Exception as e:
    print(f'  Location sync failed for {site}: {e}')
PYEOF
done

bench build --app saathimart_vendor || true

# `frappe.app:application` (the raw WSGI callable gunicorn would otherwise
# import) never serves /assets or /files — that middleware only gets
# applied inside frappe.app.serve(), which a bare gunicorn import never
# calls. This compose stack has no nginx in front to serve those paths
# from disk instead, so wrap the app here or every JS/CSS asset 404s.
cat > "$BENCH/sites/gunicorn_wsgi.py" <<'EOF'
import frappe.app
application = frappe.app.application_with_statics()
EOF

# Create Procfile for bench start with full gunicorn path
cat > "$BENCH/Procfile" <<'EOF'
web: cd /home/frappe/bench/sites && /home/frappe/bench/env/bin/gunicorn --bind 0.0.0.0:8000 gunicorn_wsgi:application
EOF

echo "=== Starting vendor sites on port $PORT ==="
for SITE in $VENDOR_SITES; do
  echo "  $SITE → http://localhost:$PORT"
done

exec bench start