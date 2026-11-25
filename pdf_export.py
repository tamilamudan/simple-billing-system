"""Lightweight pure-Python PDF 1.4 invoice generator (zero external dependencies)."""

from __future__ import annotations

from typing import Any

from billing_math import format_bps, format_cents


def _pdf_escape(text: str) -> str:
    safe = (text or "").encode("latin-1", "replace").decode("latin-1")
    return safe.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _wrap_text(text: str, max_chars: int) -> list[str]:
    words = (text or "").split()
    if not words:
        return [""]
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        if len(current) + 1 + len(word) <= max_chars:
            current += " " + word
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


class _PdfCanvas:
    def __init__(self) -> None:
        self.pages: list[list[str]] = [[]]
        self.width = 595
        self.height = 842

    def new_page(self) -> None:
        self.pages.append([])

    def _ops(self) -> list[str]:
        return self.pages[-1]

    def text(self, x: float, y: float, value: str, *, size: int = 10, bold: bool = False, gray: float = 0.1) -> None:
        font = "/F2" if bold else "/F1"
        self._ops().append(
            f"BT {gray:.2f} g {font} {size} Tf 1 0 0 1 {x:.1f} {y:.1f} Tm ({_pdf_escape(value)}) Tj ET"
        )

    def text_right(self, right_x: float, y: float, value: str, *, size: int = 10, bold: bool = False, gray: float = 0.1) -> None:
        # Approximate character width for Helvetica (~0.52 * size)
        approx_w = len(value) * size * 0.50
        self.text(max(40.0, right_x - approx_w), y, value, size=size, bold=bold, gray=gray)

    def line(self, x1: float, y1: float, x2: float, y2: float, *, gray: float = 0.82, width: float = 0.7) -> None:
        self._ops().append(f"{gray:.2f} G {width:.2f} w {x1:.1f} {y1:.1f} m {x2:.1f} {y2:.1f} l S")

    def rect(self, x: float, y: float, w: float, h: float, *, fill_gray: float = 0.95) -> None:
        self._ops().append(f"{fill_gray:.2f} g {x:.1f} {y:.1f} {w:.1f} {h:.1f} re f")

    def build_bytes(self) -> bytes:
        objects: list[bytes] = []

        # 1: Catalog, 2: Pages, 3: Helvetica, 4: Helvetica-Bold
        objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
        page_refs = []
        content_start_id = 5
        page_objs: list[bytes] = []
        content_objs: list[bytes] = []

        for idx, ops in enumerate(self.pages):
            content_id = content_start_id + idx * 2
            page_id = content_id + 1
            stream_data = "\n".join(ops).encode("latin-1", "replace")
            content_objs.append(
                f"<< /Length {len(stream_data)} >>\nstream\n".encode("ascii")
                + stream_data
                + b"\nendstream"
            )
            page_objs.append(
                (
                    f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {self.width} {self.height}] "
                    f"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents {content_id} 0 R >>"
                ).encode("ascii")
            )
            page_refs.append(f"{page_id} 0 R")

        objects.append(f"<< /Type /Pages /Kids [{' '.join(page_refs)}] /Count {len(page_refs)} >>".encode("ascii"))
        objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
        objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>")

        for c_obj, p_obj in zip(content_objs, page_objs):
            objects.append(c_obj)
            objects.append(p_obj)

        out = bytearray(b"%PDF-1.4\n")
        offsets = [0]
        for obj_id, body in enumerate(objects, start=1):
            offsets.append(len(out))
            out.extend(f"{obj_id} 0 obj\n".encode("ascii"))
            out.extend(body)
            out.extend(b"\nendobj\n")

        xref_offset = len(out)
        out.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
        out.extend(b"0000000000 65535 f \n")
        for off in offsets[1:]:
            out.extend(f"{off:010d} 00000 n \n".encode("ascii"))

        out.extend(
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode(
                "ascii"
            )
        )
        return bytes(out)


