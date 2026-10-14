"""One parcel lookup for any configured county.

Every step is a platform chosen by the county's config (core/config.py); this
module only runs them in order and adds the derived facts. What differs by state
(statewide resolver, business registry, tax check, its own platforms) comes from
states/<state>/state.py (core/state.py):

    1  parcel           the county's parcel layer (or the statewide one): the record
    2  assessor         detail page: legal, values, sales, buildings, land
    3  value_history    value by year
    4  treasurer        tax bill; then the state's tax check
    5  jurisdiction     who governs the parcel; zoning from that jurisdiction
       flood            FEMA flood (FEMA's own layer unless the county has a copy)
    6  entity           a business owner in the state's business registry
    7  recorder         recorded documents, the deed to the owner, Statement of Authority
    8  permits          from whoever issues them (the jurisdiction's config)
    9  site entities    businesses registered at the site address
    10 contact routes   from everything above (core/derive/contacts.py)

A step the county has no config for is skipped and named in the record's notes;
a step that fails marks only its own fields. Nothing here names a state or county.

    python -m core.lookup "18949 E Garden Dr, Aurora, CO 80015"          # --state colorado is the default
    python -m core.lookup 2077-16-2-22-004 --county Arapahoe
    python -m core.lookup 034472207 --county Arapahoe --no-detail
    python -m core.lookup "<address>" --state texas
"""
from __future__ import annotations

import sys
from typing import Callable, Optional

import httpx

from core import config, state as states
from core.common import looks_like_apn, save_result
from core.context import Context
from core.derive import contacts, property as prop
from core.models import Observation, SourceRecord
from core.platforms import accela, arcgis, etrakit, publicsearch

# shared platform name (as in a config) -> step function(cfg, ctx); a state adds its
# own (state.PLATFORMS)
PLATFORMS: dict[str, Callable[[dict, Context], None]] = {
    "arcgis.value_history": arcgis.value_history,
    "arcgis.point": arcgis.point,
    "arcgis.flood": arcgis.flood,
    "publicsearch": publicsearch.run,
    "etrakit": etrakit.run,
    "accela": accela.run,
}
# recorder platform -> its search page for a reception number or a name
SEARCH_URLS = {"publicsearch": publicsearch.search_url}
RESULT_TIME_FORMAT = "%H-%M - %d-%m-%y"


def platform(ctx: Context, cfg: dict) -> Callable[[dict, Context], None]:
    return {**PLATFORMS, **getattr(ctx.state, "PLATFORMS", {})}[cfg["platform"]]


def run_step(name: str, ctx: Context, skipped: list[str]) -> bool:
    cfg = ctx.section(name)
    if cfg is None:
        skipped.append(name)
        return False
    platform(ctx, cfg)(cfg, ctx)
    return True


def lookup(county_name: str, key: str, client: Optional[httpx.Client] = None,
           detail: bool = True, state: str = states.DEFAULT) -> SourceRecord:
    """Look up one parcel of a configured county by its id. Raises
    config.ConfigError for an unconfigured county, ValueError for a bad id or state."""
    county = config.county(county_name, state)
    forms = county.get("ids")
    kind, value = arcgis.normalize_id(key, forms) if forms else ("parcel id", key.strip())
    ctx_args = dict(county=county, defaults=config.defaults(state), state=states.load(state))
    if client is not None:
        return _lookup(Context(client=client, **ctx_args), kind, value, key, detail)
    with httpx.Client(timeout=30) as own:
        return _lookup(Context(client=own, **ctx_args), kind, value, key, detail)


