"""Sample seed data for the billing and invoicing system."""

from __future__ import annotations

import argparse
from datetime import date, timedelta
from pathlib import Path

from store import BillingStore, default_data


DEFAULT_DATA_FILE = Path(__file__).parent / "data" / "store.json"


def seed_store(store: BillingStore, *, force: bool = False) -> bool:
    """Populate sample data if the store is empty (or always when force=True)."""
    if not force and (store.list_customers(include_archived=True) or store.list_items(include_archived=True)):
        return False

    store.reset(default_data())
    today = date.today()

    # 1. Customers (USD & INR)
    acme = store.create_customer(
        {
            "name": "Acme Retail Labs",
            "email": "ap@acmeretail.example",
            "phone": "+1-415-555-0192",
            "billing_address": "500 Howard St, San Francisco, CA 94105, USA",
            "tax_id": "EIN 82-1940221",
            "currency": "USD",
            "payment_terms_days": 15,
        }
    )
    zenith = store.create_customer(
        {
            "name": "Zenith Logistics Pvt Ltd",
            "email": "finance@zenithlogistics.example",
            "phone": "+91-80-4092-1180",
            "billing_address": "14 Residency Road, Bengaluru, KA 560025, India",
            "tax_id": "29AAACZ1234F1Z5",
            "currency": "INR",
            "payment_terms_days": 30,
        }
    )
    beacon = store.create_customer(
        {
            "name": "Beacon Studio",
            "email": "billing@beaconstudio.example",
            "phone": "+1-212-555-0148",
            "billing_address": "88 Pine St, New York, NY 10005, USA",
            "tax_id": "EIN 47-5501928",
            "currency": "USD",
            "payment_terms_days": 14,
        }
    )
    archived_cust = store.create_customer(
        {
            "name": "Old Harbor Goods (Archived)",
            "email": "accounts@oldharbor.example",
            "phone": "+1-617-555-0111",
            "billing_address": "12 Dockside Way, Boston, MA 02110, USA",
            "tax_id": "EIN 04-1122334",
            "currency": "USD",
            "payment_terms_days": 30,
        }
    )
    store.set_customer_archived(archived_cust["id"], True)

    # 2. Catalog Items & Recurring Plans
    item_setup = store.create_item(
        {
            "sku": "SETUP-01",
            "name": "Onboarding & Integration",
            "description": "One-time API & ERP setup assistance",
            "type": "one_time",
            "unit_price": "450.00",
            "currency": "USD",
            "tax_rate": "8.5",
        }
    )
    item_consulting = store.create_item(
        {
            "sku": "CONS-HR",
            "name": "Advisory Hour",
            "description": "Hourly architecture & billing consulting",
            "type": "one_time",
            "unit_price": "150.00",
            "currency": "USD",
            "tax_rate": "8.5",
        }
    )
    item_starter = store.create_item(
        {
            "sku": "PLAN-START",
            "name": "Starter Plan",
            "description": "Up to 5 team members, monthly billing",
            "type": "recurring",
            "billing_interval": "monthly",
            "unit_price": "49.00",
            "currency": "USD",
            "tax_rate": "10",
        }
    )
    item_pro = store.create_item(
        {
            "sku": "PLAN-PRO",
            "name": "Pro Plan",
            "description": "Unlimited invoices, automation & priority support",
            "type": "recurring",
            "billing_interval": "monthly",
            "unit_price": "149.00",
            "currency": "USD",
            "tax_rate": "10",
        }
    )
    item_inr_enterprise = store.create_item(
        {
            "sku": "PLAN-ENT-Q",
            "name": "Enterprise Suite (Quarterly)",
            "description": "Dedicated cluster and GST compliance reporting",
            "type": "recurring",
            "billing_interval": "quarterly",
            "unit_price": "24000.00",
            "currency": "INR",
            "tax_rate": "18",
        }
    )

    # 3. Invoices across all lifecycle states
    # Paid invoice (Acme)
    inv_paid = store.create_invoice(
        {
            "customer_id": acme["id"],
            "status": "sent",
            "issue_date": (today - timedelta(days=25)).isoformat(),
            "due_date": (today - timedelta(days=10)).isoformat(),
            "discount_type": "percentage",
            "discount_value_raw": "10",
            "notes": "Thank you for your prompt payment.",
            "lines": [
                {"item_id": item_setup["id"], "quantity": "1"},
                {"item_id": item_consulting["id"], "quantity": "3"},
            ],
        }
    )
    store.record_payment(
        {
            "invoice_id": inv_paid["id"],
            "amount_cents": inv_paid["total_cents"],
            "method": "bank_transfer",
            "reference": "ACH-88412",
            "paid_at": (today - timedelta(days=12)).isoformat(),
        }
    )

    # Partially paid Sent invoice (Zenith INR)
    inv_partial = store.create_invoice(
        {
            "customer_id": zenith["id"],
            "status": "sent",
            "issue_date": (today - timedelta(days=5)).isoformat(),
            "due_date": (today + timedelta(days=25)).isoformat(),
            "discount_type": "fixed",
            "discount_value_raw": "2000.00",
            "notes": "GST @ 18% included. Partial UPI advance received.",
            "lines": [
                {"item_id": item_inr_enterprise["id"], "quantity": "1"},
            ],
        }
    )
    store.record_payment(
        {
            "invoice_id": inv_partial["id"],
            "amount": "10000.00",
            "method": "upi",
            "reference": "UPI-409281726",
            "paid_at": (today - timedelta(days=2)).isoformat(),
        }
    )

    # Overdue invoice (Beacon USD)
    store.create_invoice(
        {
            "customer_id": beacon["id"],
            "status": "sent",
            "issue_date": (today - timedelta(days=24)).isoformat(),
            "due_date": (today - timedelta(days=10)).isoformat(),
            "notes": "Past due notice sent.",
            "lines": [
                {"item_id": item_pro["id"], "quantity": "2"},
                {"item_id": item_consulting["id"], "quantity": "2"},
            ],
        }
    )

    # Draft invoice (Acme USD)
    store.create_invoice(
        {
            "customer_id": acme["id"],
            "status": "draft",
            "issue_date": today.isoformat(),
            "due_date": (today + timedelta(days=15)).isoformat(),
            "notes": "Draft Q4 advisory retainer.",
            "lines": [
                {"item_id": item_consulting["id"], "quantity": "4"},
            ],
        }
    )

    # Voided invoice (Beacon USD)
    inv_void = store.create_invoice(
        {
            "customer_id": beacon["id"],
            "status": "sent",
            "issue_date": (today - timedelta(days=18)).isoformat(),
            "due_date": (today - timedelta(days=4)).isoformat(),
            "notes": "Duplicate invoice created in error.",
            "lines": [{"item_id": item_starter["id"], "quantity": "1"}],
        }
    )
    store.void_invoice(inv_void["id"], reason="Duplicate entry")

    # Refunded invoice (Acme USD)
    inv_refund = store.create_invoice(
        {
            "customer_id": acme["id"],
            "status": "sent",
            "issue_date": (today - timedelta(days=35)).isoformat(),
            "due_date": (today - timedelta(days=20)).isoformat(),
            "lines": [{"item_id": item_consulting["id"], "quantity": "1"}],
        }
    )
    store.record_payment(
        {
            "invoice_id": inv_refund["id"],
            "amount_cents": inv_refund["total_cents"],
            "method": "card",
            "reference": "CH-39104",
            "paid_at": (today - timedelta(days=30)).isoformat(),
        }
    )
    store.refund_invoice(inv_refund["id"], reason="Customer canceled workshop")

    # 4. Recurring Subscriptions (including one in dunning state)
    store.create_subscription(
        {
            "customer_id": acme["id"],
            "item_id": item_pro["id"],
            "interval": "monthly",
            "quantity": "1",
            "start_date": (today - timedelta(days=10)).isoformat(),
        }
    )
    store.create_subscription(
        {
            "customer_id": zenith["id"],
            "item_id": item_inr_enterprise["id"],
            "interval": "quarterly",
            "quantity": "1",
            "start_date": (today - timedelta(days=20)).isoformat(),
        }
    )
    sub_dunning = store.create_subscription(
        {
            "customer_id": beacon["id"],
            "item_id": item_starter["id"],
            "interval": "monthly",
            "quantity": "1",
            "start_date": (today - timedelta(days=30)).isoformat(),
        }
    )
    store.renew_subscription(sub_dunning["id"], simulate_failure=True, as_of=today.isoformat())

    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed sample billing data.")
    parser.add_argument("--reset", action="store_true", help="Overwrite existing store data with fresh sample data.")
    args = parser.parse_args()
    store = BillingStore(DEFAULT_DATA_FILE)
    seeded = seed_store(store, force=args.reset)
    print("Seeded sample data." if seeded else "Store already contains data (use --reset to overwrite).")
