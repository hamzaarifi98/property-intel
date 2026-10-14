"""Facts derived from what the platforms found; nothing is fetched here.
They work for any county whose platforms give the field names used below.

    owners            the owner of record split into parties, with the tenancy
    owner_absentee    the mailing address is not the site address
    living_area_sqft  floors above grade; basement_sqft apart (from building_areas)
    land_sqft         from the acreage when the Assessor gives no square feet;
                      compared with the GIS polygon area
    sale dates        the GIS date and the Assessor's date labelled against the
                      Clerk & Recorder's recording date
    jurisdiction      a note when the postal city is not the jurisdiction
"""
from __future__ import annotations

import re
from typing import Optional

from core.context import Context
from core.derive.owners import split_owner
from core.models import Observation
from core.common import normalize_address

LIVING_AREA = re.compile(r"\b(first|second|third|main|upper|ground)\s+floor\b|\bhalf\s+story\b"
                         r"|\bfloor\s+\d\b|\bliving\b", re.I)
NOT_LIVING_AREA = re.compile(r"basement|garage|patio|porch|deck|terrace|carport|balcony", re.I)
BASEMENT = re.compile(r"basement", re.I)
BASEMENT_TOTAL = re.compile(r"basement\s+total", re.I)   # when given, the finished part is in it
SQFT_PER_ACRE = 43_560


def append(o: Observation, text: str) -> None:
    o.note = (o.note or "") + text


def owner_parties(ctx: Context) -> None:
    owner, tenancy = ctx.found("owner"), ctx.found("ownership_type")
    if not owner:
        return
    split = split_owner(owner.value)
    note = (f"split from the owner of record {owner.value!r} (the county writes people as "
            f"LAST FIRST)" if split["type"] in ("individual", "individuals")
            else f"one {split['type']}; a business or trust name is not split")
    if tenancy:
        note += f"; tenancy: the Assessor's ownership type ({tenancy.value})"
    ctx.add([owner.model_copy(update={"field": "owners", "note": note, "value": {
        "type": split["type"], "tenancy": tenancy.value if tenancy else None,
        "parties": split["parties"]}})])


def absentee_owner(ctx: Context) -> None:
    def v(field: str) -> Optional[str]:
        x = ctx.value(field)
        return str(x) if x is not None else None
    street, site = v("owner_mail_street"), v("site_address")
    src = ctx.found("owner_mail_street") or ctx.found("owner")
    if not src:
        return
    if not street or not site:
        ctx.add([src.model_copy(update={"field": "owner_absentee", "value": None,
                                        "status": "not_provided",
                                        "note": "no mailing or site address to compare"})])
        return
    a, b = normalize_address(street), normalize_address(site)
    zip_a, zip_b = (v("owner_mail_zip") or "")[:5], (v("site_zip") or "")[:5]
    building = all(a[k] == b[k] for k in ("number", "predir", "name")) and \
        (not zip_a or not zip_b or zip_a == zip_b)
    mail = ", ".join(x for x in (street, v("owner_mail_city"), v("owner_mail_zip")) if x)
    if building and a["unit"] == b["unit"]:
        absentee, note = False, (f"the mailing address ({mail}) is the site address: "
                                 f"owner-occupied or at least mailed at the property")
    elif building and not (a["unit"] and b["unit"]):
        # one side has no unit (e.g. mail to the building, parcel is unit #4): same building
        absentee, note = False, (f"the mailing address ({mail}) is the site's building "
                                 f"({site}) without a unit number: mailed at the property")
    elif building:
        absentee, note = True, (f"the mailing address ({mail}) is another unit in the site's "
                                f"building ({site})")
    else:
        absentee, note = True, f"the mailing address ({mail}) is not the site address ({site})"
    ctx.add([src.model_copy(update={"field": "owner_absentee", "value": absentee, "note": note})])