def _lookup(ctx: Context, kind: str, value: str, key: str, detail: bool) -> SourceRecord:
    county, client, st = ctx.county, ctx.client, ctx.state
    record = SourceRecord(source=f"{county['name']} County ({county['parcel']['source']})",
                          input=key, status="not_found")
    if value != key.strip():
        record.notes.append(f"searched as {kind.upper()} {value!r}")

    # 1. the parcel record decides found / not found / ambiguous
    status, notes = arcgis.parcel(county["parcel"], ctx, kind, value)
    record.status, record.notes = status, record.notes + notes
    if status != "ok":
        return record
    parcel_obs = ctx.found("owner") or ctx.observations[0]
    record.url, record.snapshot_id = parcel_obs.url, parcel_obs.snapshot_id
    recorder = ctx.section("recorder")
    if recorder and recorder["platform"] in SEARCH_URLS:
        search = SEARCH_URLS[recorder["platform"]]
        common = dict(source=parcel_obs.source, url=parcel_obs.url, record_url=parcel_obs.record_url)
        if ctx.value("last_sale_reception"):
            ctx.add([Observation(field="last_sale_deed_url", **common,
                                 value=search(recorder, ctx.value("last_sale_reception")),
                                 note="Clerk & Recorder search for the reception number")])
        if ctx.value("owner"):
            ctx.add([Observation(field="owner_records_search_url", **common,
                                 value=search(recorder, ctx.value("owner")),
                                 note="Clerk & Recorder search by owner name: deeds, deeds of "
                                      "trust, liens and any Statement of Authority. Read in "
                                      "recorded_documents, owner_acquisition_deed and "
                                      "statement_of_authority")])
    if not detail:
        return finish(record, ctx, [])

    skipped: list[str] = []
    # 2-4. Assessor, value history, Treasurer and the tax check
    run_step("assessor", ctx, skipped)
    prop.building_areas(ctx)
    prop.land_area(ctx)
    run_step("value_history", ctx, skipped)
    if run_step("treasurer", ctx, skipped) and getattr(st, "tax_check", None):
        st.tax_check(ctx, ctx.section("treasurer"))

    # 5. jurisdiction, then its zoning; flood
    run_step("jurisdiction", ctx, skipped)
    jcfg = jurisdiction_config(ctx)
    zoning(ctx, jcfg)
    run_step("flood", ctx, skipped)
    prop.jurisdiction_note(ctx)

    # 6-7. owner: parties, absentee, business entity, recorded documents
    prop.owner_parties(ctx)
    prop.absentee_owner(ctx)
    owner = ctx.value("owner")
    if owner and ctx.section("entity") and getattr(st, "entity", None):
        entity = st.entity(owner, client)
        ctx.add(entity.observations)
        ctx.notes += [f"owner entity: {n}" for n in entity.notes]
    run_step("recorder", ctx, skipped)
    prop.sale_dates(ctx)

    # 8. permits, from whoever issues them
    permits(ctx, jcfg)

    # 9. businesses registered at the site address (possible occupants)
    if ctx.section("entity") and getattr(st, "site_entities", None):
        e = ctx.found("owner_entity")
        ctx.add(st.site_entities(ctx.value("site_address"), ctx.value("site_zip"), client,
                              exclude=e.value.get("entity_id") if e and isinstance(e.value, dict)
                              else None))

    # 10. contact routes (nothing fetched)
    contacts.address_owner_note(ctx.observations)
    ctx.add(contacts.contact_routes(ctx.observations))
    return finish(record, ctx, skipped)


def jurisdiction_config(ctx: Context) -> Optional[dict]:
    """The county's [jurisdictions.<NAME>] section, or the city config it points at."""
    j = ctx.value("jurisdiction")
    jcfg = ctx.county.get("jurisdictions", {}).get(j) if j else None
    if jcfg and "city" in jcfg:
        city = config.city(jcfg["city"], states.slug_of(ctx.state))
        return {**city, **{k: v for k, v in jcfg.items() if k != "city"}}
    return jcfg


def issuer(ctx: Context, jcfg: Optional[dict]) -> str:
    j = ctx.value("jurisdiction")
    if jcfg and jcfg.get("issuer"):
        return jcfg["issuer"]
    return f"the City of {str(j).title()}" if j else "the jurisdiction"


