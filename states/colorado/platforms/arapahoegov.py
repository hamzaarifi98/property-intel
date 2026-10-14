"""Arapahoe County's own Assessor and Treasurer sites (*.arapahoegov.com).

In-house ASP.NET pages with no API, read by the ids of their labels. The ids
belong to the software and live here; URLs and source names come from the
county config (`assessor`, `treasurer`).

    assessor    the parcel page: legal, values, sales, buildings, areas, land lines,
                mill levy (linked to its per-authority breakdown)
    treasurer   the current tax year and the prior year
    levies      the per-authority levy page (tax check hook)
    statement_lines   the tax statement PDF, tax per authority (tax check hook)
"""
from __future__ import annotations

import io
import re
from datetime import datetime
from typing import Optional

import httpx
from bs4 import BeautifulSoup, Tag

from core.context import Context
from core.platforms.arcgis import fields_status
from core.platforms.web import HEADERS, adder, fetch_text, label, snake, to_number, year_in
from core.models import Observation
from core.common import SourceUnavailable, clean, parse_money

# ------------------------------------------------------------ assessor page
NOT_FOUND_TEXT = "No matching records were found"
FOUND_MARKER = "ucParcelHeader_lblAinTxt"
HEADER_FIELDS = {"ownership_type": "lblOwnershipTypeTxt", "land_acres": "lblAcreageTxt",
                 "legal_description": "lblLegalDescTxt"}
NUMBER_FIELDS = {"land_acres"}
VALUE_GROUPS = [   # (title label, {our field: value label}), ids prefixed ucParcelValue_
    ("lblAssessValueTitle", {"assessed_building": "lblAssdBuilding",
                             "assessed_land": "lblAssdLand"}),
    ("lblSchoolValueTitle", {"assessed_school_total": "lblSchoolTotal",
                             "assessed_school_building": "lblSchoolBuilding",
                             "assessed_school_land": "lblSchoolLand"}),
]
DETAIL_FIELDS = ["ownership_type", "land_acres", "legal_description", "value_tax_year",
                 "assessed_building", "assessed_land", "assessed_school_total",
                 "assessed_school_building", "assessed_school_land", "mill_levy",
                 "sales", "buildings", "building_areas", "land_lines", "land_sqft"]


def is_hidden(row: Tag) -> bool:
    return bool(re.search(r"display\s*:\s*none", row.get("style", "")))


def parse_sales(soup: BeautifulSoup) -> list[dict]:
    """Sale rows: book/page (links to the Clerk's deed search), date, price, type."""
    title = soup.find(id="ucParcelSale_rptrSale_lblSaleTitle")
    if not title:
        return []
    sales = []
    for row in title.find_parent("table").find_all("tr")[1:]:
        tds = row.find_all("td", recursive=False)
        if len(tds) < 5:
            continue
        link, flag = tds[1].find("a"), tds[1].find("span")
        raw_date = tds[2].get_text(strip=True)
        try:
            date = datetime.strptime(raw_date, "%m-%d-%Y").date().isoformat()
        except ValueError:
            date = clean(raw_date)
        sales.append({
            "book_page": clean(link.get_text(" ", strip=True).replace("\xa0", " ")) if link else None,
            "deed_search_url": link.get("href") if link else None,
            "date": date,
            "price": parse_money(tds[3].get_text(strip=True)),
            "type": clean(tds[4].get_text(" ", strip=True)),
            "flag": clean(flag.get_text(" ", strip=True)) if flag else None,
        })
    sales.sort(key=lambda s: s["date"] or "", reverse=True)
    return sales


def parse_buildings(soup: BeautifulSoup) -> list[dict]:
    """One dict per building. Rows the page hides (display:none) are skipped."""
    buildings: dict[int, dict] = {}
    for span in soup.find_all("span", id=re.compile(r"^ucParcelResdBuild_rptrResdBuild_\w+_\d+$")):
        idx = int(span["id"].rsplit("_", 1)[1])
        row = span.find_parent("tr")
        b = buildings.setdefault(idx, {"building": str(idx + 1)})
        tds = row.find_all("td", recursive=False)
        if len(tds) >= 2 and clean(tds[1].get_text(strip=True)):
            b["building"] = tds[1].get_text(strip=True)
        if is_hidden(row):
            continue
        value = clean(tds[-1].get_text(" ", strip=True)) if tds else None
        if value is not None:
            b[snake(span.get_text())] = to_number(value)
    return [buildings[i] for i in sorted(buildings)]