def building_areas(ctx: Context) -> None:
    src = ctx.found("building_areas")
    if not src:
        return
    areas = src.value
    living = [a for a in areas if a["sqft"] and LIVING_AREA.search(a["description"] or "")
              and not NOT_LIVING_AREA.search(a["description"] or "")]
    basement = [a for a in areas if a["sqft"] and BASEMENT.search(a["description"] or "")]
    if any(BASEMENT_TOTAL.search(a["description"] or "") for a in basement):
        basement = [a for a in basement if BASEMENT_TOTAL.search(a["description"] or "")]
    for field, rows, ok_note, empty in (
            ("living_area_sqft", living,
             "above-grade living area: " + " + ".join(f"{a['description']} {a['sqft']}"
                                                      for a in living)
             + "; basement, garage, patio and porches excluded",
             "no above-grade floor lines on the Assessor's page"),
            ("basement_sqft", basement,
             "reported apart from the living area: " + ", ".join(
                 f"{a['description']} {a['sqft']}" for a in basement),
             "no basement on the Assessor's page")):
        ctx.add([src.model_copy(update={"field": field, "value": sum(a["sqft"] for a in rows),
                                        "note": ok_note}) if rows else
                 src.model_copy(update={"field": field, "value": None, "status": "not_provided",
                                        "note": empty})])


def land_area(ctx: Context) -> None:
    """land_sqft: the Assessor's (land lines in SF, else acres x 43,560) is primary;
    the GIS polygon area is secondary and compared in the note."""
    land, acres = ctx.found("land_sqft"), ctx.found("land_acres")
    if land is None and acres and isinstance(acres.value, (int, float)) and acres.value > 0:
        a = acres.value
        places = len(str(a).split(".")[1]) if "." in str(a) else 0
        land = acres.model_copy(update={
            "field": "land_sqft", "value": round(a * SQFT_PER_ACRE),
            "note": f"Assessor: {a} acres x {SQFT_PER_ACRE:,} (no land lines in square feet); "
                    f"the acreage has {places} decimals, so +/- "
                    f"{round(0.5 * 10 ** -places * SQFT_PER_ACRE):,} SF"})
        ctx.add([land])
    elif land is None:
        src = acres or ctx.found("land_lines")
        if src:
            ctx.add([src.model_copy(update={"field": "land_sqft", "value": None,
                                            "status": "not_provided",
                                            "note": "no square feet or acreage from the Assessor"})])
    gis = ctx.found("gis_area_sqft")
    if land and gis and land.value:
        diff = (gis.value - land.value) / land.value * 100
        append(land, f". Primary figure; the GIS polygon (gis_area_sqft) is "
                     f"{round(gis.value):,} SF, {diff:+.1f}%")
        append(gis, f"; secondary to land_sqft ({land.value:,} SF, the Assessor's)")


def jurisdiction_note(ctx: Context) -> None:
    """The postal city is not the jurisdiction: an Aurora ZIP can be unincorporated."""
    j, city = ctx.found("jurisdiction"), ctx.found("site_city")
    if j and city and str(city.value).upper() != str(j.value).upper():
        where = (f"unincorporated {ctx.county['name']} County" if j.value == "UNINCORPORATED"
                 else f"the jurisdiction {j.value}")
        append(j, f". The postal city ({city.value}) is not the jurisdiction: the parcel is in "
                  f"{where}, which sets its zoning and issues its permits")


def sale_dates(ctx: Context) -> None:
    """Label the two last-sale dates: the GIS date against the Clerk's recording
    date, and the Assessor's sale date (often the deed's date, before recording)."""
    gis, deed, sales = (ctx.found("last_sale_date"), ctx.found("owner_acquisition_deed"),
                        ctx.found("sales"))
    recorded = deed.value.get("recorded") if deed else None
    reception = deed.value.get("instrument") if deed else None
    if gis and recorded:
        gis.note = ("recording date: the same as the Clerk & Recorder's recorded date of "
                    f"{reception}" if gis.value == recorded else
                    f"the parcel layer's sale date; the Clerk & Recorder recorded {reception} on "
                    f"{recorded}")
    if sales and reception:
        for s in sales.value:
            if (s.get("book_page") or "").replace(" ", "") == reception:
                s["recorded"] = recorded
                if s.get("date") and recorded and s["date"] != recorded:
                    append(sales, f". For {reception}, date ({s['date']}) is the Assessor's sale "
                                  f"date and recorded ({recorded}) the Clerk & Recorder's "
                                  f"recording date: a deed is usually signed before it is "
                                  f"recorded")
