"""ArcGIS REST layers: parcels, value history, point layers, flood.

Most Colorado counties publish parcels on an ArcGIS server; a county config
gives the layer URL and maps its field names to ours. Steps:

    parcels        the county parcel layer: one parcel by id (config `parcel`)
    value_history  value-by-year layers (config `value_history`)
    point          any layer queried at the parcel's point: jurisdiction, zoning
    flood          FEMA flood zones (point and polygon) and FIRM panel; FEMA's own
                   NFHL service by default, or a county's copy

Every step reports its own failures; none raises.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Optional

from shapely.geometry import mapping

from core.context import Context
from core.models import Observation
from core.common import (SourceUnavailable, clean, geometry_from_rings, iso_date, parse_money,
                     query_layer, sql_quote)

POINT_NOTE = "at the county's point inside the parcel"
CENTROID_NOTE = "at the parcel polygon's representative point"
ZERO_SALE_NOTE = "$0 transfer: not a market sale (e.g. quitclaim or related-party transfer)"
NO_BFE = -9999                                     # FEMA's "no base flood elevation"


def fields_status(fields: list[str], status: str, note: str, source: str, url: str,
                  snapshot_id: Optional[str] = None) -> list[Observation]:
    """The same status for every field of a step that could not run or found nothing."""
    return [Observation(field=f, status=status, note=note, source=source, url=url,
                        snapshot_id=snapshot_id) for f in fields]


def rows(cfg_url: str, params: dict, source: str, ctx: Context,
         post: bool = False) -> tuple[list[dict], Optional[str], str]:
    data, sid, req = query_layer(params, ctx.client, url=cfg_url, source=source, post=post)
    return [f["attributes"] for f in data["features"]], sid, req


def distinct(records: list[dict], field: str) -> Any:
    """The one value of `field` across records, a list when they differ, None when blank."""
    values: list = []
    for r in records:
        v = clean(r.get(field))
        if v is not None and v not in values:
            values.append(v)
    return values[0] if len(values) == 1 else (values or None)


def epoch_ms_to_date(value: Any) -> Optional[str]:
    if not isinstance(value, (int, float)):
        return None
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).date().isoformat()


# ------------------------------------------------------------------ ids

def normalize_id(text: str, forms: list[dict]) -> tuple[str, str]:
    """The typed id in its county form, by the config's `ids` list.

    A form is {kind, digits, format: '####-##-#-##-###'} (exactly that many digits)
    or {kind, max_digits, zero_pad} (digits only). Raises ValueError.
    """
    t = re.sub(r"\s+", "", text)
    digits = re.sub(r"\D", "", t)
    for f in forms:
        if f.get("pattern") and re.fullmatch(f["pattern"], t.upper()):   # e.g. R0085603
            return f["kind"], t.upper()
        if f.get("digits") == len(digits) and re.fullmatch(r"[\d\-\.]+", t):
            it = iter(digits)
            return f["kind"], "".join(next(it) if ch == "#" else ch for ch in f["format"])
        if f.get("max_digits") and digits == t and len(digits) <= f["max_digits"]:
            return f["kind"], digits.zfill(f["max_digits"]) if f.get("zero_pad") else digits
    examples = " or ".join(f"{f['kind'].upper()} ({f.get('example', '?')})" for f in forms)
    raise ValueError(f"expected {examples}, got {text!r}")


# -------------------------------------------------------------- parcels

def parcel(cfg: dict, ctx: Context, kind: str, value: str) -> tuple[str, list[str]]:
    """Find one parcel by id; add its observations. Returns (status, notes):
    ok, not_found, ambiguous or source_unavailable."""
    source, fields = cfg["source"], cfg["fields"]
    q = sql_quote(value)
    where = " OR ".join(f"{f} = '{q}'" for f in cfg["search_fields"])
    if cfg.get("filter"):                  # e.g. one county of the statewide layer
        where = f"({where}) AND {cfg['filter']}"
    try:
        data, sid, url = query_layer({"where": where, "returnGeometry": "true"}, ctx.client,
                                     url=cfg["url"], source=source,
                                     page_size=cfg.get("page_size", 1000))
    except SourceUnavailable as e:
        return "source_unavailable", [f"{source} unavailable: {e}"]
    features = data["features"]
    if not features:
        return "not_found", [f"no parcel with this {kind.upper()} in {source}"]
    ids = sorted({clean(f["attributes"].get(fields["parcel_id"])) or "?" for f in features})
    merged_note = None
    if len(features) > 1 and cfg.get("merge_rows") and len(ids) == 1:
        # some layers repeat a parcel once per owner: one parcel, values joined
        attrs = merge_rows([f["attributes"] for f in features])
        merged_note = f"{len(features)} rows for this parcel in the layer (one per owner); joined"
    elif len(features) > 1:
        return "ambiguous", [f"{len(features)} records match in {source}: {ids}"]
    else:
        attrs = features[0]["attributes"]
    ctx.ids = {k: clean(attrs.get(fields[k])) for k in ("parcel_id", "account") if k in fields}
    record_url = (clean(attrs.get(cfg["record_url_field"])) if cfg.get("record_url_field")
                  else None) or cfg.get("record_url", "").format(**ctx.ids) or None
    as_of = iso_date(attrs.get(cfg["as_of_field"])) if cfg.get("as_of_field") else None
    common = dict(source=source, url=url, snapshot_id=sid, record_url=record_url, as_of=as_of)
    dates, money = set(cfg.get("date_fields", [])), set(cfg.get("money_fields", []))
    notes = cfg.get("notes", {})
    blank = cfg.get("blank_note", f"blank in {source}")
    for ours, theirs in fields.items():
        v = clean(attrs.get(theirs))
        if ours in dates:                             # epoch milliseconds, or text
            v = iso_date(v) if isinstance(v, str) else epoch_ms_to_date(v)
        if ours in money and v is not None:           # some layers store money as text
            v = parse_money(v)
        note = ZERO_SALE_NOTE if ours == "last_sale_price" and v == 0 else notes.get(ours)
        if merged_note and ours == "owner":
            note = f"{note}; {merged_note}" if note else merged_note
        ctx.add([Observation(field=ours, status="not_provided", note=blank, **common)
                 if v is None else Observation(field=ours, value=v, note=note, **common)])

    # the point the location steps query: the county's own, else inside the polygon
    ctx.rings = (features[0].get("geometry") or {}).get("rings")
    x, y = (attrs.get(f) for f in cfg.get("point_fields", [None, None]))
    if isinstance(x, (int, float)) and isinstance(y, (int, float)):
        ctx.point, ctx.point_note = (x, y), POINT_NOTE
    elif ctx.rings:
        p = geometry_from_rings(ctx.rings).representative_point()
        ctx.point, ctx.point_note = (p.x, p.y), CENTROID_NOTE
    if cfg.get("polygon_field") and ctx.rings:
        ctx.add([Observation(field=cfg["polygon_field"],
                             value=mapping(geometry_from_rings(ctx.rings)),
                             note="GeoJSON, WGS84", **common)])
    return "ok", []


def merge_rows(records: list[dict]) -> dict:
    """Rows of one parcel -> one: differing text joined with ' & ' (owners), other
    differing values kept from the first row."""
    out = dict(records[0])
    for k in out:
        values = []
        for r in records:
            v = clean(r.get(k))
            if v is not None and v not in values:
                values.append(v)
        if len(values) > 1 and all(isinstance(v, str) for v in values):
            out[k] = " & ".join(values)
    return out


# -------------------------------------------------------- value history

def value_history(cfg: dict, ctx: Context) -> None:
    """Appraised value per tax year, newest first, one observation per year with
    the year in as_of. The current value (cfg `current`) is added under the tax
    year of the Assessor's values (value_tax_year)."""
    field, source = "appraised_total_history", cfg["source"]
    pid = ctx.ids.get("parcel_id")
    first_url = cfg["url"].format(layer=next(iter(cfg["layers"])))
    if not pid:
        ctx.add(fields_status([field], "not_provided", "no parcel id to look up", source,
                              first_url))
        return
    by_year: dict[str, Observation] = {}
    current, tax_year = ctx.found(cfg.get("current", "appraised_total")), ctx.value("value_tax_year")
    if current is not None and tax_year:
        by_year[tax_year] = current.model_copy(update={
            "field": field, "as_of": tax_year,
            "note": "current value in the county GIS layer; tax year from the Assessor's page"})
    errors = []
    for layer, years in cfg["layers"].items():
        try:
            recs, sid, url = rows(cfg["url"].format(layer=layer),
                                  {"where": f"{cfg['id_field']} = '{sql_quote(pid)}'",
                                   "returnGeometry": "false"}, source, ctx)
        except SourceUnavailable as e:
            errors.append(f"layer {layer}: {e}")
            continue
        for year, theirs in years.items():
            v = distinct(recs, theirs)
            if str(year) in by_year or v in (None, 0):     # 0 is how these layers leave a year blank
                continue
            if isinstance(v, float) and v.is_integer():
                v = int(v)
            note = f"{theirs} in layer {layer}"
            if isinstance(v, list):
                note += f"; its {len(recs)} records for this parcel disagree"
            by_year[str(year)] = Observation(field=field, value=v, as_of=str(year), source=source,
                                             url=url, snapshot_id=sid, note=note)
    obs = [by_year[y] for y in sorted(by_year, reverse=True)]
    if errors:
        obs += fields_status([field], "source_unavailable", "; ".join(errors), source, first_url)
    elif len(obs) == (1 if current is not None and tax_year else 0):
        obs.append(Observation(field=field, status="not_found", source=source, url=first_url,
                               note="no earlier values for this parcel in the value-change layers"))
    ctx.add(obs)