def parse_areas(soup: BeautifulSoup) -> list[dict]:
    """Area lines per building (first floor, basement, ...). Totals rows are skipped."""
    table = soup.find(id="tblAreasResidential")
    if not table:
        return []
    areas, building = [], None
    for row in table.find_all("tr"):
        tds = row.find_all("td", recursive=False)
        if len(tds) != 4 or "td_area" in (tds[0].get("class") or []):
            continue
        building = clean(tds[1].get_text(strip=True)) or building
        areas.append({"building": building,
                      "description": clean(tds[2].get_text(" ", strip=True)),
                      "sqft": to_number(clean(tds[3].get_text(strip=True)))})
    return areas


def parse_land_lines(soup: BeautifulSoup) -> list[dict]:
    th = next((t for t in soup.find_all("th") if t.get_text(strip=True) == "Land Line"), None)
    if not th:
        return []
    lines = []
    for row in th.find_parent("table").find_all("tr"):
        tds = row.find_all("td", recursive=False)
        if len(tds) != 4:
            continue
        m = re.fullmatch(r"([\d,.]+)\s*(\w+)", tds[1].get_text(strip=True))
        lines.append({"units": to_number(m.group(1)) if m else None,
                      "unit_type": m.group(2) if m else None,
                      "land_use": clean(tds[3].get_text(" ", strip=True))})
    return lines


def page_observations(html: str, snapshot_id: Optional[str], request_url: str,
                      source: str) -> list[Observation]:
    soup = BeautifulSoup(html, "html.parser")
    obs: list[Observation] = []
    add = adder(obs, dict(source=source, url=request_url, snapshot_id=snapshot_id,
                          record_url=request_url), "blank on the Assessor's page")

    for ours, id_ in HEADER_FIELDS.items():
        value = label(soup, f"ucParcelHeader_{id_}")
        add(ours, to_number(value) if ours in NUMBER_FIELDS else value)
    appeal = label(soup, "ucParcelHeader_lblAppealsLink")
    if appeal:
        add("appeal_notice", appeal)

    value_title = label(soup, "ucParcelValue_lblValueTitle")
    add("value_tax_year", year_in(value_title),
        f"tax year of the values on the page ({value_title})" if value_title else None)
    for title_id, fields in VALUE_GROUPS:
        title = label(soup, f"ucParcelValue_{title_id}")
        year = year_in(title)
        for ours, id_ in fields.items():
            raw = label(soup, f"ucParcelValue_{id_}")
            value = parse_money(raw)
            note = f"could not parse number from {raw!r}" if raw is not None and value is None \
                else title
            add(ours, value, note, as_of=year)

    levy_title = label(soup, "ucParcelValue_lblLevyTitle")
    add("mill_levy", to_number(label(soup, "ucParcelValue_lnkLevy")),
        levy_title.rstrip(":") if levy_title else None, as_of=year_in(levy_title))
    # the levy links to its per-authority breakdown (Levy.aspx?id=..&auth=..)
    link = soup.find(id="ucParcelValue_lnkLevy")
    m = re.search(r"window\.open\('([^']+)'", (link.get("onclick") or "") if link else "")
    if m:
        obs[-1].record_url = str(httpx.URL(request_url).join(m[1]))

    sales = parse_sales(soup)
    add("sales", sales, "most recent first; date: the Assessor's sale date; book_page is the "
        "Clerk & Recorder reception" if sales else "no sales listed on the Assessor's page")
    add("buildings", parse_buildings(soup), "rows the county page hides are left out")
    add("building_areas", parse_areas(soup))
    land_lines = parse_land_lines(soup)
    add("land_lines", land_lines,
        "initial appraisal; does not reflect appeal adjustments (per the Assessor's page)")
    sqft = [ln["units"] for ln in land_lines if ln["unit_type"] == "SF" and ln["units"] is not None]
    if land_lines and len(sqft) == len(land_lines):
        add("land_sqft", sum(sqft), "Assessor: sum of the land lines")
    return obs


