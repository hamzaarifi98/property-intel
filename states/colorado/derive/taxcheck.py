"""Tax check: does the Treasurer's bill follow from the values and levies?

Colorado taxes school levies on a separate assessed value (the "school value"),
so one "taxable value x total levy" cannot match. The check uses:

1. tax_levies: the levy per taxing authority (from the treasurer platform's
   `levies` hook; the Assessor's mill_levy observation links to the page).
   School levies are the authorities named SCHOOL.
2. The Treasurer's taxable value and taxable school value for the tax year.
3. The tax statement (the platform's `statement_lines` hook): tax per authority.
   When the page's values do not give the bill, it shows which values the bill used.

tax_check is ok when the bill is reproduced, check_failed with both numbers when
it is not. A platform without the hooks gets no check (not_provided).
"""
from __future__ import annotations

import re
from typing import Callable, Optional

import httpx

from core.context import Context
from core.models import Observation

SCHOOL_AUTHORITY = re.compile(r"\bSCHOOL\b|\bSCH\b", re.I)
LeviesHook = Callable[[str, httpx.Client], tuple[Optional[str], list[dict]]]
StatementHook = Callable[[str, httpx.Client], list[dict]]


def run(ctx: Context, levies: Optional[LeviesHook], statement: Optional[StatementHook],
        treasurer_source: str, treasurer_url: str) -> None:
    levy_obs, billed, tax_year = ctx.found("mill_levy"), ctx.value("tax_billed"), ctx.value("tax_year")
    if levies is None or not levy_obs or not levy_obs.record_url or billed is None:
        why = ("the county's treasurer platform has no levy breakdown" if levies is None else
               "no levy breakdown link or no billed tax to check")
        ctx.add([Observation(field="tax_check", status="not_provided", source=treasurer_source,
                             url=treasurer_url, note=why)])
        return
    try:
        levy_year, rows = levies(levy_obs.record_url, ctx.client)
    except httpx.HTTPError as e:
        ctx.add([Observation(field=f, status="source_unavailable", source=levy_obs.source,
                             url=levy_obs.record_url, note=f"levy page: {type(e).__name__}: {e}")
                 for f in ("tax_levies", "tax_check")])
        return
    for r in rows:
        r["school"] = bool(SCHOOL_AUTHORITY.search(r["authority"]))
    ctx.add([Observation(field="tax_levies", value=rows, as_of=levy_year, source=levy_obs.source,
                         url=levy_obs.record_url, record_url=levy_obs.record_url,
                         note="levy per taxing authority, in mills; school: taxed on the school "
                              "assessed value")])

    school_mills = sum(r["mills"] for r in rows if r["school"])
    other_mills = sum(r["mills"] for r in rows if not r["school"])
    taxable, school_value = ctx.value("tax_taxable_value"), ctx.value("tax_taxable_school_value")
    flags, notes = [], []
    if not school_value:
        # non-residential property has one assessed value; the page then shows $0
        school_value = taxable
        notes.append("the Treasurer page gives no separate school value (0 or blank, as for "
                     "non-residential property); the school levy is applied to the taxable value")
    if levy_year and tax_year and levy_year != str(tax_year):
        flags.append(f"the levies are for {levy_year}, the bill for tax year {tax_year}")
    computed = round(school_value * school_mills / 1000 + taxable * other_mills / 1000, 2) \
        if taxable is not None else None
    tolerance = round(0.01 * len(rows) + 0.01, 2)        # per-authority rounding
    value = {
        "tax_year": tax_year, "levy_year": levy_year,
        "school_mills": round(school_mills, 6), "other_mills": round(other_mills, 6),
        "page_values": {"taxable": taxable, "taxable_school": school_value},
        "computed_from_page_values": computed, "billed": billed,
        "difference": round(billed - computed, 2) if computed is not None else None,
    }
    reconciled = computed is not None and abs(billed - computed) <= tolerance

    # the bill's own values, from the statement's tax per authority
    pdf = ctx.found("tax_bill_pdf_url")
    if not reconciled and pdf and statement is not None:
        try:
            lines = statement(pdf.value, ctx.client)
        except Exception as e:                       # a PDF that can't be read: say so
            notes.append(f"tax statement could not be read: {type(e).__name__}: {e}")
            lines = []
        if lines:
            for ln in lines:
                ln["school"] = bool(SCHOOL_AUTHORITY.search(ln["authority"]))
            groups = {}
            for school in (True, False):
                part = [ln for ln in lines if ln["school"] == school]
                rate = sum(ln["rate"] for ln in part)
                amount = round(sum(ln["amount"] for ln in part), 2)
                groups["school" if school else "other"] = {
                    "rate": round(rate, 6), "tax": amount,
                    "implied_value": round(amount / rate) if rate else None}
            total = round(sum(ln["amount"] for ln in lines), 2)
            value["statement"] = {"lines": lines, "groups": groups, "total": total}
            if abs(total - billed) <= tolerance:
                reconciled = True
                value["reconciled_with"] = "the statement's own values"
                billed_values = {"taxable": groups["other"]["implied_value"],
                                 "taxable_school": groups["school"]["implied_value"]
                                 or groups["other"]["implied_value"]}
                value["billed_values"] = billed_values
                for k, v in billed_values.items():
                    shown = value["page_values"][k]
                    if v is not None and shown is not None and abs(v - shown) > 1:
                        flags.append(f"the bill used a {k.replace('_', ' ')} value of {v:,}, "
                                     f"the Treasurer page shows {shown:,.0f}" + year_hint(shown, ctx))
    elif reconciled:
        value["reconciled_with"] = "the Treasurer page's values"

    # the rest of the bill: special assessments, fees and interest, against payments
    extras = sum(ctx.value(f) or 0 for f in ("tax_special_assessments", "tax_fees", "tax_interest"))
    charged = round(billed - (ctx.value("tax_exemptions") or 0) + extras, 2)
    paid, due = ctx.value("tax_paid"), ctx.value("tax_due")
    if paid is not None and due is not None:
        value["charged"], value["paid_plus_due"] = charged, round(paid + due, 2)
        if abs(charged - value["paid_plus_due"]) > 0.01:
            flags.append(f"billed tax plus special assessments, fees and interest ({charged:,}) "
                         f"is not paid plus due ({value['paid_plus_due']:,})")
    value["flags"] = flags
    head = (f"the {tax_year} bill ({billed:,}) is reproduced from {value.get('reconciled_with')}"
            if reconciled else
            f"CHECK FAILED: the {tax_year} bill is {billed:,}, but the page's values and levies "
            + (f"give {computed:,}; the two do not agree" if computed is not None
               else "cannot be checked: the page has no taxable value"))
    note = "; ".join([head + " (school levies on the school value, the rest on the taxable "
                      "value)"] + flags + notes)
    ctx.add([Observation(field="tax_check", status="ok" if reconciled else "check_failed",
                         value=value, note=note, source=treasurer_source,
                         url=pdf.value if pdf else levy_obs.record_url,
                         record_url=pdf.value if pdf else None, as_of=str(tax_year))])


def year_hint(shown: float, ctx: Context) -> str:
    """' (= the Assessor's 2026 assessed value)' when the shown value is another year's."""
    b, ld = ctx.found("assessed_building"), ctx.found("assessed_land")
    if b and ld and abs((b.value or 0) + (ld.value or 0) - shown) <= 1:
        return f" (= the Assessor's {b.as_of} assessed value: the page pairs another year's value)"
    s = ctx.found("assessed_school_total")
    if s and abs((s.value or 0) - shown) <= 1:
        return f" (= the Assessor's {s.as_of} school value)"
    return ""
