"""Section 3 - unit price normalization, landed cost and basket totals.

Every function here is pure. Given the same inputs it returns the same outputs,
which is what lets the dashboard and the approval package agree with each other
weeks apart.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from .types import DemandLine, LineInput, Policy, QuoteInput

ZERO = Decimal("0")
ONE = Decimal("1")
HUNDRED = Decimal("100")


class ConversionError(ValueError):
    """Raised when a line cannot be normalized - never guessed around."""


def q(value: Decimal, dp: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-dp), rounding=ROUND_HALF_UP)


def money(value: Decimal) -> Decimal:
    return q(value, 2)


def dstr(value) -> str | None:
    """Decimal as a plain display string, without trailing zeros.

    Values arriving from the database carry the column's scale - a 0% freight
    adjustment reads as 0.0000 - which is noise on screen.
    """
    if value is None:
        return None
    d = Decimal(str(value))
    return f"{d.normalize():f}"


def pct(numerator: Decimal, denominator: Decimal) -> Decimal | None:
    """Percentage variance, rounded to one decimal. None when undefined."""
    if denominator == 0:
        return None
    return q(numerator / denominator * HUNDRED, 1)


# ---------------------------------------------------------------------------
# 3.1 Unit price normalization to EUR per litre
# ---------------------------------------------------------------------------

def quantity_to_litres(
    quantity: Decimal, uom: str, density: Decimal | None, policy: Policy
) -> Decimal:
    """Convert a quantity expressed in L, gal or kg into litres."""
    unit = (uom or "").strip().upper()
    if unit in ("L", "LTR", "LITRE", "LITER", "LITRES", "LITERS"):
        return quantity
    if unit in ("GAL", "GALLON", "GALLONS", "US GAL"):
        return quantity * policy.gallon_to_litre
    if unit in ("KG", "KGS", "KILOGRAM", "KILOGRAMS"):
        if not density:
            raise ConversionError("kg to litre needs a density for this material")
        return quantity / density
    raise ConversionError(f"unsupported unit of measure: {uom!r}")


def price_to_eur_per_litre(
    unit_price: Decimal,
    currency: str,
    uom: str,
    density: Decimal | None,
    policy: Policy,
) -> tuple[Decimal, Decimal | None, Decimal | None, Decimal]:
    """Return (price_eur_per_litre, uom_factor, density_used, fx_rate).

    A price per kilogram becomes a price per litre by multiplying by density:
    EUR/kg x kg/L = EUR/L. A price per gallon is divided by litres per gallon.

    The returned price is unrounded. Rounding happens once, on the landed
    price, because rounding here as well would compound: the worked example in
    section 3.1 gives 2.72 USD/kg -> 2.8934 EUR/L, and the landed price at +5%
    freight is 3.04, which only comes out if the full 2.8934 carries through.
    """
    unit = (uom or "").strip().upper()
    density_used: Decimal | None = None
    factor: Decimal | None = None

    if unit in ("L", "LTR", "LITRE", "LITER", "LITRES", "LITERS"):
        per_litre = unit_price
        factor = ONE
    elif unit in ("GAL", "GALLON", "GALLONS", "US GAL"):
        factor = ONE / policy.gallon_to_litre
        per_litre = unit_price / policy.gallon_to_litre
    elif unit in ("KG", "KGS", "KILOGRAM", "KILOGRAMS"):
        if not density:
            raise ConversionError("kg to litre needs a density for this material")
        density_used = density
        factor = density
        per_litre = unit_price * density
    else:
        raise ConversionError(f"unsupported unit of measure: {uom!r}")

    code = (currency or "EUR").strip().upper()
    if code == "EUR":
        fx = ONE
    else:
        if code not in policy.fx:
            raise ConversionError(f"no FX rate configured for {code}")
        fx = policy.fx[code]

    return per_litre * fx, factor, density_used, fx


# ---------------------------------------------------------------------------
# 3.2 Landed cost adjustment
# ---------------------------------------------------------------------------

def freight_for(incoterm: str | None, policy: Policy) -> tuple[Decimal, str, bool]:
    """Freight uplift, the basis note shown beside it, and whether it matched.

    The percentages are fixed policy, looked up on the Incoterm code that
    Document AI extracted. An Incoterm with no row is the dangerous case: a
    silent 0% would make that supplier look cheaper than it is and could flip
    the ranking, so the caller is told the lookup failed.
    """
    key = (incoterm or "").strip().upper().split()[0] if incoterm else ""
    if key in policy.freight_by_incoterm:
        pct_value, basis = policy.freight_by_incoterm[key]
        return pct_value, basis, True
    named = key or "(none extracted)"
    return ZERO, f"No freight policy for Incoterm {named}; no adjustment applied", False


def apply_freight(price_eur_l: Decimal, freight_pct: Decimal, policy: Policy) -> Decimal:
    return q(price_eur_l * (ONE + freight_pct / HUNDRED), policy.price_rounding_dp)


# ---------------------------------------------------------------------------
# 3.3 / 3.4 Basket totals
# ---------------------------------------------------------------------------

def applicable_discount(
    quote: QuoteInput, matched_cas: set[str], demand: dict[str, DemandLine]
) -> tuple[Decimal, str | None, bool]:
    """Largest discount whose condition the buyer actually meets.

    A FULL_BASKET discount only applies when the quote covers every material
    in the demand basket. Anything conditional that is not met returns zero.
    """
    best_pct, best_text, met = ZERO, None, False
    for d in quote.discounts:
        condition_met = False
        if d.condition_type == "UNCONDITIONAL":
            condition_met = True
        elif d.condition_type == "FULL_BASKET":
            condition_met = set(demand).issubset(matched_cas)
        elif d.condition_type in ("MIN_VALUE", "MIN_QTY"):
            # Threshold checks need the basket total, which is not known yet.
            # Treated as not met so a discount is never assumed.
            condition_met = False
        if condition_met and d.discount_pct > best_pct:
            best_pct, best_text, met = d.discount_pct, d.condition_text, True
    return best_pct, best_text, met


def ceiling_equivalent_total(
    demand: dict[str, DemandLine], ceilings: dict[str, Decimal]
) -> Decimal:
    """Section 3.4 - what the basket costs if every item sits at its ceiling."""
    total = ZERO
    for cas, line in demand.items():
        ceiling = ceilings.get(cas)
        if ceiling is not None:
            total += ceiling * line.required_qty_l
    return money(total)


# ---------------------------------------------------------------------------
# Section 7 - internal consistency of each quoted line
# ---------------------------------------------------------------------------

def check_line_total(line: LineInput, policy: Policy) -> tuple[Decimal | None, Decimal | None, str, str | None]:
    """unit price x quantity against the stated line total.

    Mismatches are reported, never silently corrected.
    """
    if line.quantity is None or line.unit_price is None:
        return None, None, "MISSING_FIELD", "quantity or unit price missing"
    recomputed = money(line.unit_price * line.quantity)
    if line.line_total_stated is None:
        return recomputed, None, "MISSING_FIELD", "no line total stated on the quote"

    delta = money(recomputed - line.line_total_stated)
    if line.line_total_stated == 0:
        return recomputed, delta, "LINE_TOTAL_MISMATCH", "stated line total is zero"

    variance = abs(delta / line.line_total_stated * HUNDRED)
    if variance > policy.line_total_tolerance_pct:
        note = (
            f"stated {line.line_total_stated} but unit price x quantity gives "
            f"{recomputed} (difference {delta})"
        )
        return recomputed, delta, "LINE_TOTAL_MISMATCH", note
    return recomputed, delta, "OK", None
