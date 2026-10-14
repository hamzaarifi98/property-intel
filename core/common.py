"""Helpers every state and platform shares: HTTP and ArcGIS queries, raw-response
snapshots, result files, value parsing and address normalizing."""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import httpx
import usaddress
from shapely.geometry import MultiPolygon, Polygon

from core.models import utcnow

ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = Path(os.environ.get("RESULTS_DIR", ROOT / "results"))
# Raw responses are written only with SAVE_RAW=1 (for debugging a source);
# otherwise a run leaves just its result file (save_result).
SAVE_RAW = os.environ.get("SAVE_RAW") == "1"
RAW_DIR = RESULTS_DIR / "raw"
UNSAFE_NAME_CHARS = r'[\\/:*?"<>|\x00-\x1f]'  # not allowed in Windows/Linux file names
PAGE_SIZE = 2000  # a common ArcGIS maxRecordCount

DIRECTIONS = {"NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
              "NORTHEAST": "NE", "NORTHWEST": "NW", "SOUTHEAST": "SE", "SOUTHWEST": "SW"}
STREET_TYPES = {"STREET": "ST", "AVENUE": "AVE", "AV": "AVE", "ROAD": "RD", "DRIVE": "DR",
                "BOULEVARD": "BLVD", "LANE": "LN", "COURT": "CT", "CIRCLE": "CIR",
                "PLACE": "PL", "PARKWAY": "PKWY", "HIGHWAY": "HWY", "TERRACE": "TER",
                "TRAIL": "TRL", "WAY": "WAY", "PLAZA": "PLZ"}


class SourceUnavailable(Exception):
    """The source could not be reached or returned an error."""


def query_layer(params: dict, client: Optional[httpx.Client] = None, url: str = "",
                source: str = "", page_size: int = PAGE_SIZE,
                post: bool = False) -> tuple[dict, Optional[str], str]:
    """Run one query against an ArcGIS layer (`url`), following pagination.

    Returns (data, snapshot_id, request_url); snapshot_id is None unless
    SAVE_RAW=1 (see save_snapshot). post=True sends the parameters as a form
    body, for geometries too long for a URL; the request URL then has no query
    string.
    """
    own_client = client is None
    client = client or httpx.Client(timeout=30)
    base = {"outFields": "*", "returnGeometry": "true", "outSR": "4326", "f": "json"}
    features: list[dict] = []
    pages: list[dict] = []
    request_url = ""
    try:
        offset = 0
        while True:
            p = {**base, **params, "resultOffset": offset, "resultRecordCount": page_size}
            try:
                r = client.post(url, data=p) if post else client.get(url, params=p)
            except httpx.HTTPError as e:
                raise SourceUnavailable(f"{type(e).__name__}: {e}") from e
            if not request_url:
                request_url = str(r.request.url)
            pages.append({"url": str(r.request.url), "http_status": r.status_code, "body": r.text})
            if r.status_code != 200:
                raise SourceUnavailable(f"HTTP {r.status_code}")
            try:
                data = r.json()
            except ValueError as e:
                raise SourceUnavailable("response was not JSON") from e
            # ArcGIS reports query errors with HTTP 200 and an "error" key
            if "error" in data:
                raise SourceUnavailable(f"ArcGIS error: {data['error']}")
            features.extend(data.get("features", []))
            if not data.get("exceededTransferLimit"):
                break
            offset += page_size
    finally:
        snapshot_id = save_snapshot(params, pages, source)
        if own_client:
            client.close()
    return {"features": features}, snapshot_id, request_url


def save_snapshot(params: dict, pages: list[dict], source: str = "") -> Optional[str]:
    """Save the raw responses to results/raw/<id>.json when SAVE_RAW=1; returns the id or None."""
    if not SAVE_RAW:
        return None
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    retrieved_at = utcnow().isoformat()
    payload = {"source": source, "params": params, "retrieved_at": retrieved_at, "pages": pages}
    raw = json.dumps(payload, sort_keys=True)
    snapshot_id = hashlib.sha256(raw.encode()).hexdigest()[:16]
    (RAW_DIR / f"{snapshot_id}.json").write_text(raw)
    return snapshot_id


