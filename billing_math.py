"""Exact integer-cent and Decimal arithmetic for billing, taxes, discounts, and proration."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Iterable


CURRENCY_SYMBOLS = {
    "USD": "$",
    "INR": "₹",
    "EUR": "€",
    "GBP": "£",
    "CAD": "CA$",
    "AUD": "A$",
}

PDF_CURRENCY_PREFIXES = {
    "USD": "$",
    "INR": "INR ",
    "EUR": "EUR ",
    "GBP": "GBP ",
    "CAD": "CA$",
    "AUD": "A$",
}

SUPPORTED_CURRENCIES = tuple(CURRENCY_SYMBOLS.keys())


class MoneyValidationError(ValueError):
    """Raised when a monetary, percentage, or quantity input is invalid."""


def round_half_up(value: Decimal) -> int:
    """Round a Decimal to the nearest integer using standard half-up rounding."""
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def parse_money_to_cents(raw: str | int | float, *, allow_zero: bool = True, allow_negative: bool = False) -> int:
    """Parse a currency string or number into integer cents using Decimal (never float math)."""
    if isinstance(raw, bool):
        raise MoneyValidationError("Invalid monetary amount.")
    text = str(raw).strip().replace(",", "")
    if not text:
        raise MoneyValidationError("Monetary amount is required.")
    try:
        dec = Decimal(text)
    except InvalidOperation as exc:
        raise MoneyValidationError(f"Invalid monetary amount: {raw!r}") from exc
    if not dec.is_finite():
        raise MoneyValidationError("Monetary amount must be finite.")

    # Ensure at most 2 decimal places
    if dec.as_tuple().exponent < -2:
        raise MoneyValidationError("Monetary amount cannot have more than 2 decimal places.")

    cents = round_half_up(dec * Decimal("100"))
    if not allow_negative and cents < 0:
        raise MoneyValidationError("Monetary amount cannot be negative.")
    if not allow_zero and cents == 0:
        raise MoneyValidationError("Monetary amount must be greater than zero.")
    return cents


def parse_percent_to_bps(raw: str | int | float, *, max_percent: int = 100) -> int:
    """Parse a percentage (e.g. '18' or '18.5') into basis points (1% = 100 bps)."""
    if isinstance(raw, bool):
        raise MoneyValidationError("Invalid percentage.")
    text = str(raw).strip().replace("%", "")
    if not text:
        return 0
    try:
        dec = Decimal(text)
    except InvalidOperation as exc:
        raise MoneyValidationError(f"Invalid percentage: {raw!r}") from exc
    if not dec.is_finite():
        raise MoneyValidationError("Percentage must be finite.")
    if dec < Decimal("0") or dec > Decimal(max_percent):
        raise MoneyValidationError(f"Percentage must be between 0 and {max_percent}.")
    if dec.as_tuple().exponent < -2:
        raise MoneyValidationError("Percentage cannot have more than 2 decimal places.")
    return round_half_up(dec * Decimal("100"))


def parse_quantity(raw: str | int | float) -> Decimal:
    """Parse a positive line-item quantity using Decimal."""
    if isinstance(raw, bool):
        raise MoneyValidationError("Invalid quantity.")
    text = str(raw).strip()
    if not text:
        raise MoneyValidationError("Quantity is required.")
    try:
        qty = Decimal(text)
    except InvalidOperation as exc:
        raise MoneyValidationError(f"Invalid quantity: {raw!r}") from exc
    if not qty.is_finite() or qty <= Decimal("0"):
        raise MoneyValidationError("Quantity must be greater than zero.")
    return qty.normalize()


def format_cents(cents: int, currency: str = "USD", *, ascii_only: bool = False) -> str:
    """Format integer cents into a human-readable currency string."""
    sign = "-" if cents < 0 else ""
    abs_cents = abs(int(cents))
    whole, frac = divmod(abs_cents, 100)
    code = (currency or "USD").upper()
    prefix = PDF_CURRENCY_PREFIXES.get(code, f"{code} ") if ascii_only else CURRENCY_SYMBOLS.get(code, f"{code} ")
    return f"{sign}{prefix}{whole:,}.{frac:02d}"


def format_bps(bps: int) -> str:
    """Format basis points (e.g. 1850) as a percentage string ('18.50%' or '18%')."""
    dec = (Decimal(int(bps)) / Decimal("100")).normalize()
    text = format(dec, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return f"{text}%"


@dataclass(frozen=True)
class LineInput:
    description: str
    quantity: Decimal
    unit_price_cents: int
    tax_rate_bps: int
    item_id: str | None = None
    is_proration: bool = False


@dataclass(frozen=True)
class CalculatedLine:
    item_id: str | None
    description: str
    quantity: str
    unit_price_cents: int
    tax_rate_bps: int
    line_subtotal_cents: int
    line_discount_cents: int
    line_tax_cents: int
    line_total_cents: int
    is_proration: bool


@dataclass(frozen=True)
class InvoiceCalculation:
    lines: list[CalculatedLine]
    subtotal_cents: int
    discount_cents: int
    tax_cents: int
    total_cents: int


def calculate_invoice_totals(
    items: Iterable[LineInput],
    discount_type: str = "none",
    discount_value: int = 0,
) -> InvoiceCalculation:
    """
    Calculate line and invoice totals using exact Decimal/integer-cent math.

    Business rule: Invoice-level discounts are applied BEFORE tax and distributed
    proportionally across line items with deterministic remainder penny allocation.
    """
    item_list = list(items)
    if not item_list:
        raise MoneyValidationError("An invoice must have at least one line item.")

    line_subtotals: list[int] = []
    for item in item_list:
        if item.unit_price_cents < 0 and not item.is_proration:
            raise MoneyValidationError("Unit price cannot be negative.")
        if item.tax_rate_bps < 0 or item.tax_rate_bps > 10000:
            raise MoneyValidationError("Tax rate must be between 0% and 100%.")
        sub = round_half_up(item.quantity * Decimal(item.unit_price_cents))
        line_subtotals.append(sub)

    subtotal_cents = sum(line_subtotals)

    if discount_type not in ("none", "percentage", "fixed"):
        raise MoneyValidationError(f"Unsupported discount type: {discount_type!r}")
    if discount_value < 0:
        raise MoneyValidationError("Discount value cannot be negative.")

    if discount_type == "none" or subtotal_cents <= 0:
        discount_cents = 0
    elif discount_type == "percentage":
        if discount_value > 10000:
            raise MoneyValidationError("Percentage discount cannot exceed 100%.")
        discount_cents = round_half_up(
            Decimal(subtotal_cents) * Decimal(discount_value) / Decimal("10000")
        )
    else:  # fixed
        discount_cents = min(int(discount_value), subtotal_cents)

    # Distribute discount_cents proportionally across positive-subtotal lines
    line_discounts = [0] * len(item_list)
    if discount_cents > 0 and subtotal_cents > 0:
        remainders: list[tuple[Decimal, int]] = []
        allocated = 0
        for idx, sub in enumerate(line_subtotals):
            if sub <= 0:
                remainders.append((Decimal("-1"), idx))
                continue
            exact = Decimal(discount_cents) * Decimal(sub) / Decimal(subtotal_cents)
            floored = int(exact)
            line_discounts[idx] = floored
            allocated += floored
            remainders.append((exact - Decimal(floored), idx))

        leftover = discount_cents - allocated
        remainders.sort(key=lambda pair: (-pair[0], pair[1]))
        for _, idx in remainders[:leftover]:
            if line_subtotals[idx] > 0:
                line_discounts[idx] += 1

    calculated_lines: list[CalculatedLine] = []
    total_tax_cents = 0

    for idx, item in enumerate(item_list):
        sub = line_subtotals[idx]
        disc = line_discounts[idx]
        taxable = sub - disc
        line_tax = round_half_up(Decimal(taxable) * Decimal(item.tax_rate_bps) / Decimal("10000"))
        line_total = taxable + line_tax
        total_tax_cents += line_tax

        qty_str = format(item.quantity.normalize(), "f")
        if "." in qty_str:
            qty_str = qty_str.rstrip("0").rstrip(".")

        calculated_lines.append(
            CalculatedLine(
                item_id=item.item_id,
                description=item.description.strip(),
                quantity=qty_str,
                unit_price_cents=item.unit_price_cents,
                tax_rate_bps=item.tax_rate_bps,
                line_subtotal_cents=sub,
                line_discount_cents=disc,
                line_tax_cents=line_tax,
                line_total_cents=line_total,
                is_proration=item.is_proration,
            )
        )

    total_cents = subtotal_cents - discount_cents + total_tax_cents
    return InvoiceCalculation(
        lines=calculated_lines,
        subtotal_cents=subtotal_cents,
        discount_cents=discount_cents,
        tax_cents=total_tax_cents,
        total_cents=total_cents,
    )


def calculate_balance(total_cents: int, paid_cents: int, refunded_cents: int = 0) -> int:
    """Compute remaining balance due on an invoice in integer cents."""
    net_paid = max(0, int(paid_cents) - int(refunded_cents))
    return max(0, int(total_cents) - net_paid)


def calculate_proration(
    old_price_cents: int,
    new_price_cents: int,
    total_days: int,
    remaining_days: int,
    quantity: Decimal = Decimal("1"),
) -> dict[str, int]:
    """
    Calculate day-based proration amounts when upgrading or downgrading a recurring plan.

    Returns unused_credit_cents (credit for old plan), new_charge_cents (charge for new plan),
    and net_proration_cents (positive = charge due, negative = credit).
    """
    if total_days <= 0:
        raise MoneyValidationError("Total billing period days must be positive.")
    if remaining_days < 0 or remaining_days > total_days:
        raise MoneyValidationError("Remaining days must be between 0 and total period days.")

    ratio = Decimal(remaining_days) / Decimal(total_days)
    unused_credit_cents = round_half_up(Decimal(old_price_cents) * quantity * ratio)
    new_charge_cents = round_half_up(Decimal(new_price_cents) * quantity * ratio)
    net_proration_cents = new_charge_cents - unused_credit_cents
    return {
        "unused_credit_cents": unused_credit_cents,
        "new_charge_cents": new_charge_cents,
        "net_proration_cents": net_proration_cents,
    }
