"""Mutations to policy_config, benchmark, demand and compliance_requirement.

Pulled out of api/routes.py so the HTTP endpoints and the agent's change
tools call exactly one implementation each. Two copies of this validation
logic would drift the moment one of them got a fix the other didn't - the
agent must never be able to write something the UI would have rejected, or
the reverse.

Two shapes of input arrive here:

* The Policy in force screen sends structured rows ({"key", "value"} and
  {"code", "label", "tier"}) - apply_policy_updates, apply_compliance_updates,
  remove_compliance.
* The agent and the chat's "Apply this change" button send a ChangeSet in
  "KEY=VALUE" form - parse_changes, then apply_change_set. The same parsed
  ChangeSet is what evaluation.simulate() overrides with, so a simulated
  change and an applied one can never be read two different ways.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import (
    Benchmark, ComplianceRequirement, DeletedComplianceCode, Demand, Material,
    PolicyConfig,
)

VALID_TIERS = {"MANDATORY", "ADVISORY"}
REMOVED = "REMOVED"

# (min, max, integer_only) - see routes.py's update_policy for why this
# exists: a guardrail on the input, not a business rule.
POLICY_LIMITS: dict[str, tuple[Decimal, Decimal, bool]] = {
    "fx_usd_eur": (Decimal("0.01"), Decimal("100"), False),
    "gallon_to_litre": (Decimal("1"), Decimal("10"), False),
    "price_rounding_dp": (Decimal("0"), Decimal("6"), True),
    "line_total_tolerance_pct": (Decimal("0"), Decimal("100"), False),
    "moq_overbuy_threshold_pct": (Decimal("0"), Decimal("1000"), False),
    "ceiling_materiality_pct": (Decimal("0"), Decimal("100"), False),
    "promotion_band_pct": (Decimal("0"), Decimal("100"), False),
    "primary_allocation_pct": (Decimal("0"), Decimal("100"), False),
    "max_vendor_share_pct": (Decimal("0"), Decimal("100"), False),
    "min_supplier_count": (Decimal("1"), Decimal("20"), True),
    "extraction_confidence_threshold": (Decimal("0"), Decimal("1"), False),
}

# Stored and editable, but not read by engine.evaluate(). Changing one is
# legitimate; saying it moved the ranking would not be.
NOT_USED_BY_ENGINE = {
    "min_supplier_count": "recorded policy only; the evaluation engine does not read it",
    "extraction_confidence_threshold": (
        "applies when a document is extracted, not when quotes are evaluated"),
}

# Sanity bounds for per-material values, same spirit as POLICY_LIMITS.
CEILING_LIMITS = (Decimal("0"), Decimal("10000"))       # EUR/L, exclusive of 0
VOLUME_LIMITS = (Decimal("0"), Decimal("1000000000"))   # litres, exclusive of 0

TIER_ALIASES = {
    "MANDATORY": "MANDATORY", "REQUIRED": "MANDATORY",
    "ADVISORY": "ADVISORY", "OPTIONAL": "ADVISORY",
    "REMOVED": REMOVED, "REMOVE": REMOVED, "DELETE": REMOVED, "DELETED": REMOVED,
    "NONE": REMOVED,
}


def compliance_code(raw: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", raw.strip().upper()).strip("_")


def policy_value_error(key: str, value: Decimal, unit: str | None) -> str | None:
    """Why this value cannot be stored for this key, or None when it can."""
    limits = POLICY_LIMITS.get(key)
    if not limits:
        return None
    lo, hi, integer_only = limits
    if not (lo <= value <= hi):
        return f"must be between {lo} and {hi} ({unit or 'unitless'})"
    if integer_only and value != value.to_integral_value():
        return "must be a whole number"
    return None


def apply_policy_updates(session: Session, updates: list[dict]) -> dict:
    """updates: [{"key": ..., "value": ...}, ...]. See routes.update_policy."""
    updated: list[dict] = []
    errors: list[dict] = []

    for item in updates:
        key = item["key"]
        try:
            value = Decimal(str(item["value"]))
        except (InvalidOperation, ValueError):
            errors.append({"key": key, "error": "not a number"})
            continue
        row = session.get(PolicyConfig, key)
        if not row:
            errors.append({"key": key, "error": "unknown policy key"})
            continue

        problem = policy_value_error(key, value, row.unit)
        if problem:
            errors.append({"key": key, "error": problem})
            continue

        row.value = value
        updated.append({"key": key, "value": str(value), "unit": row.unit})

    if updated:
        session.commit()
    return {"updated": updated, "errors": errors}


def apply_compliance_updates(session: Session, updates: list[dict]) -> dict:
    """updates: [{"code", "label"?, "tier"?, "match_hint"?}, ...]. See
    routes.update_compliance."""
    updated: list[dict] = []
    created: list[dict] = []
    errors: list[dict] = []

    for item in updates:
        code = compliance_code(item["code"])
        if not code:
            errors.append({"code": item.get("code", ""), "error": "empty or invalid code"})
            continue

        tier = item.get("tier", "").strip().upper() if item.get("tier") else None
        if tier and tier not in VALID_TIERS:
            errors.append({"code": code, "error": "tier must be MANDATORY or ADVISORY"})
            continue

        row = session.get(ComplianceRequirement, code)
        if not row and (not item.get("label") or not tier):
            errors.append({"code": code, "error": "a new code needs both a label and a tier"})
            continue

        tombstone = session.get(DeletedComplianceCode, code)
        if tombstone:
            session.delete(tombstone)

        if row:
            if item.get("label"):
                row.label = item["label"].strip()
            if tier:
                row.tier = tier
            if item.get("match_hint") is not None:
                row.match_hint = item["match_hint"].strip() or None
            row.manual_override = True
            updated.append({"code": code, "label": row.label, "tier": row.tier})
        else:
            session.add(ComplianceRequirement(
                code=code, label=item["label"].strip(), tier=tier,
                match_hint=(item.get("match_hint") or "").strip() or None,
                manual_override=True,
            ))
            created.append({"code": code, "label": item["label"].strip(), "tier": tier})

    if updated or created:
        session.commit()
    return {"updated": updated, "created": created, "errors": errors}


def remove_compliance(session: Session, code: str) -> dict:
    """See routes.delete_compliance."""
    normalized = compliance_code(code)
    row = session.get(ComplianceRequirement, normalized)
    if not row:
        return {"error": f"unknown compliance code: {normalized}"}

    session.delete(row)
    if not session.get(DeletedComplianceCode, normalized):
        session.add(DeletedComplianceCode(code=normalized))
    session.commit()
    return {"deleted": normalized}


# ---------------------------------------------------------------------------
# Change sets - "KEY=VALUE" changes from the agent and the chat
# ---------------------------------------------------------------------------

@dataclass
class ChangeSet:
    """A validated set of changes, resolved to absolute values.

    policy: policy_config key -> value. ceilings / volumes: CAS -> EUR/L or
    litres. compliance: code -> MANDATORY, ADVISORY or REMOVED. `details`
    carries the previous and new value of each, so whoever reports the change
    quotes both from here rather than working either one out.
    """
    policy: dict[str, Decimal] = field(default_factory=dict)
    ceilings: dict[str, Decimal] = field(default_factory=dict)
    volumes: dict[str, Decimal] = field(default_factory=dict)
    compliance: dict[str, str] = field(default_factory=dict)
    details: list[dict] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.policy or self.ceilings or self.volumes or self.compliance)

    def as_arguments(self) -> dict[str, list[str]]:
        """The same changes in absolute KEY=VALUE form.

        Relative entries ("+2", "-10%") are resolved against the value in
        force when they were parsed. Replaying the resolved form - which is
        what the chat's Apply button does after a simulation - applies exactly
        what was simulated, even if the value moved in between.
        """
        return {
            "policy_changes": [f"{k}={_plain(v)}" for k, v in self.policy.items()],
            "ceiling_price_changes": [f"{k}={_plain(v)}" for k, v in self.ceilings.items()],
            "volume_changes": [f"{k}={_plain(v)}" for k, v in self.volumes.items()],
            "compliance_changes": [f"{k}={v}" for k, v in self.compliance.items()],
        }


def _plain(value: Decimal) -> str:
    return f"{value.normalize():f}"


_NUMBER = re.compile(
    r"(?P<sign>[+-])?\s*(?P<digits>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d*\.?\d+)\s*(?P<pct>%)?")
_UNIT_SUFFIX = re.compile(
    r"\s*(?:eur\s*/\s*l(?:itre|iter)?|eur|€|litres?|liters?|l)\s*$", re.IGNORECASE)


def _entries(value) -> list:
    """The list of KEY=VALUE entries, however the caller shaped it.

    The tool schema asks for a list of strings, but a model will sometimes
    send one string or a {key: value} object instead. Iterating a string
    would validate it one character at a time, so both are normalized here.
    """
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [f"{key}={val}" for key, val in value.items()]
    return list(value)


def _split(entry, errors: list[str]) -> tuple[str, str] | None:
    text = str(entry).strip()
    key, sep, value = text.partition("=")
    if not sep or not key.strip() or not value.strip():
        errors.append(f"{text!r}: expected KEY=VALUE")
        return None
    return key.strip(), value.strip()


def _number(raw: str, current: Decimal | None, label: str,
            errors: list[str], scale: int) -> Decimal | None:
    """An absolute value, or a relative one resolved against `current`.

    "10" and "10%" are absolute (every value here is non-negative, so a bare
    number is never ambiguous). A leading sign makes it relative: "+2" adds 2
    in the value's own unit, "-10%" takes off ten percent of the current
    value. Resolving it here is what keeps arithmetic out of the model.

    Rounded to the column's scale, so a simulation runs on exactly the value
    the database would store - otherwise Postgres rounds the applied value
    and it no longer matches what was simulated.
    """
    text = _UNIT_SUFFIX.sub("", raw.strip())
    match = _NUMBER.fullmatch(text)
    if not match:
        errors.append(f"{label}: {raw!r} is not a number (use a dot for decimals)")
        return None
    amount = Decimal(match["digits"].replace(",", ""))
    if match["sign"]:
        if current is None:
            errors.append(f"{label}: there is no current value to change relative to")
            return None
        delta = current * amount / Decimal("100") if match["pct"] else amount
        amount = current + delta if match["sign"] == "+" else current - delta
    return amount.quantize(Decimal(1).scaleb(-scale))


def _material_key(session: Session, raw: str) -> str | None:
    """A CAS number as given, or resolved from the material's catalogue name."""
    key = raw.strip()
    if session.get(Material, key):
        return key
    wanted = key.lower()
    for material in session.scalars(select(Material)):
        if material.name.lower() == wanted:
            return material.cas_no
    return None