# ---------------------------------------------------------- point layers

def point_params(ctx: Context) -> dict:
    x, y = ctx.point
    return {"geometry": f"{x},{y}", "geometryType": "esriGeometryPoint", "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects", "returnGeometry": "false"}


def point(cfg: dict, ctx: Context) -> None:
    """Fields of the layer feature(s) at the parcel's point: cfg.fields maps
    ours -> theirs (jurisdiction, zoning_code, ...)."""
    fields, source = list(cfg["fields"]), cfg["source"]
    if ctx.point is None:
        ctx.add(fields_status(fields, "not_provided", "no point for the parcel", source,
                              cfg["url"]))
        return
    try:
        recs, sid, url = rows(cfg["url"], point_params(ctx), source, ctx)
    except SourceUnavailable as e:
        ctx.add(fields_status(fields, "source_unavailable", f"{source}: {e}", source, cfg["url"]))
        return
    for ours, theirs in cfg["fields"].items():
        v = distinct(recs, theirs)
        ctx.add([Observation(field=ours, value=v, note=ctx.point_note, source=source, url=url,
                             snapshot_id=sid) if v is not None else
                 Observation(field=ours, status="not_provided", source=source, url=url,
                             snapshot_id=sid, note=cfg.get("empty_note",
                                                            f"nothing at {ctx.point_note}"))])


