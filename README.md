# Simple Billing & Invoicing Web App

A lightweight, full-stack billing and invoicing application built with **Python (Flask)**, **HTML**, **CSS**, and **atomic local JSON storage** (no external database server required).

## Features
- **Customer & Product Catalog Management**: Create, edit, archive, restore, and search customers (billing address, tax ID/GSTIN, currency, payment terms) and billable items/plans (one-time items, recurring plans, unit pricing, tax rates).
- **Invoice Generation & Lifecycle**: Create draft or sent invoices with automatic subtotal, before-tax discount (percentage or fixed), tax, and total calculations. Supports **Draft**, **Sent**, **Paid**, **Overdue**, **Void**, and **Refunded** statuses, browser print view, and downloadable PDF invoices (`/invoices/<id>/pdf`).
- **Payments & Immutable Ledger**: Record full or partial payments via Card, Bank Transfer, UPI, or Cash; automatically update invoice balances and overdue statuses; maintain an append-only immutable transaction ledger.
- **Optional Recurring Billing & Dunning**: Toggleable in the header or Settings. Supports monthly, quarterly, and yearly billing intervals, day-based proration on plan upgrades/downgrades, and payment retry/reminder states (`ok` -> `retry_1` -> `retry_2` -> `retry_3` -> `failed`).
- **Dashboard & Accounting Exports**: Summary cards (Total Billed, Collected, Outstanding/Overdue, MRR), filterable invoice tables, and CSV exports for both invoices and the accounting ledger.
- **Exact Money Math**: All monetary arithmetic uses Python `Decimal` (`ROUND_HALF_UP`) and integer cents — never floating-point math.

## Folder Structure
```text
d:\billing system\
├── app.py                 # Flask web & REST API routes
├── billing_math.py        # Integer-cent / Decimal math, taxes, discounts, proration
├── store.py               # Thread-safe atomic JSON persistence & domain rules
├── pdf_export.py          # Pure-Python PDF invoice generator
├── seed.py                # Sample seed data generator
├── requirements.txt       # Python dependencies (Flask)
├── data/
│   └── store.json         # Local JSON data file (auto-created & seeded on first run)
├── templates/             # Clean server-rendered HTML templates
├── static/
│   └── style.css          # Responsive & print-friendly CSS
└── tests/
    └── test_billing.py    # Unit & integration tests
```

## Setup & Run

1. **Install dependencies** (Flask 3.1+):
   ```powershell
   pip install -r requirements.txt
   ```

2. **(Optional) Reset & re-seed sample data**:
   ```powershell
   python seed.py --reset
   ```
   *(Sample data is automatically seeded on first startup if `data/store.json` does not exist.)*

3. **Run the web application**:
   ```powershell
   python app.py
   ```
   Open [http://127.0.0.1:5000](http://127.0.0.1:5000) in your browser.

4. **Run the test suite**:
   ```powershell
   python -m unittest discover -s tests -v
   ```

## REST API Endpoints
- `GET /api/reports/summary` — Dashboard metrics & MRR
- `GET, POST /api/customers`, `PUT /api/customers/<id>`, `POST /api/customers/<id>/archive`
- `GET, POST /api/items`, `PUT /api/items/<id>`, `POST /api/items/<id>/archive`
- `GET, POST /api/invoices`, `PUT /api/invoices/<id>`, `POST /api/invoices/<id>/send|void|refund`
- `GET, POST /api/payments`
- `GET /api/ledger` *(read-only immutable)*
- `GET, POST /api/subscriptions`, `POST /api/subscriptions/<id>/change-plan|renew|retry|remind|cancel`
- `GET, PUT /api/settings`
- `GET /export/invoices.csv`, `GET /export/ledger.csv`, `GET /invoices/<id>/pdf`
