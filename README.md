# SaathiMart Vendor

Sync layer between a vendor's ERPNext site and the SaathiMart hub. Installed
alongside ERPNext on each vendor's own Frappe bench — the hub never talks to
ERPNext directly, only to this app's whitelisted API and outbox.

## Architecture

```
Vendor ERPNext site
├── Vendor Config          (single) — hub URL, vendor ID, API keys, warehouse
├── Product Mapping                — barcode ↔ ERPNext Item ↔ hub Product
├── Sync Outbox                    — transactional outbox of events to push
├── Vendor Order                   — orders received from the hub
│
├── hooks/stock.py   — Purchase Receipt / Sales Invoice / Stock Entry
│                       → enqueue stock.receipt / stock.deduct / stock.adjustment
├── hooks/orders.py   — Sales Order / Delivery Note
│                       → enqueue order.confirmed / order.dispatched
├── api/receive.py    — hub pushes order.new / order.cancel / order.reassign here
├── api/mapping.py    — barcode lookup, CSV bulk import, re-sync unmapped items
└── tasks.py           — flush_outbox (1 min), check_hub_health (5 min),
                          reconcile_stock (hourly)
```

Events flow through `Sync Outbox` in the same DB transaction as the ERPNext
document that triggered them (transactional outbox pattern), so nothing is
lost if the hub is unreachable — `flush_outbox` retries with exponential
backoff and marks an entry `Dead` (and emails `admin_email`) after 10 failed
attempts.

## Key doctypes

| Doctype | Purpose |
|---------|---------|
| `Vendor Config` | Single doctype — hub URL, vendor ID, API key/secret, webhook secret, default warehouse, lat/lng, reconciliation thresholds |
| `Product Mapping` | Links a physical barcode → ERPNext `Item` → hub `Product` (`hub_product_id`, `hub_sku`) |
| `Sync Outbox` | Pending/Sent/Failed/Dead queue of outbound events (`stock.*`, `price.update`, `order.*`) |
| `Vendor Order` | Orders pushed from the hub; lifecycle `Received → Accepted → Dispatched → Delivered` (or `Cancelled`) |

## Key API endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `saathimart_vendor.api.receive.receive_from_hub` | Hub pushes `order.new` / `order.cancel` / `order.reassign` |
| GET | `saathimart_vendor.api.mapping.lookup_barcode` | Scan a barcode → ask hub what product it is |
| POST | `saathimart_vendor.api.mapping.bulk_import` | CSV `item_code,barcode` → creates + syncs Product Mapping rows |
| POST | `saathimart_vendor.api.mapping.sync_all_unmapped` | Re-try hub lookup for Unmapped/Error mappings |

Vendor Order also exposes whitelisted doc methods: `accept_order()` (creates
and submits an ERPNext Sales Order), `mark_dispatched()`, `mark_delivered()`,
`cancel_order(reason)`.

## Quick start

Runs as part of the combined stack, from the parent directory that contains
both this repo and `saathimart` (the hub) — `docker-compose.saathimart.yml`
lives there, one level up from this README:

```bash
docker compose -f ../docker-compose.saathimart.yml up --build -d
```

By default one vendor site (`vendor1.localhost`) is created, available at
`http://localhost:8001`. Add more names to that file's `VENDOR_SITES` env var
(space-separated, e.g. `vendor1.localhost vendor2.localhost`) if you need
several vendor sites sharing the same bench/container — each additional site
still comes up behind the same port, distinguished by `Host` header, since
there is no nginx router in front of them. Configure `Vendor Config` on each
site (Setup → Vendor Config) with the hub URL and `vendor_id` assigned by the
SaathiMart admin before sync starts.

## Testing

```bash
docker compose -f ../docker-compose.saathimart.yml exec vendor \
    bench --site vendor1.localhost run-tests --app saathimart_vendor
```

Test suite lives in `saathimart_vendor/tests/test_saathimart_vendor.py` and
covers Vendor Config validation, Product Mapping + hub sync, Sync Outbox
enqueue/flush/retry/dead-letter, the stock hooks (Purchase Receipt / Sales
Invoice / Stock Entry → outbox events), the full Vendor Order lifecycle
(accept → Sales Order, dispatch, deliver, cancel), inbound hub webhook
handlers, the mapping bulk-import API, and the hub-health/reconciliation
scheduled tasks. Hub HTTP calls are mocked (`unittest.mock`) — no live hub
needed to run this suite.