def generate_invoice_pdf(invoice: dict[str, Any], settings: dict[str, Any]) -> bytes:
    """Render an invoice dictionary into downloadable PDF bytes."""
    pdf = _PdfCanvas()
    curr = invoice["currency"]
    customer = invoice.get("customer") or {}

    y = 795.0
    # Header
    pdf.text(44, y, settings.get("company_name", "Billing Co."), size=16, bold=True, gray=0.08)
    pdf.text_right(551, y, "INVOICE", size=18, bold=True, gray=0.12)

    y -= 18
    for addr_line in _wrap_text(settings.get("company_address", ""), 48):
        pdf.text(44, y, addr_line, size=9, gray=0.35)
        y -= 12
    if settings.get("company_tax_id"):
        pdf.text(44, y, f"Tax ID: {settings['company_tax_id']}", size=9, gray=0.35)
        y -= 12
    if settings.get("company_email"):
        pdf.text(44, y, settings["company_email"], size=9, gray=0.35)

    meta_y = 777.0
    pdf.text_right(551, meta_y, f"No: {invoice['invoice_number']}", size=10, bold=True)
    pdf.text_right(551, meta_y - 15, f"Status: {invoice['status'].upper()}", size=10, bold=True, gray=0.25)
    pdf.text_right(551, meta_y - 30, f"Issue Date: {invoice['issue_date']}", size=9, gray=0.3)
    pdf.text_right(551, meta_y - 43, f"Due Date: {invoice['due_date']}", size=9, gray=0.3)

    y = min(y, meta_y - 58) - 10
    pdf.line(44, y, 551, y)
    y -= 20

    # Bill To
    pdf.text(44, y, "BILL TO", size=9, bold=True, gray=0.45)
    y -= 14
    pdf.text(44, y, customer.get("name", "Customer"), size=11, bold=True)
    y -= 13
    if customer.get("email"):
        pdf.text(44, y, customer["email"], size=9, gray=0.3)
        y -= 12
    if customer.get("billing_address"):
        for addr_line in _wrap_text(customer["billing_address"], 65):
            pdf.text(44, y, addr_line, size=9, gray=0.3)
            y -= 12
    if customer.get("tax_id"):
        pdf.text(44, y, f"Tax ID / GSTIN: {customer['tax_id']}", size=9, gray=0.3)
        y -= 12

    y -= 14

    def draw_table_header(curr_y: float) -> float:
        pdf.rect(44, curr_y - 6, 507, 20, fill_gray=0.93)
        pdf.text(50, curr_y, "Description", size=9, bold=True)
        pdf.text_right(335, curr_y, "Qty", size=9, bold=True)
        pdf.text_right(415, curr_y, "Unit Price", size=9, bold=True)
        pdf.text_right(472, curr_y, "Tax", size=9, bold=True)
        pdf.text_right(545, curr_y, "Line Total", size=9, bold=True)
        return curr_y - 22

    y = draw_table_header(y)

    for line in invoice.get("lines", []):
        desc_lines = _wrap_text(line["description"], 44)
        row_height = max(18, len(desc_lines) * 12 + 6)
        if y - row_height < 110:
            pdf.new_page()
            y = draw_table_header(780.0)

        for idx, dline in enumerate(desc_lines):
            pdf.text(50, y - idx * 12, dline, size=9, gray=0.12)
        pdf.text_right(335, y, str(line["quantity"]), size=9)
        pdf.text_right(415, y, format_cents(line["unit_price_cents"], curr, ascii_only=True), size=9)
        pdf.text_right(472, y, format_bps(line["tax_rate_bps"]), size=9)
        pdf.text_right(545, y, format_cents(line["line_total_cents"], curr, ascii_only=True), size=9, bold=True)
        y -= row_height
        pdf.line(44, y + 6, 551, y + 6, gray=0.92, width=0.4)

    if y < 180:
        pdf.new_page()
        y = 770.0

    y -= 12
    # Totals block
    pdf.text_right(440, y, "Subtotal:", size=9, gray=0.35)
    pdf.text_right(545, y, format_cents(invoice["subtotal_cents"], curr, ascii_only=True), size=9)
    y -= 15
    if invoice.get("discount_cents", 0) > 0:
        pdf.text_right(440, y, "Discount:", size=9, gray=0.35)
        pdf.text_right(545, y, "-" + format_cents(invoice["discount_cents"], curr, ascii_only=True), size=9)
        y -= 15
    pdf.text_right(440, y, "Tax:", size=9, gray=0.35)
    pdf.text_right(545, y, format_cents(invoice["tax_cents"], curr, ascii_only=True), size=9)
    y -= 16
    pdf.line(350, y + 10, 551, y + 10, gray=0.7)
    pdf.text_right(440, y, "Total:", size=11, bold=True)
    pdf.text_right(545, y, format_cents(invoice["total_cents"], curr, ascii_only=True), size=11, bold=True)
    y -= 16
    pdf.text_right(440, y, "Paid:", size=9, gray=0.35)
    pdf.text_right(545, y, format_cents(invoice.get("paid_cents", 0), curr, ascii_only=True), size=9)
    y -= 15
    if invoice.get("refunded_cents", 0) > 0:
        pdf.text_right(440, y, "Refunded:", size=9, gray=0.35)
        pdf.text_right(545, y, format_cents(invoice["refunded_cents"], curr, ascii_only=True), size=9)
        y -= 15
    pdf.text_right(440, y, "Balance Due:", size=10, bold=True)
    pdf.text_right(545, y, format_cents(invoice["balance_cents"], curr, ascii_only=True), size=10, bold=True)

    if invoice.get("notes"):
        y -= 28
        pdf.text(44, y, "Notes", size=9, bold=True, gray=0.35)
        y -= 13
        for nline in _wrap_text(invoice["notes"], 85):
            pdf.text(44, y, nline, size=9, gray=0.25)
            y -= 12

    pdf.text(44, 42, f"Generated by {settings.get('company_name', 'Northstar Billing Co.')}", size=8, gray=0.5)
    return pdf.build_bytes()