def assessor(cfg: dict, ctx: Context) -> None:
    source, url = cfg["source"], cfg["url"]
    pid = ctx.ids.get("parcel_id")
    if not pid:
        ctx.notes.append("the parcel record has no parcel id; the Assessor's page was not fetched")
        ctx.add(fields_status(DETAIL_FIELDS, "not_provided", "no parcel id to look up", source, url))
        return
    page_url = str(httpx.URL(url, params={cfg["id_param"]: pid}))
    try:
        html, sid, req = fetch_text(url, {cfg["id_param"]: pid}, source, ctx.client)
    except SourceUnavailable as e:
        ctx.notes.append(f"{source} unavailable: {e}")
        ctx.add(fields_status(DETAIL_FIELDS, "source_unavailable",
                              f"parcel page could not be read: {e}", source, page_url))
        return
    if FOUND_MARKER in html:
        ctx.add(page_observations(html, sid, req, source))
    elif NOT_FOUND_TEXT in html:
        ctx.notes.append(f"the Assessor's page has no record for {pid}, although the parcel "
                         "layer does")
        ctx.add(fields_status(DETAIL_FIELDS, "not_found",
                              "the Assessor's page has no record for this parcel", source, req, sid))
    else:       # a 200 that is neither a parcel nor "no records": maintenance or a layout change
        ctx.notes.append("unexpected Assessor page (maintenance or layout change)")
        ctx.add(fields_status(DETAIL_FIELDS, "source_unavailable",
                              "unexpected page (maintenance or layout change)", source, req, sid))


# ----------------------------------------------------------------- treasurer
TREASURER_FOUND = "ContentPlaceHolder1_lblAIN"
TREASURER_NOT_FOUND = "No matching record was found"
TAX_DUE_NOTE = ("certified by the Treasurer, but not a Certificate of Taxes Due (the document "
                "used at closing)")
TAX_MONEY_FIELDS = {                                  # the full-year column of the tax detail
    "tax_taxable_value": "lblTaxableValue",
    "tax_taxable_school_value": "lblTaxableSchoolValue",   # school levies use this one
    "tax_billed": "lblOrigTaxAmt",                    # "Assessed Tax" row
    "tax_exemptions": "lblFullExAmt",
    "tax_special_assessments": "lblOrigSAAmt",
    "tax_fees": "lblOrigFeeAmt",
    "tax_interest": "lblFullInterest",
    "tax_paid": "lblPaidTotal",
    "tax_due": "lblTaxFull",
}
TAX_FLAGS = {"tax_prior_years_due": "lblPriorYear", "tax_bankruptcy": "lblBankruptcy",
             "tax_treasurers_assessment": "lblTreasAssess", "tax_lien": "lblTaxLien",
             "tax_treasurers_deed": "lblTreasDeed"}
PRIOR_MONEY_FIELDS = {"tax_prior_year_billed": "lblOrigTotalAmt",
                      "tax_prior_year_paid": "lblPaidTotal"}
TAX_FIELDS = ["tax_year", "tax_rate", *TAX_MONEY_FIELDS, *TAX_FLAGS, "tax_bill_pdf_url"]
PRIOR_TAX_FIELDS = ["tax_prior_year", "tax_prior_year_rate", *PRIOR_MONEY_FIELDS]


