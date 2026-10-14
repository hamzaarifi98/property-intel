"""Tax check: does the Treasurer's bill follow from the values and levies?

Colorado taxes school levies on a separate assessed value (the "school value"),
so one "taxable value x total levy" cannot match. The check uses:

1. tax_levies: the levy per taxing authority, from the Assessor's Levy.aspx
   page (the mill_levy link); school levies are the authorities named SCHOOL.
2. The Treasurer's taxable value and taxable school value for the tax year.
3. The tax statement PDF, which lists the tax per authority. When the page's
   values do not give the bill, the statement shows which values the bill used.

tax_check is ok when the bill is reproduced, check_failed with both numbers when
it is not. The value year is compared with the tax year either way.
"""
from __future__ import annotations

import io
import re
from typing import Callable, Optional

import httpx
from bs4 import BeautifulSoup

from models import Observation

from .settings import HEADERS, PAGE_SOURCE, TREASURER_SOURCE, TREASURER_URL, SCHOOL_AUTHORITY

STATEMENT_LINE = re.compile(r"^\*?\s*([A-Z][A-Z0-9 .&'/()#,-]+?)\s+(0\.\d{6})\s+([\d,]+\.\d{2})\s*$")


def levies(url: str, client: httpx.Client) -> tuple[Optional[str], list[dict]]:
    """(levy year, [{code, authority, mills, school}]) from Levy.aspx."""
    r = client.get(url, headers=HEADERS, follow_redirects=True)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    year = re.search(r"\b(\d{4}) Tax Levies", soup.get_text(" "))
    rows, seen = [], set()
    for tr in soup.find_all("tr"):
        tds = [td.get_text(" ", strip=True) for td in tr.find_all("td", recursive=False)]
        if len(tds) == 3 and re.fullmatch(r"\d{3,5}", tds[0]) and tds[0] not in seen:
            seen.add(tds[0])           # the page repeats the table in nested layouts
            rows.append({"code": tds[0], "authority": tds[1], "mills": float(tds[2]),
                         "school": bool(SCHOOL_AUTHORITY.search(tds[1]))})
    return (year[1] if year else None), rows


def statement_lines(url: str, client: httpx.Client) -> list[dict]:
    """[{authority, rate, amount}] from the tax statement PDF; special assessments
    (no rate) are not included."""
    from pypdf import PdfReader
    r = client.get(url, headers=HEADERS, follow_redirects=True)
    r.raise_for_status()
    text = "\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(r.content)).pages)
    out = []
    for line in text.splitlines():
        m = STATEMENT_LINE.match(line.strip())
        if m:
            out.append({"authority": m[1].strip(), "rate": float(m[2]),
                        "amount": float(m[3].replace(",", "")),
                        "school": bool(SCHOOL_AUTHORITY.search(m[1]))})
    return out


