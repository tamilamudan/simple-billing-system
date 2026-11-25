"""Thread-safe local JSON storage and billing domain operations."""

from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import threading
import uuid
from typing import Any

from billing_math import (
    LineInput,
    MoneyValidationError,
    SUPPORTED_CURRENCIES,
    calculate_balance,
    calculate_invoice_totals,
    calculate_proration,
    parse_money_to_cents,
    parse_percent_to_bps,
    parse_quantity,
    round_half_up,
)


PAYMENT_METHODS = ("card", "bank_transfer", "upi", "cash")
INVOICE_STATUSES = ("draft", "sent", "paid", "overdue", "void", "refunded")
BILLING_INTERVALS = ("monthly", "quarterly", "yearly")
DUNNING_STATES = ("ok", "retry_1", "retry_2", "retry_3", "failed")


class DomainError(ValueError):
    """Raised on business rule or validation failure."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


class NotFoundError(DomainError):
    """Raised when a requested resource does not exist."""

    def __init__(self, message: str) -> None:
        super().__init__(message, status_code=404)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def today_iso() -> str:
    return date.today().isoformat()


def add_interval(start_iso: str, interval: str) -> str:
    """Advance an ISO date string by monthly, quarterly, or yearly interval."""
    dt = date.fromisoformat(start_iso)
    months = {"monthly": 1, "quarterly": 3, "yearly": 12}.get(interval)
    if months is None:
        raise DomainError(f"Unsupported billing interval: {interval!r}")
    total_months = dt.month - 1 + months
    year = dt.year + total_months // 12
    month = total_months % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(dt.day, last_day)).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def default_data() -> dict[str, Any]:
    return {
        "settings": {
            "company_name": "Northstar Billing Co.",
            "company_address": "420 Market Street, Suite 300, San Francisco, CA 94105",
            "company_tax_id": "EIN 94-3829104",
            "company_email": "billing@northstar.example",
            "invoice_prefix": "INV",
            "next_invoice_seq": 1001,
            "recurring_enabled": True,
        },
        "customers": [],
        "items": [],
        "invoices": [],
        "payments": [],
        "ledger_entries": [],
        "subscriptions": [],
    }


class BillingStore:
    """JSON-backed persistence layer with atomic writes and business rule enforcement."""

    def __init__(self, data_path: str | Path) -> None:
        self.data_path = Path(data_path)
        self._lock = threading.RLock()
        self.data_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.data_path.exists():
            self._write_raw(default_data())

    def _read_raw(self) -> dict[str, Any]:
        with self.data_path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        base = default_data()
        for key, val in base.items():
            if key not in data:
                data[key] = val
        return data

    def _write_raw(self, data: dict[str, Any]) -> None:
        tmp_path = self.data_path.with_suffix(".tmp")
        with tmp_path.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        os.replace(tmp_path, self.data_path)

    def reset(self, data: dict[str, Any] | None = None) -> None:
        with self._lock:
            self._write_raw(data if data is not None else default_data())

    def _refresh_overdue(self, data: dict[str, Any], as_of: str | None = None) -> bool:
        today = as_of or today_iso()
        changed = False
        for inv in data["invoices"]:
            if inv["status"] == "sent" and inv["balance_cents"] > 0 and inv["due_date"] < today:
                inv["status"] = "overdue"
                inv["updated_at"] = utc_now_iso()
                changed = True
        return changed

    def _append_ledger(
        self,
        data: dict[str, Any],
        *,
        customer_id: str,
        invoice_id: str | None,
        payment_id: str | None,
        entry_type: str,
        direction: str,
        amount_cents: int,
        currency: str,
        balance_after_cents: int,
        note: str,
        entry_date: str | None = None,
    ) -> dict[str, Any]:
        entry = {
            "id": _new_id("led"),
            "entry_date": entry_date or today_iso(),
            "customer_id": customer_id,
            "invoice_id": invoice_id,
            "payment_id": payment_id,
            "entry_type": entry_type,
            "direction": direction,
            "amount_cents": int(amount_cents),
            "currency": currency,
            "balance_after_cents": int(balance_after_cents),
            "note": note,
            "created_at": utc_now_iso(),
        }
        data["ledger_entries"].append(entry)
        return entry

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------
    def get_settings(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._read_raw()["settings"])

    def update_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            data = self._read_raw()
            settings = data["settings"]
            for field in ("company_name", "company_address", "company_tax_id", "company_email", "invoice_prefix"):
                if field in payload:
                    val = str(payload[field]).strip()
                    if field in ("company_name", "invoice_prefix") and not val:
                        raise DomainError(f"{field.replace('_', ' ').title()} cannot be empty.")
                    settings[field] = val
            if "recurring_enabled" in payload:
                settings["recurring_enabled"] = bool(payload["recurring_enabled"])
            self._write_raw(data)
            return dict(settings)

    def toggle_recurring(self, enabled: bool | None = None) -> dict[str, Any]:
        with self._lock:
            data = self._read_raw()
            current = bool(data["settings"].get("recurring_enabled", True))
            data["settings"]["recurring_enabled"] = (not current) if enabled is None else bool(enabled)
            self._write_raw(data)
            return dict(data["settings"])

    # ------------------------------------------------------------------
    # Customers
    # ------------------------------------------------------------------
    def list_customers(self, *, search: str = "", include_archived: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            customers = self._read_raw()["customers"]
            q = search.strip().lower()
            result = []
            for c in customers:
                if not include_archived and c.get("archived"):
                    continue
                if q:
                    haystack = " ".join(
                        [
                            c.get("name", ""),
                            c.get("email", ""),
                            c.get("tax_id", ""),
                            c.get("billing_address", ""),
                            c.get("currency", ""),
                        ]
                    ).lower()
                    if q not in haystack:
                        continue
                result.append(c)
            return sorted(result, key=lambda x: x["name"].lower())

    def get_customer(self, customer_id: str) -> dict[str, Any]:
        with self._lock:
            for c in self._read_raw()["customers"]:
                if c["id"] == customer_id:
                    return c
            raise NotFoundError(f"Customer {customer_id!r} not found.")

    def _validate_customer_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        name = str(payload.get("name", "")).strip()
        email = str(payload.get("email", "")).strip()
        if not name:
            raise DomainError("Customer name is required.")
        if not email or "@" not in email:
            raise DomainError("A valid customer email is required.")

        currency = str(payload.get("currency", "USD")).strip().upper()
        if currency not in SUPPORTED_CURRENCIES:
            raise DomainError(f"Unsupported currency {currency!r}. Choose from {', '.join(SUPPORTED_CURRENCIES)}.")

        try:
            terms = int(payload.get("payment_terms_days", 15))
        except (TypeError, ValueError) as exc:
            raise DomainError("Payment terms days must be a whole number.") from exc
        if terms < 0 or terms > 365:
            raise DomainError("Payment terms days must be between 0 and 365.")

        return {
            "name": name,
            "email": email,
            "phone": str(payload.get("phone", "")).strip(),
            "billing_address": str(payload.get("billing_address", "")).strip(),
            "tax_id": str(payload.get("tax_id", "")).strip(),
            "currency": currency,
            "payment_terms_days": terms,
        }

    def create_customer(self, payload: dict[str, Any]) -> dict[str, Any]:
        clean = self._validate_customer_payload(payload)
        now = utc_now_iso()
        customer = {
            "id": _new_id("cust"),
            **clean,
            "archived": False,
            "created_at": now,
            "updated_at": now,
        }
        with self._lock:
            data = self._read_raw()
            data["customers"].append(customer)
            self._write_raw(data)
        return customer

    def update_customer(self, customer_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        clean = self._validate_customer_payload(payload)
        with self._lock:
            data = self._read_raw()
            for c in data["customers"]:
                if c["id"] == customer_id:
                    c.update(clean)
                    c["updated_at"] = utc_now_iso()
                    self._write_raw(data)
                    return c
            raise NotFoundError(f"Customer {customer_id!r} not found.")

    def set_customer_archived(self, customer_id: str, archived: bool) -> dict[str, Any]:
        with self._lock:
            data = self._read_raw()
            for c in data["customers"]:
                if c["id"] == customer_id:
                    c["archived"] = bool(archived)
                    c["updated_at"] = utc_now_iso()
                    self._write_raw(data)
                    return c
            raise NotFoundError(f"Customer {customer_id!r} not found.")

    # ------------------------------------------------------------------
    # Catalog Items & Plans
    # ------------------------------------------------------------------
    def list_items(
        self,
        *,
        search: str = "",
        include_archived: bool = False,
        item_type: str = "",
    ) -> list[dict[str, Any]]:
        with self._lock:
            items = self._read_raw()["items"]
            q = search.strip().lower()
            result = []
            for item in items:
                if not include_archived and item.get("archived"):
                    continue
                if item_type and item.get("type") != item_type:
                    continue
                if q:
                    haystack = " ".join(
                        [item.get("name", ""), item.get("sku", ""), item.get("description", "")]
                    ).lower()
                    if q not in haystack:
                        continue
                result.append(item)
            return sorted(result, key=lambda x: x["name"].lower())

    def get_item(self, item_id: str) -> dict[str, Any]:
        with self._lock:
            for item in self._read_raw()["items"]:
                if item["id"] == item_id:
                    return item
            raise NotFoundError(f"Catalog item {item_id!r} not found.")

    def _validate_item_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        name = str(payload.get("name", "")).strip()
        if not name:
            raise DomainError("Item name is required.")

        item_type = str(payload.get("type", "one_time")).strip().lower()
        if item_type not in ("one_time", "recurring"):
            raise DomainError("Item type must be 'one_time' or 'recurring'.")

        interval = payload.get("billing_interval")
        if item_type == "recurring":
            interval = str(interval or "monthly").strip().lower()
            if interval not in BILLING_INTERVALS:
                raise DomainError(f"Recurring items must have billing interval: {', '.join(BILLING_INTERVALS)}.")
        else:
            interval = None

        currency = str(payload.get("currency", "USD")).strip().upper()
        if currency not in SUPPORTED_CURRENCIES:
            raise DomainError(f"Unsupported currency {currency!r}.")

        try:
            if "unit_price_cents" in payload and payload["unit_price_cents"] is not None:
                unit_price_cents = int(payload["unit_price_cents"])
                if unit_price_cents < 0:
                    raise MoneyValidationError("Unit price cannot be negative.")
            else:
                unit_price_cents = parse_money_to_cents(payload.get("unit_price", "0"))

            if "tax_rate_bps" in payload and payload["tax_rate_bps"] is not None:
                tax_rate_bps = int(payload["tax_rate_bps"])
                if tax_rate_bps < 0 or tax_rate_bps > 10000:
                    raise MoneyValidationError("Tax rate must be between 0% and 100%.")
            else:
                tax_rate_bps = parse_percent_to_bps(payload.get("tax_rate", "0"))
        except MoneyValidationError as exc:
            raise DomainError(str(exc)) from exc

        return {
            "name": name,
            "sku": str(payload.get("sku", "")).strip().upper(),
            "description": str(payload.get("description", "")).strip(),
            "type": item_type,
            "billing_interval": interval,
            "unit_price_cents": unit_price_cents,
            "currency": currency,
            "tax_rate_bps": tax_rate_bps,
        }

    def create_item(self, payload: dict[str, Any]) -> dict[str, Any]:
        clean = self._validate_item_payload(payload)
        now = utc_now_iso()
        item = {
            "id": _new_id("item"),
            **clean,
            "archived": False,
            "created_at": now,
            "updated_at": now,
        }
        with self._lock:
            data = self._read_raw()
            data["items"].append(item)
            self._write_raw(data)
        return item

    def update_item(self, item_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        clean = self._validate_item_payload(payload)
        with self._lock:
            data = self._read_raw()
            for item in data["items"]:
                if item["id"] == item_id:
                    item.update(clean)
                    item["updated_at"] = utc_now_iso()
                    self._write_raw(data)
                    return item
            raise NotFoundError(f"Catalog item {item_id!r} not found.")

    def set_item_archived(self, item_id: str, archived: bool) -> dict[str, Any]:
        with self._lock:
            data = self._read_raw()
            for item in data["items"]:
                if item["id"] == item_id:
                    item["archived"] = bool(archived)
                    item["updated_at"] = utc_now_iso()
                    self._write_raw(data)
                    return item
            raise NotFoundError(f"Catalog item {item_id!r} not found.")

    # ------------------------------------------------------------------
    # Invoices & Lifecycle
    # ------------------------------------------------------------------
    def _build_line_inputs(
        self,
        data: dict[str, Any],
        raw_lines: list[dict[str, Any]],
    ) -> list[LineInput]:
        if not raw_lines:
            raise DomainError("At least one line item is required.")
        items_by_id = {i["id"]: i for i in data["items"]}
        parsed: list[LineInput] = []
        for idx, line in enumerate(raw_lines, start=1):
            item_id = line.get("item_id") or None
            catalog_item = items_by_id.get(item_id) if item_id else None
            if item_id and catalog_item is None:
                raise DomainError(f"Line {idx}: catalog item {item_id!r} not found.")

            description = str(line.get("description") or (catalog_item["name"] if catalog_item else "")).strip()
            if not description:
                raise DomainError(f"Line {idx}: description is required.")

            try:
                quantity = parse_quantity(line.get("quantity", "1"))
                if "unit_price_cents" in line and line["unit_price_cents"] is not None:
                    unit_price_cents = int(line["unit_price_cents"])
                elif "unit_price" in line and str(line["unit_price"]).strip() != "":
                    unit_price_cents = parse_money_to_cents(
                        line["unit_price"],
                        allow_negative=bool(line.get("is_proration", False)),
                    )
                elif catalog_item is not None:
                    unit_price_cents = int(catalog_item["unit_price_cents"])
                else:
                    raise DomainError(f"Line {idx}: unit price is required.")

                if "tax_rate_bps" in line and line["tax_rate_bps"] is not None:
                    tax_rate_bps = int(line["tax_rate_bps"])
                elif "tax_rate" in line and str(line["tax_rate"]).strip() != "":
                    tax_rate_bps = parse_percent_to_bps(line["tax_rate"])
                elif catalog_item is not None:
                    tax_rate_bps = int(catalog_item["tax_rate_bps"])
                else:
                    tax_rate_bps = 0
            except MoneyValidationError as exc:
                raise DomainError(f"Line {idx}: {exc}") from exc

            parsed.append(
                LineInput(
                    item_id=item_id,
                    description=description,
                    quantity=quantity,
                    unit_price_cents=unit_price_cents,
                    tax_rate_bps=tax_rate_bps,
                    is_proration=bool(line.get("is_proration", False)),
                )
            )
        return parsed

    def _parse_discount(self, payload: dict[str, Any]) -> tuple[str, int]:
        discount_type = str(payload.get("discount_type", "none")).strip().lower()
        if discount_type not in ("none", "percentage", "fixed"):
            raise DomainError("Discount type must be 'none', 'percentage', or 'fixed'.")
        if discount_type == "none":
            return "none", 0

        try:
            if "discount_value_raw" in payload and payload["discount_value_raw"] is not None:
                raw_val = payload["discount_value_raw"]
                val = (
                    parse_percent_to_bps(raw_val)
                    if discount_type == "percentage"
                    else parse_money_to_cents(raw_val)
                )
            elif "discount_value" in payload and payload["discount_value"] is not None:
                val = int(payload["discount_value"])
                if val < 0:
                    raise MoneyValidationError("Discount cannot be negative.")
            else:
                val = 0
        except MoneyValidationError as exc:
            raise DomainError(str(exc)) from exc
        return discount_type, val

    def _validate_iso_date(self, value: str, label: str) -> str:
        try:
            return date.fromisoformat(str(value).strip()).isoformat()
        except ValueError as exc:
            raise DomainError(f"Invalid {label}: {value!r}. Expected YYYY-MM-DD.") from exc

    def _create_invoice_in_data(
        self,
        data: dict[str, Any],
        payload: dict[str, Any],
        *,
        initial_status: str = "draft",
        subscription_id: str | None = None,
    ) -> dict[str, Any]:
        customer_id = str(payload.get("customer_id", "")).strip()
        customer = next((c for c in data["customers"] if c["id"] == customer_id), None)
        if customer is None:
            raise NotFoundError(f"Customer {customer_id!r} not found.")
        if customer.get("archived") and initial_status == "draft":
            raise DomainError("Cannot create an invoice for an archived customer.")

        issue_date = self._validate_iso_date(payload.get("issue_date") or today_iso(), "issue date")
        if payload.get("due_date"):
            due_date = self._validate_iso_date(payload["due_date"], "due date")
        else:
            due_date = (
                date.fromisoformat(issue_date) + timedelta(days=int(customer["payment_terms_days"]))
            ).isoformat()

        if due_date < issue_date:
            raise DomainError("Due date cannot be earlier than issue date.")

        line_inputs = self._build_line_inputs(data, payload.get("lines", []))
        discount_type, discount_value = self._parse_discount(payload)
        try:
            calc = calculate_invoice_totals(line_inputs, discount_type=discount_type, discount_value=discount_value)
        except MoneyValidationError as exc:
            raise DomainError(str(exc)) from exc

        seq = int(data["settings"].get("next_invoice_seq", 1001))
        prefix = data["settings"].get("invoice_prefix", "INV")
        invoice_number = f"{prefix}-{date.fromisoformat(issue_date).year}-{seq:04d}"
        data["settings"]["next_invoice_seq"] = seq + 1

        now = utc_now_iso()
        currency = str(payload.get("currency") or customer["currency"]).upper()
        invoice = {
            "id": _new_id("inv"),
            "invoice_number": invoice_number,
            "customer_id": customer_id,
            "subscription_id": subscription_id,
            "status": initial_status,
            "currency": currency,
            "issue_date": issue_date,
            "due_date": due_date,
            "discount_type": discount_type,
            "discount_value": discount_value,
            "subtotal_cents": calc.subtotal_cents,
            "discount_cents": calc.discount_cents,
            "tax_cents": calc.tax_cents,
            "total_cents": calc.total_cents,
            "paid_cents": 0,
            "refunded_cents": 0,
            "balance_cents": calc.total_cents,
            "notes": str(payload.get("notes", "")).strip(),
            "lines": [
                {
                    "id": _new_id("line"),
                    "item_id": ln.item_id,
                    "description": ln.description,
                    "quantity": ln.quantity,
                    "unit_price_cents": ln.unit_price_cents,
                    "tax_rate_bps": ln.tax_rate_bps,
                    "line_subtotal_cents": ln.line_subtotal_cents,
                    "line_discount_cents": ln.line_discount_cents,
                    "line_tax_cents": ln.line_tax_cents,
                    "line_total_cents": ln.line_total_cents,
                    "is_proration": ln.is_proration,
                }
                for ln in calc.lines
            ],
            "sent_at": now if initial_status == "sent" else None,
            "paid_at": None,
            "voided_at": None,
            "created_at": now,
            "updated_at": now,
        }

        if initial_status == "sent":
            if invoice["total_cents"] == 0:
                invoice["status"] = "paid"
                invoice["paid_at"] = now
            self._append_ledger(
                data,
                customer_id=customer_id,
                invoice_id=invoice["id"],
                payment_id=None,
                entry_type="invoice_issued",
                direction="debit",
                amount_cents=invoice["total_cents"],
                currency=currency,
                balance_after_cents=invoice["balance_cents"],
                note=f"Issued {invoice_number}",
                entry_date=issue_date,
            )
            self._refresh_overdue(data)

        data["invoices"].append(invoice)
        return invoice

    def create_invoice(self, payload: dict[str, Any]) -> dict[str, Any]:
        status = str(payload.get("status", "draft")).strip().lower()
        if status not in ("draft", "sent"):
            raise DomainError("New invoices must start as 'draft' or 'sent'.")
        with self._lock:
            data = self._read_raw()
            invoice = self._create_invoice_in_data(data, payload, initial_status=status)
            self._write_raw(data)
            return invoice

    def update_draft_invoice(self, invoice_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            data = self._read_raw()
            invoice = next((i for i in data["invoices"] if i["id"] == invoice_id), None)
            if invoice is None:
                raise NotFoundError(f"Invoice {invoice_id!r} not found.")
            if invoice["status"] != "draft":
                raise DomainError("Only Draft invoices can be edited. Void and reissue Sent invoices.", status_code=409)

            customer_id = str(payload.get("customer_id") or invoice["customer_id"]).strip()
            customer = next((c for c in data["customers"] if c["id"] == customer_id), None)
            if customer is None:
                raise NotFoundError(f"Customer {customer_id!r} not found.")

            issue_date = self._validate_iso_date(payload.get("issue_date") or invoice["issue_date"], "issue date")
            due_date = self._validate_iso_date(payload.get("due_date") or invoice["due_date"], "due date")
            if due_date < issue_date:
                raise DomainError("Due date cannot be earlier than issue date.")

            line_inputs = self._build_line_inputs(data, payload.get("lines", []))
            discount_type, discount_value = self._parse_discount(payload)
            try:
                calc = calculate_invoice_totals(
                    line_inputs, discount_type=discount_type, discount_value=discount_value
                )
            except MoneyValidationError as exc:
                raise DomainError(str(exc)) from exc

            invoice.update(
                {
                    "customer_id": customer_id,
                    "currency": str(payload.get("currency") or customer["currency"]).upper(),
                    "issue_date": issue_date,
                    "due_date": due_date,
                    "discount_type": discount_type,
                    "discount_value": discount_value,
                    "subtotal_cents": calc.subtotal_cents,
                    "discount_cents": calc.discount_cents,
                    "tax_cents": calc.tax_cents,
                    "total_cents": calc.total_cents,
                    "balance_cents": calc.total_cents,
                    "notes": str(payload.get("notes", invoice["notes"])).strip(),
                    "lines": [
                        {
                            "id": _new_id("line"),
                            "item_id": ln.item_id,
                            "description": ln.description,
                            "quantity": ln.quantity,
                            "unit_price_cents": ln.unit_price_cents,
                            "tax_rate_bps": ln.tax_rate_bps,
                            "line_subtotal_cents": ln.line_subtotal_cents,
                            "line_discount_cents": ln.line_discount_cents,
                            "line_tax_cents": ln.line_tax_cents,
                            "line_total_cents": ln.line_total_cents,
                            "is_proration": ln.is_proration,
                        }
                        for ln in calc.lines
                    ],
                    "updated_at": utc_now_iso(),
                }
            )
            self._write_raw(data)
            return invoice

    def list_invoices(
        self,
        *,
        status: str = "",
        customer_id: str = "",
        currency: str = "",
        search: str = "",
        date_from: str = "",
        date_to: str = "",
    ) -> list[dict[str, Any]]:
        with self._lock:
            data = self._read_raw()
            if self._refresh_overdue(data):
                self._write_raw(data)

            customers_by_id = {c["id"]: c for c in data["customers"]}
            q = search.strip().lower()
            result = []
            for inv in data["invoices"]:
                if status and inv["status"] != status:
                    continue
                if customer_id and inv["customer_id"] != customer_id:
                    continue
                if currency and inv["currency"] != currency.upper():
                    continue
                if date_from and inv["issue_date"] < date_from:
                    continue
                if date_to and inv["issue_date"] > date_to:
                    continue

                cust = customers_by_id.get(inv["customer_id"], {})
                if q:
                    haystack = " ".join(
                        [inv["invoice_number"], cust.get("name", ""), cust.get("email", ""), inv.get("notes", "")]
                    ).lower()
                    if q not in haystack:
                        continue
                enriched = dict(inv)
                enriched["customer_name"] = cust.get("name", "Unknown")
                enriched["customer_email"] = cust.get("email", "")
                result.append(enriched)

            return sorted(result, key=lambda x: (x["issue_date"], x["created_at"]), reverse=True)

    def get_invoice(self, invoice_id: str) -> dict[str, Any]:
        with self._lock:
            data = self._read_raw()
            if self._refresh_overdue(data):
                self._write_raw(data)
            inv = next((i for i in data["invoices"] if i["id"] == invoice_id), None)
            if inv is None:
                raise NotFoundError(f"Invoice {invoice_id!r} not found.")
            cust = next((c for c in data["customers"] if c["id"] == inv["customer_id"]), None)
            payments = [p for p in data["payments"] if p["invoice_id"] == invoice_id]
            ledger = [l for l in data["ledger_entries"] if l["invoice_id"] == invoice_id]
            out = dict(inv)
            out["customer"] = cust
            out["payments"] = sorted(payments, key=lambda p: p["created_at"], reverse=True)
            out["ledger"] = sorted(ledger, key=lambda l: l["created_at"], reverse=True)
            return out

    def send_invoice(self, invoice_id: str, *, as_of: str | None = None) -> dict[str, Any]:
        with self._lock:
            data = self._read_raw()
            inv = next((i for i in data["invoices"] if i["id"] == invoice_id), None)
            if inv is None:
                raise NotFoundError(f"Invoice {invoice_id!r} not found.")
            if inv["status"] != "draft":
                raise DomainError("Only Draft invoices can be marked as Sent.", status_code=409)

            now = utc_now_iso()
            inv["status"] = "paid" if inv["total_cents"] == 0 else "sent"
            inv["sent_at"] = now
            if inv["status"] == "paid":
                inv["paid_at"] = now
                inv["balance_cents"] = 0
            inv["updated_at"] = now

            self._append_ledger(
                data,
                customer_id=inv["customer_id"],
                invoice_id=inv["id"],
                payment_id=None,
                entry_type="invoice_issued",
                direction="debit",
                amount_cents=inv["total_cents"],
                currency=inv["currency"],
                balance_after_cents=inv["balance_cents"],
                note=f"Issued {inv['invoice_number']}",
                entry_date=inv["issue_date"],
            )
            self._refresh_overdue(data, as_of=as_of)
            self._write_raw(data)
            return inv

    def void_invoice(self, invoice_id: str, reason: str = "") -> dict[str, Any]:
        with self._lock:
            data = self._read_raw()
            inv = next((i for i in data["invoices"] if i["id"] == invoice_id), None)
            if inv is None:
                raise NotFoundError(f"Invoice {invoice_id!r} not found.")
            if inv["status"] in ("void", "refunded"):
                raise DomainError(f"Invoice is already {inv['status']}.", status_code=409)
            if inv["paid_cents"] > 0:
                raise DomainError("Invoices with recorded payments must be refunded instead of voided.", status_code=409)

            was_issued = inv["status"] in ("sent", "overdue")
            prev_balance = inv["balance_cents"]
            now = utc_now_iso()
            inv["status"] = "void"
            inv["balance_cents"] = 0
            inv["voided_at"] = now
            inv["updated_at"] = now

            if was_issued:
                self._append_ledger(
                    data,
                    customer_id=inv["customer_id"],
                    invoice_id=inv["id"],
                    payment_id=None,
                    entry_type="invoice_voided",
                    direction="credit",
                    amount_cents=prev_balance,
                    currency=inv["currency"],
                    balance_after_cents=0,
                    note=f"Voided {inv['invoice_number']}" + (f" ({reason.strip()})" if reason.strip() else ""),
                )
            self._write_raw(data)
            return inv

    def refund_invoice(self, invoice_id: str, *, amount_cents: int | None = None, reason: str = "") -> dict[str, Any]:
        with self._lock:
            data = self._read_raw()
            inv = next((i for i in data["invoices"] if i["id"] == invoice_id), None)
            if inv is None:
                raise NotFoundError(f"Invoice {invoice_id!r} not found.")
            if inv["status"] not in ("paid", "sent", "overdue"):
                raise DomainError(f"Cannot refund an invoice with status {inv['status']!r}.", status_code=409)

            net_paid = inv["paid_cents"] - inv.get("refunded_cents", 0)
            if net_paid <= 0:
                raise DomainError("No net payments available to refund.", status_code=409)

            refund_amt = net_paid if amount_cents is None else int(amount_cents)
            if refund_amt <= 0 or refund_amt > net_paid:
                raise DomainError("Refund amount must be positive and cannot exceed net paid amount.")

            now = utc_now_iso()
            inv["refunded_cents"] = inv.get("refunded_cents", 0) + refund_amt
            remaining_net_paid = inv["paid_cents"] - inv["refunded_cents"]
            if remaining_net_paid == 0:
                inv["status"] = "refunded"
                inv["balance_cents"] = 0
            else:
                inv["balance_cents"] = calculate_balance(inv["total_cents"], inv["paid_cents"], inv["refunded_cents"])
                inv["status"] = "overdue" if inv["due_date"] < today_iso() else "sent"
            inv["updated_at"] = now

            refund_payment = {
                "id": _new_id("pay"),
                "invoice_id": inv["id"],
                "customer_id": inv["customer_id"],
                "amount_cents": -refund_amt,
                "currency": inv["currency"],
                "method": "refund",
                "reference": reason.strip() or f"Refund for {inv['invoice_number']}",
                "status": "refunded",
                "paid_at": today_iso(),
                "created_at": now,
            }
            data["payments"].append(refund_payment)
            self._append_ledger(
                data,
                customer_id=inv["customer_id"],
                invoice_id=inv["id"],
                payment_id=refund_payment["id"],
                entry_type="refund_issued",
                direction="debit",
                amount_cents=refund_amt,
                currency=inv["currency"],
                balance_after_cents=inv["balance_cents"],
                note=f"Refunded {inv['invoice_number']}" + (f": {reason.strip()}" if reason.strip() else ""),
            )
            self._write_raw(data)
            return inv

    # ------------------------------------------------------------------
    # Payments & Immutable Ledger
    # ------------------------------------------------------------------
    def record_payment(self, payload: dict[str, Any]) -> dict[str, Any]:
        invoice_id = str(payload.get("invoice_id", "")).strip()
        method = str(payload.get("method", "card")).strip().lower()
        if method not in PAYMENT_METHODS:
            raise DomainError(f"Invalid payment method {method!r}. Allowed: {', '.join(PAYMENT_METHODS)}.")

        paid_at = self._validate_iso_date(payload.get("paid_at") or today_iso(), "payment date")
        reference = str(payload.get("reference", "")).strip()
        simulate_failure = bool(payload.get("simulate_failure", False))

        with self._lock:
            data = self._read_raw()
            self._refresh_overdue(data)
            inv = next((i for i in data["invoices"] if i["id"] == invoice_id), None)
            if inv is None:
                raise NotFoundError(f"Invoice {invoice_id!r} not found.")
            if inv["status"] not in ("sent", "overdue"):
                raise DomainError(
                    f"Payments can only be recorded on Sent or Overdue invoices (current: {inv['status']}).",
                    status_code=409,
                )

            try:
                if "amount_cents" in payload and payload["amount_cents"] is not None:
                    amount_cents = int(payload["amount_cents"])
                    if amount_cents <= 0:
                        raise MoneyValidationError("Payment amount must be greater than zero.")
                else:
                    amount_cents = parse_money_to_cents(payload.get("amount", ""), allow_zero=False)
            except MoneyValidationError as exc:
                raise DomainError(str(exc)) from exc

            if amount_cents > inv["balance_cents"]:
                raise DomainError("Payment amount cannot exceed the remaining invoice balance.")

            now = utc_now_iso()
            if simulate_failure:
                payment = {
                    "id": _new_id("pay"),
                    "invoice_id": inv["id"],
                    "customer_id": inv["customer_id"],
                    "amount_cents": amount_cents,
                    "currency": inv["currency"],
                    "method": method,
                    "reference": reference or "Payment declined",
                    "status": "failed",
                    "paid_at": paid_at,
                    "created_at": now,
                }
                data["payments"].append(payment)
                self._append_ledger(
                    data,
                    customer_id=inv["customer_id"],
                    invoice_id=inv["id"],
                    payment_id=payment["id"],
                    entry_type="payment_failed",
                    direction="info",
                    amount_cents=amount_cents,
                    currency=inv["currency"],
                    balance_after_cents=inv["balance_cents"],
                    note=f"Failed {method} payment on {inv['invoice_number']}",
                    entry_date=paid_at,
                )
                self._write_raw(data)
                return payment

            payment = {
                "id": _new_id("pay"),
                "invoice_id": inv["id"],
                "customer_id": inv["customer_id"],
                "amount_cents": amount_cents,
                "currency": inv["currency"],
                "method": method,
                "reference": reference,
                "status": "succeeded",
                "paid_at": paid_at,
                "created_at": now,
            }
            data["payments"].append(payment)

            inv["paid_cents"] += amount_cents
            inv["balance_cents"] = calculate_balance(
                inv["total_cents"], inv["paid_cents"], inv.get("refunded_cents", 0)
            )
            if inv["balance_cents"] == 0:
                inv["status"] = "paid"
                inv["paid_at"] = now
            inv["updated_at"] = now

            self._append_ledger(
                data,
                customer_id=inv["customer_id"],
                invoice_id=inv["id"],
                payment_id=payment["id"],
                entry_type="payment_received",
                direction="credit",
                amount_cents=amount_cents,
                currency=inv["currency"],
                balance_after_cents=inv["balance_cents"],
                note=f"Payment ({method}) for {inv['invoice_number']}" + (f" [{reference}]" if reference else ""),
                entry_date=paid_at,
            )
            self._write_raw(data)
            return payment

    def list_payments(self) -> list[dict[str, Any]]:
        with self._lock:
            data = self._read_raw()
            customers_by_id = {c["id"]: c for c in data["customers"]}
            invoices_by_id = {i["id"]: i for i in data["invoices"]}
            rows = []
            for p in data["payments"]:
                row = dict(p)
                row["customer_name"] = customers_by_id.get(p["customer_id"], {}).get("name", "Unknown")
                row["invoice_number"] = invoices_by_id.get(p["invoice_id"], {}).get("invoice_number", "")
                rows.append(row)
            return sorted(rows, key=lambda x: (x["paid_at"], x["created_at"]), reverse=True)

    def list_ledger(self, *, customer_id: str = "", invoice_id: str = "") -> list[dict[str, Any]]:
        with self._lock:
            data = self._read_raw()
            customers_by_id = {c["id"]: c for c in data["customers"]}
            invoices_by_id = {i["id"]: i for i in data["invoices"]}
            rows = []
            for entry in data["ledger_entries"]:
                if customer_id and entry["customer_id"] != customer_id:
                    continue
                if invoice_id and entry["invoice_id"] != invoice_id:
                    continue
                row = dict(entry)
                row["customer_name"] = customers_by_id.get(entry["customer_id"], {}).get("name", "Unknown")
                row["invoice_number"] = invoices_by_id.get(entry["invoice_id"], {}).get("invoice_number", "")
                rows.append(row)
            return sorted(rows, key=lambda x: (x["created_at"], x["id"]), reverse=True)

    # ------------------------------------------------------------------
    # Recurring Subscriptions, Proration & Dunning
    # ------------------------------------------------------------------
    def _ensure_recurring_enabled(self, data: dict[str, Any]) -> None:
        if not data["settings"].get("recurring_enabled", True):
            raise DomainError("Recurring billing & dunning is currently disabled in Settings.", status_code=409)

    def list_subscriptions(self) -> list[dict[str, Any]]:
        with self._lock:
            data = self._read_raw()
            customers_by_id = {c["id"]: c for c in data["customers"]}
            items_by_id = {i["id"]: i for i in data["items"]}
            rows = []
            for sub in data["subscriptions"]:
                row = dict(sub)
                row["customer_name"] = customers_by_id.get(sub["customer_id"], {}).get("name", "Unknown")
                row["item_name"] = items_by_id.get(sub["item_id"], {}).get("name", "Plan")
                rows.append(row)
            return sorted(rows, key=lambda x: x["created_at"], reverse=True)

    def create_subscription(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            data = self._read_raw()
            self._ensure_recurring_enabled(data)
            customer_id = str(payload.get("customer_id", "")).strip()
            item_id = str(payload.get("item_id", "")).strip()
            customer = next((c for c in data["customers"] if c["id"] == customer_id), None)
            if customer is None:
                raise NotFoundError(f"Customer {customer_id!r} not found.")
            item = next((i for i in data["items"] if i["id"] == item_id), None)
            if item is None:
                raise NotFoundError(f"Plan item {item_id!r} not found.")
            if item["type"] != "recurring":
                raise DomainError("Selected catalog item must have type 'recurring'.")

            interval = str(payload.get("interval") or item.get("billing_interval") or "monthly").strip().lower()
            if interval not in BILLING_INTERVALS:
                raise DomainError(f"Unsupported billing interval: {interval!r}.")

            try:
                qty = parse_quantity(payload.get("quantity", "1"))
            except MoneyValidationError as exc:
                raise DomainError(str(exc)) from exc

            start_date = self._validate_iso_date(payload.get("start_date") or today_iso(), "start date")
            end_date = add_interval(start_date, interval)
            now = utc_now_iso()
            sub = {
                "id": _new_id("sub"),
                "customer_id": customer_id,
                "item_id": item_id,
                "interval": interval,
                "quantity": str(qty),
                "unit_price_cents": int(item["unit_price_cents"]),
                "tax_rate_bps": int(item["tax_rate_bps"]),
                "currency": customer["currency"],
                "status": "active",
                "current_period_start": start_date,
                "current_period_end": end_date,
                "next_billing_date": end_date,
                "dunning_state": "ok",
                "retry_count": 0,
                "next_retry_date": None,
                "last_error": "",
                "last_invoice_id": None,
                "events": [
                    {
                        "date": start_date,
                        "type": "subscription_started",
                        "note": f"Started {item['name']} ({interval})",
                    }
                ],
                "created_at": now,
                "updated_at": now,
            }
            data["subscriptions"].append(sub)
            self._write_raw(data)
            return sub

    def change_subscription_plan(
        self,
        sub_id: str,
        new_item_id: str,
        *,
        effective_date: str | None = None,
    ) -> dict[str, Any]:
        """Upgrade or downgrade a subscription with exact day-based proration."""
        with self._lock:
            data = self._read_raw()
            self._ensure_recurring_enabled(data)
            sub = next((s for s in data["subscriptions"] if s["id"] == sub_id), None)
            if sub is None:
                raise NotFoundError(f"Subscription {sub_id!r} not found.")
            if sub["status"] == "canceled":
                raise DomainError("Cannot change plan on a canceled subscription.", status_code=409)

            new_item = next((i for i in data["items"] if i["id"] == new_item_id), None)
            if new_item is None:
                raise NotFoundError(f"Plan item {new_item_id!r} not found.")
            if new_item["type"] != "recurring":
                raise DomainError("Target item must be a recurring plan.")

            old_item = next((i for i in data["items"] if i["id"] == sub["item_id"]), {"name": "Previous Plan"})
            eff = self._validate_iso_date(effective_date or today_iso(), "effective date")
            p_start = date.fromisoformat(sub["current_period_start"])
            p_end = date.fromisoformat(sub["current_period_end"])
            eff_dt = date.fromisoformat(eff)

            total_days = max(1, (p_end - p_start).days)
            if eff_dt <= p_start:
                remaining_days = total_days
            elif eff_dt >= p_end:
                remaining_days = 0
            else:
                remaining_days = (p_end - eff_dt).days

            qty = Decimal(sub["quantity"])
            proration = calculate_proration(
                sub["unit_price_cents"],
                int(new_item["unit_price_cents"]),
                total_days,
                remaining_days,
                quantity=qty,
            )

            created_invoice = None
            if remaining_days > 0 and proration["net_proration_cents"] != 0:
                lines = [
                    {
                        "item_id": sub["item_id"],
                        "description": f"Proration credit: {old_item['name']} ({remaining_days}/{total_days} days unused)",
                        "quantity": "1",
                        "unit_price_cents": -proration["unused_credit_cents"],
                        "tax_rate_bps": int(sub["tax_rate_bps"]),
                        "is_proration": True,
                    },
                    {
                        "item_id": new_item["id"],
                        "description": f"Proration charge: {new_item['name']} ({remaining_days}/{total_days} days remaining)",
                        "quantity": "1",
                        "unit_price_cents": proration["new_charge_cents"],
                        "tax_rate_bps": int(new_item["tax_rate_bps"]),
                        "is_proration": True,
                    },
                ]
                if proration["net_proration_cents"] > 0:
                    created_invoice = self._create_invoice_in_data(
                        data,
                        {
                            "customer_id": sub["customer_id"],
                            "currency": sub["currency"],
                            "issue_date": eff,
                            "due_date": eff,
                            "lines": lines,
                            "notes": f"Plan upgrade proration from {old_item['name']} to {new_item['name']}",
                        },
                        initial_status="sent",
                        subscription_id=sub["id"],
                    )
                    sub["last_invoice_id"] = created_invoice["id"]
                else:
                    # Downgrade credit recorded in immutable ledger
                    self._append_ledger(
                        data,
                        customer_id=sub["customer_id"],
                        invoice_id=None,
                        payment_id=None,
                        entry_type="proration_credit",
                        direction="credit",
                        amount_cents=abs(proration["net_proration_cents"]),
                        currency=sub["currency"],
                        balance_after_cents=0,
                        note=f"Downgrade proration credit ({old_item['name']} -> {new_item['name']}, {remaining_days}/{total_days} days)",
                        entry_date=eff,
                    )

            sub["item_id"] = new_item["id"]
            sub["unit_price_cents"] = int(new_item["unit_price_cents"])
            sub["tax_rate_bps"] = int(new_item["tax_rate_bps"])
            if new_item.get("billing_interval") in BILLING_INTERVALS:
                sub["interval"] = new_item["billing_interval"]
            sub["updated_at"] = utc_now_iso()
            sub["events"].append(
                {
                    "date": eff,
                    "type": "plan_changed",
                    "note": (
                        f"Changed from {old_item['name']} to {new_item['name']}; "
                        f"net proration: {proration['net_proration_cents']} cents"
                    ),
                }
            )
            self._write_raw(data)
            return {
                "subscription": sub,
                "proration": proration,
                "invoice": created_invoice,
            }

    def renew_subscription(
        self,
        sub_id: str,
        *,
        simulate_failure: bool = False,
        payment_method: str = "card",
        as_of: str | None = None,
    ) -> dict[str, Any]:
        """Generate renewal invoice and either collect payment or enter dunning retry state."""
        with self._lock:
            data = self._read_raw()
            self._ensure_recurring_enabled(data)
            sub = next((s for s in data["subscriptions"] if s["id"] == sub_id), None)
            if sub is None:
                raise NotFoundError(f"Subscription {sub_id!r} not found.")
            if sub["status"] == "canceled":
                raise DomainError("Cannot bill a canceled subscription.", status_code=409)

            item = next((i for i in data["items"] if i["id"] == sub["item_id"]), None)
            item_name = item["name"] if item else "Recurring Plan"
            run_date = as_of or today_iso()
            next_start = sub["current_period_end"]
            next_end = add_interval(next_start, sub["interval"])

            inv = self._create_invoice_in_data(
                data,
                {
                    "customer_id": sub["customer_id"],
                    "currency": sub["currency"],
                    "issue_date": run_date,
                    "due_date": run_date,
                    "lines": [
                        {
                            "item_id": sub["item_id"],
                            "description": f"{item_name} ({next_start} to {next_end})",
                            "quantity": sub["quantity"],
                            "unit_price_cents": sub["unit_price_cents"],
                            "tax_rate_bps": sub["tax_rate_bps"],
                        }
                    ],
                    "notes": f"Automated {sub['interval']} renewal",
                },
                initial_status="sent",
                subscription_id=sub["id"],
            )
            sub["last_invoice_id"] = inv["id"]

            if not simulate_failure:
                now = utc_now_iso()
                pay = {
                    "id": _new_id("pay"),
                    "invoice_id": inv["id"],
                    "customer_id": sub["customer_id"],
                    "amount_cents": inv["balance_cents"],
                    "currency": inv["currency"],
                    "method": payment_method if payment_method in PAYMENT_METHODS else "card",
                    "reference": f"Auto-charge {sub['id']}",
                    "status": "succeeded",
                    "paid_at": run_date,
                    "created_at": now,
                }
                data["payments"].append(pay)
                inv["paid_cents"] += inv["balance_cents"]
                inv["balance_cents"] = 0
                inv["status"] = "paid"
                inv["paid_at"] = now
                inv["updated_at"] = now

                self._append_ledger(
                    data,
                    customer_id=sub["customer_id"],
                    invoice_id=inv["id"],
                    payment_id=pay["id"],
                    entry_type="payment_received",
                    direction="credit",
                    amount_cents=pay["amount_cents"],
                    currency=inv["currency"],
                    balance_after_cents=0,
                    note=f"Subscription renewal paid for {inv['invoice_number']}",
                    entry_date=run_date,
                )

                sub["status"] = "active"
                sub["current_period_start"] = next_start
                sub["current_period_end"] = next_end
                sub["next_billing_date"] = next_end
                sub["dunning_state"] = "ok"
                sub["retry_count"] = 0
                sub["next_retry_date"] = None
                sub["last_error"] = ""
                sub["events"].append(
                    {"date": run_date, "type": "renewal_paid", "note": f"Renewed and paid {inv['invoice_number']}"}
                )
            else:
                fail_pay = {
                    "id": _new_id("pay"),
                    "invoice_id": inv["id"],
                    "customer_id": sub["customer_id"],
                    "amount_cents": inv["balance_cents"],
                    "currency": inv["currency"],
                    "method": payment_method if payment_method in PAYMENT_METHODS else "card",
                    "reference": "Charge declined (dunning started)",
                    "status": "failed",
                    "paid_at": run_date,
                    "created_at": utc_now_iso(),
                }
                data["payments"].append(fail_pay)
                self._append_ledger(
                    data,
                    customer_id=sub["customer_id"],
                    invoice_id=inv["id"],
                    payment_id=fail_pay["id"],
                    entry_type="payment_failed",
                    direction="info",
                    amount_cents=inv["balance_cents"],
                    currency=inv["currency"],
                    balance_after_cents=inv["balance_cents"],
                    note=f"Renewal charge failed on {inv['invoice_number']}; entered Retry 1",
                    entry_date=run_date,
                )
                sub["status"] = "past_due"
                sub["dunning_state"] = "retry_1"
                sub["retry_count"] = 1
                sub["next_retry_date"] = (date.fromisoformat(run_date) + timedelta(days=3)).isoformat()
                sub["last_error"] = "Card declined / insufficient funds"
                sub["events"].append(
                    {
                        "date": run_date,
                        "type": "dunning_retry_1",
                        "note": f"Charge failed on {inv['invoice_number']}; reminder sent, Retry 1 scheduled for {sub['next_retry_date']}",
                    }
                )

            sub["updated_at"] = utc_now_iso()
            self._write_raw(data)
            return {"subscription": sub, "invoice": inv}

    def retry_dunning(
        self,
        sub_id: str,
        *,
        simulate_success: bool = True,
        payment_method: str = "card",
        as_of: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            data = self._read_raw()
            self._ensure_recurring_enabled(data)
            sub = next((s for s in data["subscriptions"] if s["id"] == sub_id), None)
            if sub is None:
                raise NotFoundError(f"Subscription {sub_id!r} not found.")
            if sub["dunning_state"] == "ok":
                raise DomainError("Subscription is not in a dunning retry state.", status_code=409)

            run_date = as_of or today_iso()
            inv = next((i for i in data["invoices"] if i["id"] == sub.get("last_invoice_id")), None)
            if inv is None or inv["balance_cents"] <= 0:
                sub["status"] = "active"
                sub["dunning_state"] = "ok"
                sub["retry_count"] = 0
                sub["next_retry_date"] = None
                sub["last_error"] = ""
                sub["updated_at"] = utc_now_iso()
                self._write_raw(data)
                return sub

            if simulate_success:
                now = utc_now_iso()
                pay = {
                    "id": _new_id("pay"),
                    "invoice_id": inv["id"],
                    "customer_id": sub["customer_id"],
                    "amount_cents": inv["balance_cents"],
                    "currency": inv["currency"],
                    "method": payment_method if payment_method in PAYMENT_METHODS else "card",
                    "reference": f"Dunning retry #{sub['retry_count']} succeeded",
                    "status": "succeeded",
                    "paid_at": run_date,
                    "created_at": now,
                }
                data["payments"].append(pay)
                inv["paid_cents"] += inv["balance_cents"]
                inv["balance_cents"] = 0
                inv["status"] = "paid"
                inv["paid_at"] = now
                inv["updated_at"] = now
                self._append_ledger(
                    data,
                    customer_id=sub["customer_id"],
                    invoice_id=inv["id"],
                    payment_id=pay["id"],
                    entry_type="payment_received",
                    direction="credit",
                    amount_cents=pay["amount_cents"],
                    currency=inv["currency"],
                    balance_after_cents=0,
                    note=f"Dunning recovery payment for {inv['invoice_number']}",
                    entry_date=run_date,
                )
                next_start = sub["current_period_end"]
                next_end = add_interval(next_start, sub["interval"])
                sub["status"] = "active"
                sub["current_period_start"] = next_start
                sub["current_period_end"] = next_end
                sub["next_billing_date"] = next_end
                sub["dunning_state"] = "ok"
                sub["retry_count"] = 0
                sub["next_retry_date"] = None
                sub["last_error"] = ""
                sub["events"].append(
                    {"date": run_date, "type": "dunning_recovered", "note": f"Retry succeeded on {inv['invoice_number']}"}
                )
            else:
                next_retry = sub["retry_count"] + 1
                sub["retry_count"] = next_retry
                if next_retry == 2:
                    sub["dunning_state"] = "retry_2"
                    sub["next_retry_date"] = (date.fromisoformat(run_date) + timedelta(days=5)).isoformat()
                elif next_retry == 3:
                    sub["dunning_state"] = "retry_3"
                    sub["next_retry_date"] = (date.fromisoformat(run_date) + timedelta(days=7)).isoformat()
                else:
                    sub["dunning_state"] = "failed"
                    sub["next_retry_date"] = None
                sub["status"] = "past_due"
                self._append_ledger(
                    data,
                    customer_id=sub["customer_id"],
                    invoice_id=inv["id"],
                    payment_id=None,
                    entry_type="payment_failed",
                    direction="info",
                    amount_cents=inv["balance_cents"],
                    currency=inv["currency"],
                    balance_after_cents=inv["balance_cents"],
                    note=f"Dunning retry #{next_retry} failed on {inv['invoice_number']} ({sub['dunning_state']})",
                    entry_date=run_date,
                )
                sub["events"].append(
                    {
                        "date": run_date,
                        "type": f"dunning_{sub['dunning_state']}",
                        "note": f"Retry #{next_retry} failed; state is now {sub['dunning_state']}",
                    }
                )

            sub["updated_at"] = utc_now_iso()
            self._write_raw(data)
            return sub

    def send_dunning_reminder(self, sub_id: str) -> dict[str, Any]:
        with self._lock:
            data = self._read_raw()
            self._ensure_recurring_enabled(data)
            sub = next((s for s in data["subscriptions"] if s["id"] == sub_id), None)
            if sub is None:
                raise NotFoundError(f"Subscription {sub_id!r} not found.")
            run_date = today_iso()
            sub["events"].append(
                {
                    "date": run_date,
                    "type": "reminder_sent",
                    "note": f"Payment reminder email queued (state: {sub['dunning_state']})",
                }
            )
            sub["updated_at"] = utc_now_iso()
            self._write_raw(data)
            return sub

    def cancel_subscription(self, sub_id: str) -> dict[str, Any]:
        with self._lock:
            data = self._read_raw()
            sub = next((s for s in data["subscriptions"] if s["id"] == sub_id), None)
            if sub is None:
                raise NotFoundError(f"Subscription {sub_id!r} not found.")
            sub["status"] = "canceled"
            sub["next_retry_date"] = None
            sub["events"].append({"date": today_iso(), "type": "canceled", "note": "Subscription canceled"})
            sub["updated_at"] = utc_now_iso()
            self._write_raw(data)
            return sub

    # ------------------------------------------------------------------
    # Dashboard & Reporting
    # ------------------------------------------------------------------
    def get_dashboard_summary(self, *, currency: str = "") -> dict[str, Any]:
        with self._lock:
            data = self._read_raw()
            if self._refresh_overdue(data):
                self._write_raw(data)

            invoices = data["invoices"]
            subscriptions = data["subscriptions"]
            filter_curr = currency.strip().upper()
            available_currencies = sorted(
                {inv["currency"] for inv in invoices} | {sub["currency"] for sub in subscriptions} | {"USD"}
            )
            if filter_curr:
                invoices = [i for i in invoices if i["currency"] == filter_curr]
                subscriptions = [s for s in subscriptions if s["currency"] == filter_curr]

            by_currency: dict[str, dict[str, int]] = {}

            def ensure_bucket(curr: str) -> dict[str, int]:
                if curr not in by_currency:
                    by_currency[curr] = {
                        "billed_cents": 0,
                        "collected_cents": 0,
                        "outstanding_cents": 0,
                        "overdue_cents": 0,
                        "mrr_cents": 0,
                    }
                return by_currency[curr]

            status_counts = {s: 0 for s in INVOICE_STATUSES}
            for inv in invoices:
                status_counts[inv["status"]] = status_counts.get(inv["status"], 0) + 1
                b = ensure_bucket(inv["currency"])
                if inv["status"] not in ("draft", "void"):
                    b["billed_cents"] += int(inv["total_cents"])
                    net_collected = max(0, int(inv["paid_cents"]) - int(inv.get("refunded_cents", 0)))
                    b["collected_cents"] += net_collected
                if inv["status"] in ("sent", "overdue"):
                    b["outstanding_cents"] += int(inv["balance_cents"])
                if inv["status"] == "overdue":
                    b["overdue_cents"] += int(inv["balance_cents"])

            for sub in subscriptions:
                if sub["status"] in ("active", "past_due"):
                    b = ensure_bucket(sub["currency"])
                    qty = Decimal(sub["quantity"])
                    line_cents = Decimal(sub["unit_price_cents"]) * qty
                    divisor = {"monthly": Decimal("1"), "quarterly": Decimal("3"), "yearly": Decimal("12")}.get(
                        sub["interval"], Decimal("1")
                    )
                    b["mrr_cents"] += round_half_up(line_cents / divisor)

            return {
                "currencies": available_currencies,
                "selected_currency": filter_curr,
                "by_currency": by_currency,
                "status_counts": status_counts,
                "customer_count": sum(1 for c in data["customers"] if not c.get("archived")),
                "item_count": sum(1 for i in data["items"] if not i.get("archived")),
                "active_subscription_count": sum(
                    1 for s in data["subscriptions"] if s["status"] in ("active", "past_due")
                ),
            }