# ------------------------------------------------------------------ flood

FLOOD_FIELDS = ["flood_zone", "flood_zone_subtype", "flood_sfha", "flood_bfe"]
FLOOD_PANEL_FIELDS = ["flood_panel", "flood_panel_url"]


def flood(cfg: dict, ctx: Context) -> None:
    """FEMA flood zone at the point, Special Flood Hazard Area anywhere on the
    parcel polygon, and the FIRM panel. Layers with FEMA's NFHL field names."""
    source = cfg["source"]
    if ctx.point is None:
        ctx.add(fields_status(FLOOD_FIELDS + ["flood_sfha_on_parcel"] + FLOOD_PANEL_FIELDS,
                              "not_provided", "no point for the parcel", source, cfg["zones_url"]))
        return
    note = ctx.point_note
    try:
        recs, sid, url = rows(cfg["zones_url"], point_params(ctx), source, ctx)
    except SourceUnavailable as e:
        ctx.add(fields_status(FLOOD_FIELDS, "source_unavailable", f"flood layer: {e}", source,
                              cfg["zones_url"]))
    else:
        common = dict(source=source, url=url, snapshot_id=sid)
        sfha = distinct(recs, "SFHA_TF")
        bfe = distinct([r for r in recs if r.get("STATIC_BFE") != NO_BFE], "STATIC_BFE")
        values = {
            "flood_zone": (distinct(recs, "FLD_ZONE"), f"FEMA flood zone {note}"),
            "flood_zone_subtype": (distinct(recs, "ZONE_SUBTY"), note),
            "flood_sfha": ({"T": True, "F": False}.get(sfha, sfha) if recs else None,
                           f"in FEMA's Special Flood Hazard Area (high risk), {note}"),
            "flood_bfe": (bfe, f"static base flood elevation, {note}"),
        }
        for f, (v, n) in values.items():
            ctx.add([Observation(field=f, value=v, note=n, **common) if v is not None else
                     Observation(field=f, status="not_provided", **common,
                                 note="no static base flood elevation at the parcel's point"
                                 if f == "flood_bfe" else "no FEMA flood area mapped at the "
                                                          "parcel's point")])

    # any Special Flood Hazard Area on the polygon (sent by POST: it can be long)
    field = "flood_sfha_on_parcel"
    if not ctx.rings:
        ctx.add(fields_status([field], "not_provided", "no parcel polygon", source,
                              cfg["zones_url"]))
    else:
        params = {"geometry": json.dumps({"rings": ctx.rings, "spatialReference": {"wkid": 4326}}),
                  "geometryType": "esriGeometryPolygon", "inSR": "4326",
                  "spatialRel": "esriSpatialRelIntersects", "where": "SFHA_TF = 'T'",
                  "returnGeometry": "false"}
        try:
            recs, sid, url = rows(cfg["zones_url"], params, source, ctx, post=True)
        except SourceUnavailable as e:
            ctx.add(fields_status([field], "source_unavailable", f"flood layer: {e}", source,
                                  cfg["zones_url"]))
        else:
            zones = distinct(recs, "FLD_ZONE")
            ctx.add([Observation(
                field=field, value=bool(recs), source=source, url=url, snapshot_id=sid,
                note=(f"zone(s) {zones} touch or cross the parcel polygon; check the panel for "
                      f"how much" if recs else "no Special Flood Hazard Area touches the parcel "
                                               "polygon"))])

    # FIRM panel
    try:
        recs, sid, url = rows(cfg["panels_url"], point_params(ctx), source, ctx)
    except SourceUnavailable as e:
        ctx.add(fields_status(FLOOD_PANEL_FIELDS, "source_unavailable", f"flood panel layer: {e}",
                              source, cfg["panels_url"]))
        return
    common = dict(source=source, url=url, snapshot_id=sid)
    panel = distinct(recs, "FIRM_PAN")
    link = distinct(recs, cfg["panel_link_field"]) if cfg.get("panel_link_field") else None
    ctx.add([Observation(field="flood_panel", value=panel, note=f"FEMA flood map (FIRM) panel {note}",
                         **common) if panel else
             Observation(field="flood_panel", status="not_provided", **common,
                         note="no FEMA flood map panel at the parcel's point")])
    ctx.add([Observation(field="flood_panel_url", value=link, note="the county's copy of the panel PDF",
                         **common) if link else
             Observation(field="flood_panel_url", status="not_provided", **common,
                         note=cfg.get("panel_link_note", "no panel PDF link in this layer"))])