def tax_check(obs: list[Observation], client: httpx.Client) -> list[Observation]:
    def found(field: str) -> Optional[Observation]:
        return next((o for o in obs if o.field == field and o.status == "ok"), None)

    def val(field: str):
        o = found(field)
        return o.value if o else None

    levy_obs = found("mill_levy")
    billed, tax_year = val("tax_billed"), val("tax_year")
    if not levy_obs or not levy_obs.record_url or billed is None:
        return [Observation(field="tax_check", status="not_provided", source=TREASURER_SOURCE,
                            url=levy_obs.url if levy_obs else TREASURER_URL,
                            note="no levy breakdown link or no billed tax to check")]
    out: list[Observation] = []
    try:
        levy_year, rows = levies(levy_obs.record_url, client)
    except httpx.HTTPError as e:
        return [Observation(field=f, status="source_unavailable", source=PAGE_SOURCE,
                            url=levy_obs.record_url, note=f"levy page: {type(e).__name__}: {e}")
                for f in ("tax_levies", "tax_check")]
    out.append(Observation(field="tax_levies", value=rows, as_of=levy_year, source=PAGE_SOURCE,
                           url=levy_obs.record_url, record_url=levy_obs.record_url,
                           note="levy per taxing authority, in mills; school: taxed on the "
                                "school assessed value"))

    school_mills = sum(r["mills"] for r in rows if r["school"])
    other_mills = sum(r["mills"] for r in rows if not r["school"])
    taxable, school_value = val("tax_taxable_value"), val("tax_taxable_school_value")
    flags, notes = [], []
    if not school_value:
        # non-residential property has one assessed value; the page then shows $0
        school_value = taxable
        notes.append("the Treasurer page gives no separate school value (0 or blank, as for "
                     "non-residential property); the school levy is applied to the taxable "
                     "value")
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
    pdf = found("tax_bill_pdf_url")
    if not reconciled and pdf:
        try:
            lines = statement_lines(pdf.value, client)
        except Exception as e:                       # a PDF that can't be read: say so
            notes.append(f"tax statement could not be read: {type(e).__name__}: {e}")
            lines = []
        if lines:
            groups = {}
            for school in (True, False):
                part = [ln for ln in lines if ln["school"] == school]
                rate = sum(ln["rate"] for ln in part)
                amount = round(sum(ln["amount"] for ln in part), 2)
                groups["school" if school else "other"] = {
                    "rate": round(rate, 6), "tax": amount,
                    "implied_value": round(amount / rate) if rate else None}
            statement_total = round(sum(ln["amount"] for ln in lines), 2)
            value["statement"] = {"lines": lines, "groups": groups, "total": statement_total}
            if abs(statement_total - billed) <= tolerance:
                reconciled = True
                value["reconciled_with"] = "the statement's own values"
                billed_values = {"taxable": groups["other"]["implied_value"],
                                 "taxable_school": groups["school"]["implied_value"]}
                value["billed_values"] = billed_values
                for k, v in billed_values.items():
                    shown = value["page_values"][k]
                    if v is not None and shown is not None and abs(v - shown) > 1:
                        flags.append(f"the bill used a {k.replace('_', ' ')} value of {v:,}, "
                                     f"the Treasurer page shows {shown:,.0f}"
                                     + year_hint(shown, found))
    elif reconciled:
        value["reconciled_with"] = "the Treasurer page's values"

    # the rest of the bill: special assessments, fees and interest, against payments
    extras = sum(val(f) or 0 for f in ("tax_special_assessments", "tax_fees", "tax_interest"))
    charged = round(billed - (val("tax_exemptions") or 0) + extras, 2)
    paid, due = val("tax_paid"), val("tax_due")
    if paid is not None and due is not None:
        value["charged"] = charged
        value["paid_plus_due"] = round(paid + due, 2)
        if abs(charged - value["paid_plus_due"]) > 0.01:
            flags.append(f"billed tax plus special assessments, fees and interest ({charged:,}) "
                         f"is not paid plus due ({value['paid_plus_due']:,})")
    value["flags"] = flags
    status = "ok" if reconciled else "check_failed"
    head = (f"the {tax_year} bill ({billed:,}) is reproduced from "
            f"{value.get('reconciled_with')}" if reconciled else
            f"CHECK FAILED: the {tax_year} bill is {billed:,}, but the page's values and levies "
            + (f"give {computed:,}; the two do not agree" if computed is not None
               else "cannot be checked: the page has no taxable value"))
    note = "; ".join([head + " (school levies on the school value, the rest on the taxable "
                      "value)"] + flags + notes)
    out.append(Observation(field="tax_check", status=status, value=value, note=note,
                           source=TREASURER_SOURCE, url=pdf.value if pdf else levy_obs.record_url,
                           record_url=pdf.value if pdf else None, as_of=str(tax_year)))
    return out


def year_hint(shown: float, found: Callable[[str], Optional[Observation]]) -> str:
    """' (= the Assessor's 2026 assessed value)' when the shown value is another year's."""
    b, ld = found("assessed_building"), found("assessed_land")
    if b and ld and abs((b.value or 0) + (ld.value or 0) - shown) <= 1:
        return f" (= the Assessor's {b.as_of} assessed value: the page pairs another year's value)"
    s = found("assessed_school_total")
    if s and abs((s.value or 0) - shown) <= 1:
        return f" (= the Assessor's {s.as_of} school value)"
    return ""