def treasurer(cfg: dict, ctx: Context) -> None:
    """Current tax year (cfg url), then the prior year (cfg prior_year_url)."""
    source, url = cfg["source"], cfg["url"]
    account = ctx.ids.get("account")
    if not account:
        ctx.add(fields_status(TAX_FIELDS + PRIOR_TAX_FIELDS, "not_provided",
                              "no account number to look up", source, url))
        return
    try:
        html, sid, req = fetch_text(url, {cfg["id_param"]: account}, source, ctx.client)
    except SourceUnavailable as e:
        ctx.add(fields_status(TAX_FIELDS + PRIOR_TAX_FIELDS, "source_unavailable",
                              f"tax page could not be read: {e}", source, url))
        return
    if TREASURER_FOUND not in html:
        status, note = (("not_found", "the Treasurer has no record for this account")
                        if TREASURER_NOT_FOUND in html else
                        ("source_unavailable", "unexpected tax page (maintenance or layout change)"))
        ctx.add(fields_status(TAX_FIELDS + PRIOR_TAX_FIELDS, status, note, source, req, sid))
        return
    soup = BeautifulSoup(html, "html.parser")

    def lab(id_: str) -> Optional[str]:
        return label(soup, f"ContentPlaceHolder1_{id_}")

    m = re.search(r"Last updated on (\d{2})/(\d{2})/(\d{4})", lab("lblUpdateDate") or "")
    updated = f"{m[3]}-{m[1]}-{m[2]}" if m else None
    obs: list[Observation] = []
    add = adder(obs, dict(source=source, url=req, snapshot_id=sid, record_url=req),
                "blank on the Treasurer's page")
    years = re.search(r"(\d{4}) Payable (\d{4})", lab("lblPayable") or "")
    add("tax_year", years[1] if years else None,
        f"taxes for {years[1]} payable {years[2]}" if years else None, as_of=updated)
    add("tax_rate", to_number(lab("lblMilllevy")), "total tax rate (mill levy / 1000)",
        as_of=updated)
    for ours, id_ in TAX_MONEY_FIELDS.items():
        add(ours, parse_money(lab(id_)), TAX_DUE_NOTE if ours == "tax_due" else None,
            as_of=updated)
    for ours, id_ in TAX_FLAGS.items():
        raw = lab(id_)
        add(ours, {"Y": True, "N": False}.get(raw or "", raw), as_of=updated)
    add("tax_bill_pdf_url",
        cfg["bill_url"].format(parcel_id=ctx.ids.get("parcel_id") or lab("lblAIN"),
                               account=account),
        "Treasurer's tax statement (PDF) for the current tax year")
    ctx.add(obs)
    ctx.add(prior_year(cfg, ctx, account))


def prior_year(cfg: dict, ctx: Context, account: str) -> list[Observation]:
    source, url = cfg["source"], cfg["prior_year_url"]
    try:
        html, sid, req = fetch_text(url, {cfg["id_param"]: account}, source, ctx.client)
    except SourceUnavailable as e:
        return fields_status(PRIOR_TAX_FIELDS, "source_unavailable",
                             f"prior-year page could not be read: {e}", source, url)
    soup = BeautifulSoup(html, "html.parser")

    def lab(id_: str) -> Optional[str]:
        return label(soup, f"ContentPlaceHolder1_{id_}")

    years = re.search(r"(\d{4}) Payable (\d{4})", lab("lblPayable") or "")
    year = years[1] if years else None
    obs: list[Observation] = []
    add = adder(obs, dict(source=source, url=req, snapshot_id=sid, record_url=req),
                "blank on the Treasurer's prior-year page")
    add("tax_prior_year", year, f"taxes for {year} payable {years[2]}" if years else None)
    add("tax_prior_year_rate", to_number(lab("lblTaxRate")), "total tax rate", as_of=year)
    for ours, id_ in PRIOR_MONEY_FIELDS.items():
        add(ours, parse_money(lab(id_)), "only amounts received and applied by December 31 "
            "of the payable year (per the Treasurer's page)", as_of=year)
    return obs


# ------------------------------------------------------- tax check hooks
STATEMENT_LINE = re.compile(r"^\*?\s*([A-Z][A-Z0-9 .&'/()#,-]+?)\s+(0\.\d{6})\s+([\d,]+\.\d{2})\s*$")


def levies(url: str, client: httpx.Client) -> tuple[Optional[str], list[dict]]:
    """(levy year, [{code, authority, mills}]) from the Assessor's Levy.aspx."""
    r = client.get(url, headers=HEADERS, follow_redirects=True)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    year = re.search(r"\b(\d{4}) Tax Levies", soup.get_text(" "))
    out, seen = [], set()
    for tr in soup.find_all("tr"):
        tds = [td.get_text(" ", strip=True) for td in tr.find_all("td", recursive=False)]
        if len(tds) == 3 and re.fullmatch(r"\d{3,5}", tds[0]) and tds[0] not in seen:
            seen.add(tds[0])                   # the page repeats the table in nested layouts
            out.append({"code": tds[0], "authority": tds[1], "mills": float(tds[2])})
    return (year[1] if year else None), out


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
                        "amount": float(m[3].replace(",", ""))})
    return out