def save_result(subdir: str, label: str, payload: str, time_format: str) -> Path:
    """Write one run's result to results/<subdir>/<label> - <local time>.json.

    `label` is what the user typed (address or id), made safe for a file name.
    Never overwrites: a second run in the same minute gets " (2)", and so on.
    """
    folder = RESULTS_DIR / subdir
    folder.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"\s+", " ", re.sub(UNSAFE_NAME_CHARS, " ", label)).strip()[:150] or "result"
    stem = f"{name} - {datetime.now().strftime(time_format)}"
    path, n = folder / f"{stem}.json", 2
    while path.exists():
        path, n = folder / f"{stem} ({n}).json", n + 1
    path.write_text(payload)
    return path


def sql_quote(value: str) -> str:
    """Escape a value for a single-quoted ArcGIS where clause."""
    return value.replace("'", "''")


def normalize_address(text: str) -> dict:
    """Parse an address into upper-cased, USPS-abbreviated parts."""
    text = text.strip()
    try:
        tagged, _ = usaddress.tag(text)
        parts = dict(tagged)
    except usaddress.RepeatedLabelError:
        parts = {}
    if not parts.get("AddressNumber"):
        m = re.match(r"\s*(\d+)\s+(.+)", text)
        parts = {"AddressNumber": m.group(1), "StreetName": m.group(2).split(",")[0]} if m else {}

    def up(key: str) -> str:
        return re.sub(r"[.,#]", "", parts.get(key, "")).strip().upper()

    predir = up("StreetNamePreDirectional")
    stype = up("StreetNamePostType")
    return {
        "number": up("AddressNumber"),
        "predir": DIRECTIONS.get(predir, predir),
        "name": re.sub(r"\s+", " ", up("StreetName")),
        "type": STREET_TYPES.get(stype, stype),
        "postdir": DIRECTIONS.get(up("StreetNamePostDirectional"), up("StreetNamePostDirectional")),
        "unit": up("OccupancyIdentifier"),
        "city": up("PlaceName"),
        "zip": up("ZipCode")[:5],
    }


def looks_like_apn(text: str) -> bool:
    """An APN has digits and maybe separators, but no street words."""
    t = text.strip()
    return bool(re.fullmatch(r"[A-Za-z]?[\d][\d\-\.\s]{3,}[A-Za-z]?", t))


def geometry_from_rings(rings: list) -> Optional[Polygon | MultiPolygon]:
    """Esri rings -> shapely. Esri outer rings are clockwise, holes counter-clockwise."""
    if not rings:
        return None
    outers: list[list] = []
    holes: list[list] = []
    for ring in rings:
        poly = Polygon(ring)
        if not poly.is_valid and poly.area == 0:
            continue
        (holes if poly.exterior.is_ccw else outers).append(ring)
    if not outers:  # odd data: treat every ring as an outer ring
        outers, holes = holes, []
    polys = []
    for outer in outers:
        shell = Polygon(outer)
        inner = [h for h in holes if shell.contains(Polygon(h).representative_point())]
        polys.append(Polygon(outer, inner))
    return polys[0] if len(polys) == 1 else MultiPolygon(polys)


def clean(value: Any) -> Any:
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


def iso_date(value: Any) -> Optional[str]:
    """'4/21/2026' or '04/21/2026 00:00:00' -> '2026-04-21'; ISO dates pass through.

    Anything else is returned cleaned and unchanged, so an odd value stays visible.
    """
    s = clean(value)
    if not isinstance(s, str):
        return s
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})\b", s)
    if m:
        return f"{m[3]}-{int(m[1]):02d}-{int(m[2]):02d}"
    m = re.match(r"\d{4}-\d{2}-\d{2}", s)
    return m.group(0) if m else s


def parse_money(value: Any) -> Optional[float]:
    if value is None:
        return None
    s = re.sub(r"[$,\s]", "", str(value))
    try:
        return float(s)
    except ValueError:
        return None