def zoning(ctx: Context, jcfg: Optional[dict]) -> None:
    if jcfg and jcfg.get("zoning"):
        platform(ctx, jcfg["zoning"])(jcfg["zoning"], ctx)
    elif ctx.value("jurisdiction"):
        j = ctx.found("jurisdiction")
        ctx.add(arcgis.fields_status(["zoning_code"], "not_provided",
                                     f"zoning is set by {issuer(ctx, jcfg)}, whose map is not "
                                     f"configured", j.source, j.url, j.snapshot_id))


def permits(ctx: Context, jcfg: Optional[dict]) -> None:
    if jcfg and jcfg.get("permits"):
        platform(ctx, jcfg["permits"])(jcfg["permits"], ctx)
        return
    j = ctx.found("jurisdiction")
    if j:
        ctx.add(arcgis.fields_status(["permits"], "not_provided",
                                     f"permits are issued by {issuer(ctx, jcfg)}, whose permit "
                                     f"portal is not configured", j.source, j.url))


def finish(record: SourceRecord, ctx: Context, skipped: list[str]) -> SourceRecord:
    record.observations = ctx.observations
    record.notes += ctx.notes
    if skipped:
        record.notes.append(f"not configured for {ctx.county['name']} County: "
                            f"{', '.join(skipped)}")
    down = sorted({o.source for o in record.observations if o.status == "source_unavailable"})
    record.notes += [f"{s} unavailable for some fields; see their observations" for s in down]
    record.notes += [f"{o.field}: {o.note}" for o in record.observations
                     if o.status == "check_failed"]
    return record


# --------------------------------------------------------------------- CLI

def option(args: list[str], name: str) -> tuple[Optional[str], list[str]]:
    """'--county Arapahoe' out of the arguments: (value, the other arguments)."""
    if name not in args:
        return None, args
    i = args.index(name)
    return " ".join(args[i + 1:i + 2]) or None, args[:i] + args[i + 2:]


def main(argv: list[str]) -> None:
    detail = "--no-detail" not in argv
    args = [a for a in argv if a != "--no-detail"]
    county_name, args = option(args, "--county")
    state_slug, args = option(args, "--state")
    state_slug = state_slug or states.DEFAULT
    text = " ".join(args).strip()
    if not text:
        sys.exit('usage: python -m core.lookup "<address or parcel id>" [--state STATE] '
                 '[--county NAME] [--no-detail]')
    try:
        st = states.load(state_slug)
    except ValueError as e:
        sys.exit(str(e))
    key = text
    if county_name is None or not looks_like_apn(text):
        if getattr(st, "resolve", None) is None:
            sys.exit(f"{st.NAME} has no statewide resolver yet: give a county id with --county NAME")
        res = st.resolve(text)             # the statewide source finds the parcel and county
        if res.status != "match" or res.parcel is None:
            print(res.model_dump_json(indent=2))
            hint = " (for a county id such as a PIN, add --county NAME)" if looks_like_apn(text) else ""
            sys.exit(f"statewide resolve returned {res.status}{hint}")
        found = res.parcel.county or ""
        if county_name and config.slug(found) != config.slug(county_name):
            sys.exit(f"{text!r} is in {found} County, not {county_name}")
        county_name, key = found, res.parcel.parcel_id or ""
    try:
        rec = lookup(county_name, key, detail=detail, state=state_slug)
    except (config.ConfigError, ValueError) as e:
        sys.exit(str(e))
    payload = rec.model_dump_json(indent=2)
    print(payload)
    path = save_result(f"{state_slug}/{config.slug(county_name)}", text, payload, RESULT_TIME_FORMAT)
    print(f"saved to {path}", file=sys.stderr)      # stderr keeps piped JSON clean


if __name__ == "__main__":
    main(sys.argv[1:])
