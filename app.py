"""Flask application providing both clean HTML/CSS UI and structured REST API."""

from __future__ import annotations

import csv
import io
import os
from pathlib import Path
from typing import Any

from flask import (
    Flask,
    Response,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from billing_math import MoneyValidationError, SUPPORTED_CURRENCIES, format_bps, format_cents, parse_money_to_cents
from pdf_export import generate_invoice_pdf
from seed import DEFAULT_DATA_FILE, seed_store
from store import (
    BILLING_INTERVALS,
    BillingStore,
    DomainError,
    INVOICE_STATUSES,
    NotFoundError,
    PAYMENT_METHODS,
)


def create_app(data_path: str | Path | None = None, *, auto_seed: bool = True) -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.secret_key = os.environ.get("BILLING_SECRET_KEY", "dev-billing-secret-key")

    store_file = Path(data_path) if data_path else Path(os.environ.get("BILLING_DATA_FILE", DEFAULT_DATA_FILE))
    store = BillingStore(store_file)
    if auto_seed:
        seed_store(store, force=False)
    app.config["BILLING_STORE"] = store

    # ------------------------------------------------------------------
    # Template filters & globals
    # ------------------------------------------------------------------
    @app.template_filter("money")
    def money_filter(cents: int | None, currency: str = "USD") -> str:
        return format_cents(int(cents or 0), currency)

    @app.template_filter("bps")
    def bps_filter(bps: int | None) -> str:
        return format_bps(int(bps or 0))

    @app.template_filter("cents_input")
    def cents_input_filter(cents: int | None) -> str:
        val = int(cents or 0)
        sign = "-" if val < 0 else ""
        whole, frac = divmod(abs(val), 100)
        return f"{sign}{whole}.{frac:02d}"

    @app.template_filter("bps_input")
    def bps_input_filter(bps: int | None) -> str:
        text = format_bps(int(bps or 0))
        return text[:-1] if text.endswith("%") else text

    @app.context_processor
    def inject_globals() -> dict[str, Any]:
        return {
            "app_settings": store.get_settings(),
            "supported_currencies": SUPPORTED_CURRENCIES,
            "payment_methods": PAYMENT_METHODS,
            "invoice_statuses": INVOICE_STATUSES,
            "billing_intervals": BILLING_INTERVALS,
        }

    def _safe_next_url(default_endpoint: str) -> str:
        target = request.form.get("next") or request.referrer
        if target and target.startswith("/"):
            return target
        return url_for(default_endpoint)

    def _extract_invoice_lines_from_form() -> list[dict[str, Any]]:
        item_ids = request.form.getlist("line_item_id")
        descriptions = request.form.getlist("line_description")
        quantities = request.form.getlist("line_quantity")
        unit_prices = request.form.getlist("line_unit_price")
        tax_rates = request.form.getlist("line_tax_rate")

        count = max(len(descriptions), len(item_ids), len(quantities), len(unit_prices), len(tax_rates))
        lines: list[dict[str, Any]] = []
        for idx in range(count):
            item_id = item_ids[idx].strip() if idx < len(item_ids) else ""
            desc = descriptions[idx].strip() if idx < len(descriptions) else ""
            qty = quantities[idx].strip() if idx < len(quantities) else "1"
            price = unit_prices[idx].strip() if idx < len(unit_prices) else ""
            tax = tax_rates[idx].strip() if idx < len(tax_rates) else "0"
            if not item_id and not desc and not price:
                continue
            lines.append(
                {
                    "item_id": item_id or None,
                    "description": desc,
                    "quantity": qty or "1",
                    "unit_price": price,
                    "tax_rate": tax or "0",
                }
            )
        return lines

    # ==================================================================
    # HTML Routes
    # ==================================================================
    @app.get("/")
    def dashboard() -> str:
        currency = request.args.get("currency", "").strip().upper()
        status = request.args.get("status", "").strip().lower()
        search = request.args.get("q", "").strip()
        date_from = request.args.get("date_from", "").strip()
        date_to = request.args.get("date_to", "").strip()

        summary = store.get_dashboard_summary(currency=currency)
        invoices = store.list_invoices(
            status=status,
            currency=currency,
            search=search,
            date_from=date_from,
            date_to=date_to,
        )
        return render_template(
            "dashboard.html",
            summary=summary,
            invoices=invoices,
            filters={
                "currency": currency,
                "status": status,
                "q": search,
                "date_from": date_from,
                "date_to": date_to,
            },
        )

    # --- Customers ---
    @app.get("/customers")
    def customers_page() -> str:
        search = request.args.get("q", "").strip()
        show_archived = request.args.get("archived") == "1"
        edit_id = request.args.get("edit", "").strip()
        customers = store.list_customers(search=search, include_archived=show_archived)
        edit_customer = None
        if edit_id:
            try:
                edit_customer = store.get_customer(edit_id)
            except NotFoundError:
                flash("Customer not found.", "error")
        return render_template(
            "customers.html",
            customers=customers,
            edit_customer=edit_customer,
            search=search,
            show_archived=show_archived,
        )

    @app.post("/customers")
    def create_customer_form() -> Response:
        try:
            cust = store.create_customer(request.form.to_dict())
            flash(f"Customer '{cust['name']}' created.", "success")
        except DomainError as exc:
            flash(str(exc), "error")
        return redirect(url_for("customers_page"))

    @app.post("/customers/<customer_id>/edit")
    def edit_customer_form(customer_id: str) -> Response:
        try:
            cust = store.update_customer(customer_id, request.form.to_dict())
            flash(f"Customer '{cust['name']}' updated.", "success")
        except DomainError as exc:
            flash(str(exc), "error")
        return redirect(url_for("customers_page"))

    @app.post("/customers/<customer_id>/archive")
    def toggle_customer_archive_form(customer_id: str) -> Response:
        archived = request.form.get("archived", "1") == "1"
        try:
            cust = store.set_customer_archived(customer_id, archived)
            flash(f"Customer '{cust['name']}' {'archived' if archived else 'restored'}.", "success")
        except DomainError as exc:
            flash(str(exc), "error")
        return redirect(url_for("customers_page", archived="1" if not archived else ""))

    # --- Catalog ---
    @app.get("/catalog")
    def catalog_page() -> str:
        search = request.args.get("q", "").strip()
        item_type = request.args.get("type", "").strip()
        show_archived = request.args.get("archived") == "1"
        edit_id = request.args.get("edit", "").strip()
        items = store.list_items(search=search, item_type=item_type, include_archived=show_archived)
        edit_item = None
        if edit_id:
            try:
                edit_item = store.get_item(edit_id)
            except NotFoundError:
                flash("Catalog item not found.", "error")
        return render_template(
            "catalog.html",
            items=items,
            edit_item=edit_item,
            search=search,
            item_type=item_type,
            show_archived=show_archived,
        )

    @app.post("/catalog")
    def create_item_form() -> Response:
        try:
            item = store.create_item(request.form.to_dict())
            flash(f"Item '{item['name']}' added to catalog.", "success")
        except DomainError as exc:
            flash(str(exc), "error")
        return redirect(url_for("catalog_page"))

    @app.post("/catalog/<item_id>/edit")
    def edit_item_form(item_id: str) -> Response:
        try:
            item = store.update_item(item_id, request.form.to_dict())
            flash(f"Item '{item['name']}' updated.", "success")
        except DomainError as exc:
            flash(str(exc), "error")
        return redirect(url_for("catalog_page"))

    @app.post("/catalog/<item_id>/archive")
    def toggle_item_archive_form(item_id: str) -> Response:
        archived = request.form.get("archived", "1") == "1"
        try:
            item = store.set_item_archived(item_id, archived)
            flash(f"Item '{item['name']}' {'archived' if archived else 'restored'}.", "success")
        except DomainError as exc:
            flash(str(exc), "error")
        return redirect(url_for("catalog_page"))

    # --- Invoices ---
    @app.get("/invoices")
    def invoices_page() -> str:
        status = request.args.get("status", "").strip().lower()
        currency = request.args.get("currency", "").strip().upper()
        search = request.args.get("q", "").strip()
        invoices = store.list_invoices(status=status, currency=currency, search=search)
        return render_template(
            "invoices.html",
            invoices=invoices,
            filters={"status": status, "currency": currency, "q": search},
        )

    @app.get("/invoices/new")
    def new_invoice_page() -> str:
        customers = store.list_customers()
        items = store.list_items()
        return render_template("invoice_form.html", invoice=None, customers=customers, items=items)

    @app.post("/invoices/new")
    def create_invoice_form() -> Response | str:
        lines = _extract_invoice_lines_from_form()
        payload = {
            "customer_id": request.form.get("customer_id", ""),
            "status": "sent" if request.form.get("action") == "send" else "draft",
            "issue_date": request.form.get("issue_date", ""),
            "due_date": request.form.get("due_date", ""),
            "discount_type": request.form.get("discount_type", "none"),
            "discount_value_raw": request.form.get("discount_value", "0"),
            "notes": request.form.get("notes", ""),
            "lines": lines,
        }
        try:
            inv = store.create_invoice(payload)
            flash(f"Invoice {inv['invoice_number']} created ({inv['status']}).", "success")
            return redirect(url_for("invoice_detail_page", invoice_id=inv["id"]))
        except DomainError as exc:
            flash(str(exc), "error")
            return redirect(url_for("new_invoice_page"))

    @app.get("/invoices/<invoice_id>")
    def invoice_detail_page(invoice_id: str) -> Response | str:
        try:
            invoice = store.get_invoice(invoice_id)
        except NotFoundError:
            flash("Invoice not found.", "error")
            return redirect(url_for("invoices_page"))
        return render_template("invoice_detail.html", invoice=invoice)

    @app.get("/invoices/<invoice_id>/edit")
    def edit_invoice_page(invoice_id: str) -> Response | str:
        try:
            invoice = store.get_invoice(invoice_id)
        except NotFoundError:
            flash("Invoice not found.", "error")
            return redirect(url_for("invoices_page"))
        if invoice["status"] != "draft":
            flash("Only Draft invoices can be edited.", "error")
            return redirect(url_for("invoice_detail_page", invoice_id=invoice_id))
        customers = store.list_customers()
        items = store.list_items()
        return render_template("invoice_form.html", invoice=invoice, customers=customers, items=items)

    @app.post("/invoices/<invoice_id>/edit")
    def update_invoice_form(invoice_id: str) -> Response:
        lines = _extract_invoice_lines_from_form()
        payload = {
            "customer_id": request.form.get("customer_id", ""),
            "issue_date": request.form.get("issue_date", ""),
            "due_date": request.form.get("due_date", ""),
            "discount_type": request.form.get("discount_type", "none"),
            "discount_value_raw": request.form.get("discount_value", "0"),
            "notes": request.form.get("notes", ""),
            "lines": lines,
        }
        try:
            inv = store.update_draft_invoice(invoice_id, payload)
            if request.form.get("action") == "send":
                store.send_invoice(invoice_id)
                flash(f"Invoice {inv['invoice_number']} updated and marked Sent.", "success")
            else:
                flash(f"Draft invoice {inv['invoice_number']} updated.", "success")
            return redirect(url_for("invoice_detail_page", invoice_id=invoice_id))
        except DomainError as exc:
            flash(str(exc), "error")
            return redirect(url_for("edit_invoice_page", invoice_id=invoice_id))

    @app.post("/invoices/<invoice_id>/send")
    def send_invoice_form(invoice_id: str) -> Response:
        try:
            inv = store.send_invoice(invoice_id)
            flash(f"Invoice {inv['invoice_number']} marked as {inv['status'].title()}.", "success")
        except DomainError as exc:
            flash(str(exc), "error")
        return redirect(url_for("invoice_detail_page", invoice_id=invoice_id))

    @app.post("/invoices/<invoice_id>/void")
    def void_invoice_form(invoice_id: str) -> Response:
        try:
            inv = store.void_invoice(invoice_id, reason=request.form.get("reason", ""))
            flash(f"Invoice {inv['invoice_number']} voided.", "success")
        except DomainError as exc:
            flash(str(exc), "error")
        return redirect(url_for("invoice_detail_page", invoice_id=invoice_id))

    @app.post("/invoices/<invoice_id>/refund")
    def refund_invoice_form(invoice_id: str) -> Response:
        raw_amount = request.form.get("amount", "").strip()
        try:
            amt_cents = parse_money_to_cents(raw_amount, allow_zero=False) if raw_amount else None
            inv = store.refund_invoice(
                invoice_id,
                amount_cents=amt_cents,
                reason=request.form.get("reason", ""),
            )
            flash(f"Refund recorded for {inv['invoice_number']}.", "success")
        except (DomainError, ValueError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("invoice_detail_page", invoice_id=invoice_id))

    @app.get("/invoices/<invoice_id>/pdf")
    def download_invoice_pdf(invoice_id: str) -> Response:
        try:
            invoice = store.get_invoice(invoice_id)
        except NotFoundError:
            flash("Invoice not found.", "error")
            return redirect(url_for("invoices_page"))
        pdf_bytes = generate_invoice_pdf(invoice, store.get_settings())
        filename = f"{invoice['invoice_number']}.pdf"
        return Response(
            pdf_bytes,
            mimetype="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    # --- Payments & Immutable Ledger ---
    @app.get("/payments")
    def payments_page() -> str:
        open_invoices = [
            inv for inv in store.list_invoices() if inv["status"] in ("sent", "overdue") and inv["balance_cents"] > 0
        ]
        payments = store.list_payments()
        ledger = store.list_ledger()
        return render_template(
            "payments.html",
            open_invoices=open_invoices,
            payments=payments,
            ledger=ledger,
        )

    @app.post("/payments")
    def record_payment_form() -> Response:
        try:
            payment = store.record_payment(
                {
                    "invoice_id": request.form.get("invoice_id", ""),
                    "amount": request.form.get("amount", ""),
                    "method": request.form.get("method", "card"),
                    "reference": request.form.get("reference", ""),
                    "paid_at": request.form.get("paid_at", ""),
                    "simulate_failure": request.form.get("simulate_failure") == "1",
                }
            )
            if payment["status"] == "failed":
                flash("Simulated failed payment logged to the ledger.", "error")
            else:
                flash("Payment recorded and ledger updated.", "success")
        except DomainError as exc:
            flash(str(exc), "error")
        return redirect(_safe_next_url("payments_page"))

    # --- Recurring Subscriptions & Dunning ---
    @app.get("/subscriptions")
    def subscriptions_page() -> str:
        settings = store.get_settings()
        subscriptions = store.list_subscriptions()
        customers = store.list_customers()
        recurring_items = store.list_items(item_type="recurring")
        return render_template(
            "subscriptions.html",
            settings=settings,
            subscriptions=subscriptions,
            customers=customers,
            recurring_items=recurring_items,
        )

    @app.post("/subscriptions")
    def create_subscription_form() -> Response:
        try:
            store.create_subscription(request.form.to_dict())
            flash("Subscription started.", "success")
        except DomainError as exc:
            flash(str(exc), "error")
        return redirect(url_for("subscriptions_page"))

    @app.post("/subscriptions/<sub_id>/change-plan")
    def change_plan_form(sub_id: str) -> Response:
        try:
            res = store.change_subscription_plan(
                sub_id,
                request.form.get("new_item_id", ""),
                effective_date=request.form.get("effective_date") or None,
            )
            net = res["proration"]["net_proration_cents"]
            if res["invoice"]:
                flash(
                    f"Plan upgraded with proration invoice {res['invoice']['invoice_number']}.",
                    "success",
                )
            elif net < 0:
                flash("Plan downgraded; proration credit logged to ledger.", "success")
            else:
                flash("Subscription plan updated.", "success")
        except DomainError as exc:
            flash(str(exc), "error")
        return redirect(url_for("subscriptions_page"))

    @app.post("/subscriptions/<sub_id>/renew")
    def renew_subscription_form(sub_id: str) -> Response:
        simulate_failure = request.form.get("simulate_failure") == "1"
        try:
            res = store.renew_subscription(
                sub_id,
                simulate_failure=simulate_failure,
                payment_method=request.form.get("payment_method", "card"),
            )
            if simulate_failure:
                flash(
                    f"Simulated charge failure on {res['invoice']['invoice_number']}. Entered dunning Retry 1.",
                    "error",
                )
            else:
                flash(f"Renewal invoice {res['invoice']['invoice_number']} generated and paid.", "success")
        except DomainError as exc:
            flash(str(exc), "error")
        return redirect(url_for("subscriptions_page"))

    @app.post("/subscriptions/<sub_id>/retry")
    def retry_dunning_form(sub_id: str) -> Response:
        simulate_success = request.form.get("simulate_success", "1") == "1"
        try:
            sub = store.retry_dunning(
                sub_id,
                simulate_success=simulate_success,
                payment_method=request.form.get("payment_method", "card"),
            )
            if simulate_success:
                flash("Dunning payment retry succeeded; subscription is Active.", "success")
            else:
                flash(f"Retry failed; dunning state is now {sub['dunning_state']}.", "error")
        except DomainError as exc:
            flash(str(exc), "error")
        return redirect(url_for("subscriptions_page"))

    @app.post("/subscriptions/<sub_id>/remind")
    def remind_dunning_form(sub_id: str) -> Response:
        try:
            store.send_dunning_reminder(sub_id)
            flash("Payment reminder event logged.", "success")
        except DomainError as exc:
            flash(str(exc), "error")
        return redirect(url_for("subscriptions_page"))

    @app.post("/subscriptions/<sub_id>/cancel")
    def cancel_subscription_form(sub_id: str) -> Response:
        try:
            store.cancel_subscription(sub_id)
            flash("Subscription canceled.", "success")
        except DomainError as exc:
            flash(str(exc), "error")
        return redirect(url_for("subscriptions_page"))

    # --- Settings ---
    @app.get("/settings")
    def settings_page() -> str:
        return render_template("settings.html", settings=store.get_settings())

    @app.post("/settings")
    def update_settings_form() -> Response:
        try:
            payload = {
                "company_name": request.form.get("company_name", ""),
                "company_address": request.form.get("company_address", ""),
                "company_tax_id": request.form.get("company_tax_id", ""),
                "company_email": request.form.get("company_email", ""),
                "invoice_prefix": request.form.get("invoice_prefix", ""),
                "recurring_enabled": request.form.get("recurring_enabled") == "1",
            }
            store.update_settings(payload)
            flash("Settings saved.", "success")
        except DomainError as exc:
            flash(str(exc), "error")
        return redirect(url_for("settings_page"))

    @app.post("/settings/toggle-recurring")
    def toggle_recurring_form() -> Response:
        settings = store.toggle_recurring()
        state_text = "enabled" if settings["recurring_enabled"] else "disabled"
        flash(f"Recurring Billing & Dunning {state_text}.", "success")
        return redirect(_safe_next_url("dashboard"))

    # --- CSV Accounting Exports ---
    @app.get("/export/invoices.csv")
    def export_invoices_csv() -> Response:
        status = request.args.get("status", "").strip().lower()
        currency = request.args.get("currency", "").strip().upper()
        search = request.args.get("q", "").strip()
        date_from = request.args.get("date_from", "").strip()
        date_to = request.args.get("date_to", "").strip()
        invoices = store.list_invoices(
            status=status, currency=currency, search=search, date_from=date_from, date_to=date_to
        )

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(
            [
                "Invoice Number",
                "Customer",
                "Status",
                "Currency",
                "Issue Date",
                "Due Date",
                "Subtotal",
                "Discount",
                "Tax",
                "Total",
                "Paid",
                "Refunded",
                "Balance Due",
            ]
        )
        for inv in invoices:
            writer.writerow(
                [
                    inv["invoice_number"],
                    inv["customer_name"],
                    inv["status"],
                    inv["currency"],
                    inv["issue_date"],
                    inv["due_date"],
                    cents_input_filter(inv["subtotal_cents"]),
                    cents_input_filter(inv["discount_cents"]),
                    cents_input_filter(inv["tax_cents"]),
                    cents_input_filter(inv["total_cents"]),
                    cents_input_filter(inv["paid_cents"]),
                    cents_input_filter(inv.get("refunded_cents", 0)),
                    cents_input_filter(inv["balance_cents"]),
                ]
            )
        return Response(
            buf.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": 'attachment; filename="invoices_export.csv"'},
        )

    @app.get("/export/ledger.csv")
    def export_ledger_csv() -> Response:
        entries = store.list_ledger()
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(
            [
                "Entry ID",
                "Entry Date",
                "Entry Type",
                "Direction",
                "Customer",
                "Invoice Number",
                "Currency",
                "Amount",
                "Balance After",
                "Note",
                "Created At",
            ]
        )
        for entry in entries:
            writer.writerow(
                [
                    entry["id"],
                    entry["entry_date"],
                    entry["entry_type"],
                    entry["direction"],
                    entry["customer_name"],
                    entry["invoice_number"],
                    entry["currency"],
                    cents_input_filter(entry["amount_cents"]),
                    cents_input_filter(entry["balance_after_cents"]),
                    entry["note"],
                    entry["created_at"],
                ]
            )
        return Response(
            buf.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": 'attachment; filename="accounting_ledger.csv"'},
        )

    # ==================================================================
    # JSON REST API Routes
    # ==================================================================
    def _json_body() -> dict[str, Any]:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise DomainError("Request body must be a valid JSON object.")
        return data

    @app.errorhandler(DomainError)
    def handle_domain_error(exc: DomainError) -> tuple[Response, int] | Response:
        if request.path.startswith("/api/"):
            return jsonify({"error": str(exc)}), exc.status_code
        flash(str(exc), "error")
        return redirect(_safe_next_url("dashboard"))

    @app.get("/api/health")
    def api_health() -> Response:
        return jsonify({"status": "ok"})

    @app.get("/api/reports/summary")
    def api_summary() -> Response:
        currency = request.args.get("currency", "").strip().upper()
        return jsonify(store.get_dashboard_summary(currency=currency))

    @app.get("/api/settings")
    def api_get_settings() -> Response:
        return jsonify(store.get_settings())

    @app.put("/api/settings")
    def api_put_settings() -> Response:
        return jsonify(store.update_settings(_json_body()))

    @app.get("/api/customers")
    def api_list_customers() -> Response:
        return jsonify(
            store.list_customers(
                search=request.args.get("q", ""),
                include_archived=request.args.get("archived") == "1",
            )
        )

    @app.post("/api/customers")
    def api_create_customer() -> tuple[Response, int]:
        return jsonify(store.create_customer(_json_body())), 201

    @app.get("/api/customers/<customer_id>")
    def api_get_customer(customer_id: str) -> Response:
        return jsonify(store.get_customer(customer_id))

    @app.put("/api/customers/<customer_id>")
    def api_update_customer(customer_id: str) -> Response:
        return jsonify(store.update_customer(customer_id, _json_body()))

    @app.post("/api/customers/<customer_id>/archive")
    def api_archive_customer(customer_id: str) -> Response:
        body = request.get_json(silent=True) or {}
        return jsonify(store.set_customer_archived(customer_id, bool(body.get("archived", True))))

    @app.get("/api/items")
    def api_list_items() -> Response:
        return jsonify(
            store.list_items(
                search=request.args.get("q", ""),
                item_type=request.args.get("type", ""),
                include_archived=request.args.get("archived") == "1",
            )
        )

    @app.post("/api/items")
    def api_create_item() -> tuple[Response, int]:
        return jsonify(store.create_item(_json_body())), 201

    @app.get("/api/items/<item_id>")
    def api_get_item(item_id: str) -> Response:
        return jsonify(store.get_item(item_id))

    @app.put("/api/items/<item_id>")
    def api_update_item(item_id: str) -> Response:
        return jsonify(store.update_item(item_id, _json_body()))

    @app.post("/api/items/<item_id>/archive")
    def api_archive_item(item_id: str) -> Response:
        body = request.get_json(silent=True) or {}
        return jsonify(store.set_item_archived(item_id, bool(body.get("archived", True))))

    @app.get("/api/invoices")
    def api_list_invoices() -> Response:
        return jsonify(
            store.list_invoices(
                status=request.args.get("status", ""),
                customer_id=request.args.get("customer_id", ""),
                currency=request.args.get("currency", ""),
                search=request.args.get("q", ""),
                date_from=request.args.get("date_from", ""),
                date_to=request.args.get("date_to", ""),
            )
        )

    @app.post("/api/invoices")
    def api_create_invoice() -> tuple[Response, int]:
        return jsonify(store.create_invoice(_json_body())), 201

    @app.get("/api/invoices/<invoice_id>")
    def api_get_invoice(invoice_id: str) -> Response:
        return jsonify(store.get_invoice(invoice_id))

    @app.put("/api/invoices/<invoice_id>")
    def api_update_invoice(invoice_id: str) -> Response:
        return jsonify(store.update_draft_invoice(invoice_id, _json_body()))

    @app.post("/api/invoices/<invoice_id>/send")
    def api_send_invoice(invoice_id: str) -> Response:
        body = request.get_json(silent=True) or {}
        return jsonify(store.send_invoice(invoice_id, as_of=body.get("as_of")))

    @app.post("/api/invoices/<invoice_id>/void")
    def api_void_invoice(invoice_id: str) -> Response:
        body = request.get_json(silent=True) or {}
        return jsonify(store.void_invoice(invoice_id, reason=str(body.get("reason", ""))))

    @app.post("/api/invoices/<invoice_id>/refund")
    def api_refund_invoice(invoice_id: str) -> Response:
        body = request.get_json(silent=True) or {}
        amount_cents = body.get("amount_cents")
        if amount_cents is None and body.get("amount") is not None:
            try:
                amount_cents = parse_money_to_cents(body["amount"], allow_zero=False)
            except MoneyValidationError as exc:
                raise DomainError(str(exc)) from exc
        return jsonify(
            store.refund_invoice(
                invoice_id,
                amount_cents=amount_cents,
                reason=str(body.get("reason", "")),
            )
        )

    @app.get("/api/payments")
    def api_list_payments() -> Response:
        return jsonify(store.list_payments())

    @app.post("/api/payments")
    def api_record_payment() -> tuple[Response, int]:
        return jsonify(store.record_payment(_json_body())), 201

    @app.get("/api/ledger")
    def api_list_ledger() -> Response:
        return jsonify(
            store.list_ledger(
                customer_id=request.args.get("customer_id", ""),
                invoice_id=request.args.get("invoice_id", ""),
            )
        )

    @app.get("/api/subscriptions")
    def api_list_subscriptions() -> Response:
        return jsonify(store.list_subscriptions())

    @app.post("/api/subscriptions")
    def api_create_subscription() -> tuple[Response, int]:
        return jsonify(store.create_subscription(_json_body())), 201

    @app.post("/api/subscriptions/<sub_id>/change-plan")
    def api_change_plan(sub_id: str) -> Response:
        body = _json_body()
        return jsonify(
            store.change_subscription_plan(
                sub_id,
                str(body.get("new_item_id", "")),
                effective_date=body.get("effective_date"),
            )
        )

    @app.post("/api/subscriptions/<sub_id>/renew")
    def api_renew_subscription(sub_id: str) -> Response:
        body = request.get_json(silent=True) or {}
        return jsonify(
            store.renew_subscription(
                sub_id,
                simulate_failure=bool(body.get("simulate_failure", False)),
                payment_method=str(body.get("payment_method", "card")),
                as_of=body.get("as_of"),
            )
        )

    @app.post("/api/subscriptions/<sub_id>/retry")
    def api_retry_dunning(sub_id: str) -> Response:
        body = request.get_json(silent=True) or {}
        return jsonify(
            store.retry_dunning(
                sub_id,
                simulate_success=bool(body.get("simulate_success", True)),
                payment_method=str(body.get("payment_method", "card")),
                as_of=body.get("as_of"),
            )
        )

    @app.post("/api/subscriptions/<sub_id>/remind")
    def api_remind_dunning(sub_id: str) -> Response:
        return jsonify(store.send_dunning_reminder(sub_id))

    @app.post("/api/subscriptions/<sub_id>/cancel")
    def api_cancel_subscription(sub_id: str) -> Response:
        return jsonify(store.cancel_subscription(sub_id))

    return app


if __name__ == "__main__":
    application = create_app()
    port = int(os.environ.get("PORT", "5000"))
    application.run(host="127.0.0.1", port=port, debug=False)
