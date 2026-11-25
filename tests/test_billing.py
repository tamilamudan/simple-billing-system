"""Unit and integration tests for billing math, lifecycle, payments, ledger, and recurring dunning."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from app import create_app
from billing_math import (
    LineInput,
    MoneyValidationError,
    calculate_balance,
    calculate_invoice_totals,
    calculate_proration,
    parse_money_to_cents,
    parse_percent_to_bps,
    parse_quantity,
)


class MoneyMathTests(unittest.TestCase):
    def test_parse_money_and_percent_exact_decimal(self) -> None:
        self.assertEqual(parse_money_to_cents("19.99"), 1999)
        self.assertEqual(parse_money_to_cents("1,250.50"), 125050)
        self.assertEqual(parse_percent_to_bps("18.5"), 1850)

        with self.assertRaises(MoneyValidationError):
            parse_money_to_cents("10.999")
        with self.assertRaises(MoneyValidationError):
            parse_money_to_cents("-5.00")
        with self.assertRaises(MoneyValidationError):
            parse_percent_to_bps("101")

    def test_percentage_discount_before_tax_and_penny_allocation(self) -> None:
        # Three $10.00 items -> $30.00 subtotal; 10% discount = $3.00 -> $1.00 each
        # Tax rates: 18%, 8.5%, 0%
        calc = calculate_invoice_totals(
            [
                LineInput("Item A", Decimal("1"), 1000, 1800),
                LineInput("Item B", Decimal("1"), 1000, 850),
                LineInput("Item C", Decimal("1"), 1000, 0),
            ],
            discount_type="percentage",
            discount_value=1000,  # 10%
        )
        self.assertEqual(calc.subtotal_cents, 3000)
        self.assertEqual(calc.discount_cents, 300)
        # Taxable per line = 900 cents each -> taxes: 162, 77 (76.5 half-up), 0
        self.assertEqual(calc.lines[0].line_tax_cents, 162)
        self.assertEqual(calc.lines[1].line_tax_cents, 77)
        self.assertEqual(calc.lines[2].line_tax_cents, 0)
        self.assertEqual(calc.tax_cents, 239)
        self.assertEqual(calc.total_cents, 3000 - 300 + 239)

    def test_penny_remainder_allocation_sum_matches_discount(self) -> None:
        # $0.10 subtotal split across 3 equal lines ($0.03? no: $1.00 * 3 = $3.00, fixed discount $0.10)
        calc = calculate_invoice_totals(
            [
                LineInput("L1", Decimal("1"), 100, 1000),
                LineInput("L2", Decimal("1"), 100, 1000),
                LineInput("L3", Decimal("1"), 100, 1000),
            ],
            discount_type="fixed",
            discount_value=10,
        )
        self.assertEqual(calc.discount_cents, 10)
        self.assertEqual(sum(ln.line_discount_cents for ln in calc.lines), 10)
        self.assertEqual(sum(ln.line_total_cents for ln in calc.lines), calc.total_cents)

    def test_fixed_discount_capped_at_subtotal(self) -> None:
        calc = calculate_invoice_totals(
            [LineInput("Item", Decimal("2"), 500, 1000)],
            discount_type="fixed",
            discount_value=5000,
        )
        self.assertEqual(calc.subtotal_cents, 1000)
        self.assertEqual(calc.discount_cents, 1000)
        self.assertEqual(calc.tax_cents, 0)
        self.assertEqual(calc.total_cents, 0)

    def test_balance_and_proration(self) -> None:
        self.assertEqual(calculate_balance(10000, 4000, 0), 6000)
        self.assertEqual(calculate_balance(10000, 10000, 2500), 2500)
        self.assertEqual(calculate_balance(10000, 12000, 0), 0)

        # Upgrade from $50.00 to $150.00 halfway through a 30-day cycle
        proration = calculate_proration(5000, 15000, total_days=30, remaining_days=15)
        self.assertEqual(proration["unused_credit_cents"], 2500)
        self.assertEqual(proration["new_charge_cents"], 7500)
        self.assertEqual(proration["net_proration_cents"], 5000)

        # Quantity validation
        self.assertEqual(parse_quantity("2.5"), Decimal("2.5"))
        with self.assertRaises(MoneyValidationError):
            parse_quantity("0")


class BillingIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.data_file = Path(self.tmp_dir.name) / "test_store.json"
        self.app = create_app(data_path=self.data_file, auto_seed=False)
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def _create_customer(self, currency: str = "USD") -> dict:
        res = self.client.post(
            "/api/customers",
            json={
                "name": "Test Buyer",
                "email": "buyer@example.com",
                "billing_address": "101 Main St",
                "tax_id": "TAX-123",
                "currency": currency,
                "payment_terms_days": 15,
            },
        )
        self.assertEqual(res.status_code, 201)
        return res.get_json()

    def _create_item(
        self,
        *,
        name: str = "Consulting",
        item_type: str = "one_time",
        unit_price: str = "100.00",
        tax_rate: str = "10",
        billing_interval: str | None = None,
    ) -> dict:
        res = self.client.post(
            "/api/items",
            json={
                "name": name,
                "type": item_type,
                "unit_price": unit_price,
                "tax_rate": tax_rate,
                "billing_interval": billing_interval,
                "currency": "USD",
            },
        )
        self.assertEqual(res.status_code, 201)
        return res.get_json()

    def test_invoice_lifecycle_payments_ledger_pdf_and_csv(self) -> None:
        cust = self._create_customer()
        item = self._create_item(unit_price="200.00", tax_rate="10")

        # Create draft invoice (2 * $200 = $400; 10% discount = $40; taxable = $360; 10% tax = $36 -> $396)
        past_due = (date.today() - timedelta(days=5)).isoformat()
        past_issue = (date.today() - timedelta(days=20)).isoformat()
        inv_res = self.client.post(
            "/api/invoices",
            json={
                "customer_id": cust["id"],
                "status": "draft",
                "issue_date": past_issue,
                "due_date": past_due,
                "discount_type": "percentage",
                "discount_value_raw": "10",
                "lines": [{"item_id": item["id"], "quantity": "2"}],
            },
        )
        self.assertEqual(inv_res.status_code, 201)
        inv = inv_res.get_json()
        self.assertEqual(inv["total_cents"], 39600)
        self.assertEqual(inv["status"], "draft")

        # Edit draft
        edit_res = self.client.put(
            f"/api/invoices/{inv['id']}",
            json={
                "discount_type": "none",
                "lines": [{"item_id": item["id"], "quantity": "1"}],  # $200 + 10% tax = $220
            },
        )
        self.assertEqual(edit_res.status_code, 200)
        self.assertEqual(edit_res.get_json()["total_cents"], 22000)

        # Send invoice -> since due_date < today, becomes overdue automatically
        send_res = self.client.post(f"/api/invoices/{inv['id']}/send")
        self.assertEqual(send_res.status_code, 200)
        self.assertEqual(send_res.get_json()["status"], "overdue")

        # Cannot edit once sent
        blocked_edit = self.client.put(
            f"/api/invoices/{inv['id']}",
            json={"lines": [{"item_id": item["id"], "quantity": "3"}]},
        )
        self.assertEqual(blocked_edit.status_code, 409)

        # Partial payment ($100.00 via UPI)
        p1 = self.client.post(
            "/api/payments",
            json={"invoice_id": inv["id"], "amount": "100.00", "method": "upi", "reference": "UPI-1"},
        )
        self.assertEqual(p1.status_code, 201)
        inv_after_p1 = self.client.get(f"/api/invoices/{inv['id']}").get_json()
        self.assertEqual(inv_after_p1["balance_cents"], 12000)
        self.assertEqual(inv_after_p1["status"], "overdue")

        # Overpayment rejected
        overpay = self.client.post(
            "/api/payments",
            json={"invoice_id": inv["id"], "amount": "150.00", "method": "card"},
        )
        self.assertEqual(overpay.status_code, 400)

        # Remaining payment ($120.00 via cash) -> Paid
        p2 = self.client.post(
            "/api/payments",
            json={"invoice_id": inv["id"], "amount": "120.00", "method": "cash"},
        )
        self.assertEqual(p2.status_code, 201)
        inv_paid = self.client.get(f"/api/invoices/{inv['id']}").get_json()
        self.assertEqual(inv_paid["balance_cents"], 0)
        self.assertEqual(inv_paid["status"], "paid")

        # Void on paid invoice blocked; refund allowed
        void_blocked = self.client.post(f"/api/invoices/{inv['id']}/void")
        self.assertEqual(void_blocked.status_code, 409)

        refund_res = self.client.post(f"/api/invoices/{inv['id']}/refund", json={"reason": "Customer return"})
        self.assertEqual(refund_res.status_code, 200)
        self.assertEqual(refund_res.get_json()["status"], "refunded")

        # Immutable ledger has invoice_issued, 2 payment_received, and refund_issued
        ledger = self.client.get(f"/api/ledger?invoice_id={inv['id']}").get_json()
        types = [entry["entry_type"] for entry in ledger]
        self.assertIn("invoice_issued", types)
        self.assertEqual(types.count("payment_received"), 2)
        self.assertIn("refund_issued", types)

        # PDF download & CSV exports
        pdf_res = self.client.get(f"/invoices/{inv['id']}/pdf")
        self.assertEqual(pdf_res.status_code, 200)
        self.assertTrue(pdf_res.data.startswith(b"%PDF-1.4"))
        self.assertTrue(pdf_res.data.rstrip().endswith(b"%%EOF"))

        csv_inv = self.client.get("/export/invoices.csv")
        self.assertEqual(csv_inv.status_code, 200)
        self.assertIn("Invoice Number", csv_inv.get_data(as_text=True))

        csv_led = self.client.get("/export/ledger.csv")
        self.assertEqual(csv_led.status_code, 200)
        self.assertIn("refund_issued", csv_led.get_data(as_text=True))

    def test_recurring_proration_dunning_and_toggle(self) -> None:
        cust = self._create_customer()
        starter = self._create_item(
            name="Starter", item_type="recurring", unit_price="50.00", tax_rate="10", billing_interval="monthly"
        )
        pro = self._create_item(
            name="Pro", item_type="recurring", unit_price="150.00", tax_rate="10", billing_interval="monthly"
        )

        # Start subscription on 2026-01-01 (31-day Jan cycle)
        sub_res = self.client.post(
            "/api/subscriptions",
            json={"customer_id": cust["id"], "item_id": starter["id"], "interval": "monthly", "start_date": "2026-01-01"},
        )
        self.assertEqual(sub_res.status_code, 201)
        sub = sub_res.get_json()

        # Upgrade mid-cycle on 2026-01-16 (16 days remaining out of 31)
        chg_res = self.client.post(
            f"/api/subscriptions/{sub['id']}/change-plan",
            json={"new_item_id": pro["id"], "effective_date": "2026-01-16"},
        )
        self.assertEqual(chg_res.status_code, 200)
        chg_data = chg_res.get_json()
        self.assertGreater(chg_data["proration"]["net_proration_cents"], 0)
        self.assertIsNotNone(chg_data["invoice"])

        # Simulate failed renewal -> past_due + retry_1
        fail_res = self.client.post(
            f"/api/subscriptions/{sub['id']}/renew",
            json={"simulate_failure": True, "as_of": "2026-02-01"},
        )
        self.assertEqual(fail_res.status_code, 200)
        self.assertEqual(fail_res.get_json()["subscription"]["dunning_state"], "retry_1")
        self.assertEqual(fail_res.get_json()["subscription"]["status"], "past_due")

        # Retry fails -> retry_2
        retry_fail = self.client.post(
            f"/api/subscriptions/{sub['id']}/retry",
            json={"simulate_success": False, "as_of": "2026-02-04"},
        )
        self.assertEqual(retry_fail.status_code, 200)
        self.assertEqual(retry_fail.get_json()["dunning_state"], "retry_2")

        # Retry succeeds -> active + ok
        retry_ok = self.client.post(
            f"/api/subscriptions/{sub['id']}/retry",
            json={"simulate_success": True, "as_of": "2026-02-09"},
        )
        self.assertEqual(retry_ok.status_code, 200)
        self.assertEqual(retry_ok.get_json()["dunning_state"], "ok")
        self.assertEqual(retry_ok.get_json()["status"], "active")

        # Disable recurring module via settings -> new subscriptions blocked
        self.client.put("/api/settings", json={"recurring_enabled": False})
        blocked_sub = self.client.post(
            "/api/subscriptions",
            json={"customer_id": cust["id"], "item_id": starter["id"], "interval": "monthly"},
        )
        self.assertEqual(blocked_sub.status_code, 409)


if __name__ == "__main__":
    unittest.main()