def parse_changes(
    session: Session,
    policy_changes: list[str] | None = None,
    ceiling_price_changes: list[str] | None = None,
    volume_changes: list[str] | None = None,
    compliance_changes: list[str] | None = None,
) -> tuple[ChangeSet, list[str]]:
    """Validate every entry against what is in force now.

    Returns the ChangeSet and a list of errors. Callers apply nothing unless
    the error list is empty: half of a requested change is a policy nobody
    asked for.
    """
    changes = ChangeSet()
    errors: list[str] = []

    for entry in _entries(policy_changes):
        parts = _split(entry, errors)
        if not parts:
            continue
        key = parts[0].lower()
        row = session.get(PolicyConfig, key)
        if not row:
            known = ", ".join(sorted(r.key for r in session.scalars(select(PolicyConfig))))
            errors.append(f"unknown policy key {key!r}; valid keys: {known}")
            continue
        current = Decimal(str(row.value))
        value = _number(parts[1], current, key, errors, scale=6)   # Numeric(16, 6)
        if value is None:
            continue
        problem = policy_value_error(key, value, row.unit)
        if problem:
            errors.append(f"{key}: {_plain(value)} {problem}")
            continue
        changes.policy[key] = value
        changes.details.append({
            "kind": "policy threshold", "key": key, "unit": row.unit,
            "previous": _plain(current), "new": _plain(value),
            "affects_evaluation": key not in NOT_USED_BY_ENGINE,
            **({"note": NOT_USED_BY_ENGINE[key]} if key in NOT_USED_BY_ENGINE else {}),
        })

    for entry in _entries(ceiling_price_changes):
        parts = _split(entry, errors)
        if not parts:
            continue
        cas = _material_key(session, parts[0])
        if not cas:
            errors.append(f"unknown material {parts[0]!r}; use a CAS number from "
                          "get_current_materials")
            continue
        row = session.get(Benchmark, cas)
        current = Decimal(str(row.ceiling_price_eur_l)) if row else None
        value = _number(parts[1], current, f"ceiling price for {cas}", errors,
                        scale=6)   # Numeric(14, 6)
        if value is None:
            continue
        lo, hi = CEILING_LIMITS
        if not (lo < value <= hi):
            errors.append(f"ceiling price for {cas}: {_plain(value)} must be above "
                          f"{lo} and at most {hi} EUR/L")
            continue
        changes.ceilings[cas] = value
        changes.details.append({
            "kind": "ceiling price", "key": cas,
            "material": session.get(Material, cas).name, "unit": "EUR/L",
            "previous": _plain(current) if current is not None else None,
            "new": _plain(value), "affects_evaluation": True,
        })

    for entry in _entries(volume_changes):
        parts = _split(entry, errors)
        if not parts:
            continue
        cas = _material_key(session, parts[0])
        row = session.get(Demand, cas) if cas else None
        if not row:
            errors.append(f"{parts[0]!r} is not in the demand basket; only the "
                          "required volume of a basket material can change")
            continue
        current = Decimal(str(row.required_qty_l))
        value = _number(parts[1], current, f"required volume for {cas}", errors,
                        scale=4)   # Numeric(16, 4)
        if value is None:
            continue
        lo, hi = VOLUME_LIMITS
        if not (lo < value <= hi):
            errors.append(f"required volume for {cas}: {_plain(value)} must be above "
                          f"{lo} and at most {hi} L")
            continue
        changes.volumes[cas] = value
        changes.details.append({
            "kind": "required volume", "key": cas,
            "material": session.get(Material, cas).name, "unit": "L",
            "previous": _plain(current), "new": _plain(value),
            "affects_evaluation": True,
        })

    for entry in _entries(compliance_changes):
        parts = _split(entry, errors)
        if not parts:
            continue
        code = compliance_code(parts[0])
        row = session.get(ComplianceRequirement, code)
        if not row:
            known = ", ".join(sorted(
                r.code for r in session.scalars(select(ComplianceRequirement))))
            errors.append(
                f"{code!r} is not on the compliance checklist (current codes: {known}). "
                "Adding a new requirement needs a label and is a separate action.")
            continue
        tier = TIER_ALIASES.get(parts[1].strip().upper())
        if not tier:
            errors.append(f"{code}: tier must be MANDATORY, ADVISORY or REMOVED, "
                          f"not {parts[1]!r}")
            continue
        changes.compliance[code] = tier
        changes.details.append({
            "kind": "compliance requirement", "key": code, "label": row.label,
            "previous": row.tier, "new": tier, "affects_evaluation": True,
        })

    return changes, errors


def apply_change_set(session: Session, changes: ChangeSet) -> list[dict]:
    """Write a validated ChangeSet in one commit. Returns its details."""
    for key, value in changes.policy.items():
        session.get(PolicyConfig, key).value = value

    for cas, value in changes.ceilings.items():
        row = session.get(Benchmark, cas)
        if row:
            row.ceiling_price_eur_l = value
        else:
            session.add(Benchmark(cas_no=cas, ceiling_price_eur_l=value))

    for cas, value in changes.volumes.items():
        session.get(Demand, cas).required_qty_l = value

    for code, tier in changes.compliance.items():
        row = session.get(ComplianceRequirement, code)
        if tier == REMOVED:
            session.delete(row)
            if not session.get(DeletedComplianceCode, code):
                session.add(DeletedComplianceCode(code=code))
        else:
            row.tier = tier
            # Same protection a manual edit on the Policy in force screen
            # gets: seed.py stops reconciling this code back to its default.
            row.manual_override = True

    session.commit()
    return changes.details
