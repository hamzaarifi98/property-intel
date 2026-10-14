"""GovOS (Kofile) "publicsearch" Clerk & Recorder sites: <county>.<st>.publicsearch.us.

Used in Colorado by Arapahoe, Boulder, Pitkin and Routt, and by many Texas
counties. The county config (`recorder`) gives the host, the source name and, if
they differ, the department and document-type codes.

The search site has no REST API: its page sends every search as a JSON message
over a WebSocket (wss://<host>/ws), authenticated by a token the page embeds
(window.__ort) and the session cookie it sets. Recorder does the same.

1. recorded_documents: every document naming the owner, from the index
   (grantor/grantee) and from the scanned text (OCR).
2. owner_acquisition_deed: the deed that conveyed the parcel to the owner
   (the one with the Assessor's last-sale reception number, if it is there).
3. statement_of_authority: where the state has such a document (its state.py
   AUTHORITY: document types, form wording), each one the owner recorded, with
   the person(s) who may sign for it. The index lists only the entity, so the
   names are read from the scanned text: the search returns ~200 characters
   around a match, so read_authorized searches the form's own wording and then
   that wording plus what it has read so far, until the next item of the form.

Each search's results page is the observation's url, so it opens in a browser.
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Any, Optional
from urllib.parse import urlencode

import httpx
from websockets.exceptions import WebSocketException
from websockets.sync.client import ClientConnection, connect

from core.names import entity_key
from core.context import Context
from core.derive.owners import split_owner
from core.platforms.web import HEADERS
from core.models import Observation
from core.common import SourceUnavailable, clean, iso_date, parse_money, save_snapshot

SEARCH_MESSAGE = "@kofile/FETCH_DOCUMENTS/v4"    # the page's search message
PAGE_SIZE = 50                                   # the page's own page size
TIMEOUT = 30                                     # seconds per WebSocket reply
FIELDS = ["recorded_documents", "owner_acquisition_deed", "statement_of_authority"]
# Words left out when comparing subdivision names ("TUSCANY SUB 4TH FLG" ~ "Tuscany 4th Flg")
LEGAL_STOP_WORDS = {"SUB", "SUBDIVISION", "FLG", "FILING", "FIL", "AMENDED", "AMD", "REPLAT",
                    "THE", "OF", "AND", "AT", "NO", "UNIT", "PHASE", "PH", "LOT", "LOTS",
                    "BLK", "BLOCK", "TRACT"}
RECORDED_DOCS_NOTE = ("documents naming each owner (each party searched on its own), newest "
                      "first. matched_on: index (an owner is a grantor or grantee), reception "
                      "(found by the Assessor's last-sale reception) or text (only in the "
                      "scanned document). property: whether the indexed legal description is "
                      "this parcel. excerpt: scanned text around the match (OCR, may contain "
                      "errors)")
ACQUISITION_NOTE = ("the deed that conveyed the parcel to the current owner; its grantor is "
                    "the previous owner of record")


# ------------------------------------------------------------------ session

class Recorder:
    """One page load (token + cookie) and one WebSocket, reused for every search."""

    def __init__(self, client: httpx.Client, cfg: dict, authority: Optional[dict] = None):
        self.host = host = cfg["host"]
        self.source = cfg["source"]
        self.department = cfg.get("department", "RP")          # Real Property records
        # the state's record of who may act for an entity (None: the state has none)
        self.authority = authority
        self.sta_types = cfg.get("sta_doc_types") or (authority or {}).get("doc_types", [])
        self.deed_group = cfg.get("deed_group", "Deed")
        try:
            r = client.get(f"https://{host}/", headers=HEADERS, follow_redirects=True)
        except httpx.HTTPError as e:
            raise SourceUnavailable(f"{type(e).__name__}: {e}") from e
        token = re.search(r'window\.__ort\s*=\s*"([^"]+)"', r.text)
        if r.status_code != 200 or not token:
            raise SourceUnavailable(f"HTTP {r.status_code}" if r.status_code != 200
                                    else "search page has no session token (layout change?)")
        self.token = token.group(1)
        # this site's session cookie only; the shared client holds other sites' cookies
        cookie = "; ".join(f"{c.name}={c.value}" for c in client.cookies.jar
                           if c.domain.lstrip(".") == host and c.name.startswith("authToken"))
        try:
            self.ws: ClientConnection = connect(
                f"wss://{host}/ws", open_timeout=TIMEOUT,
                additional_headers={"User-Agent": HEADERS["User-Agent"],
                                    "Origin": f"https://{host}", "Cookie": cookie})
        except (OSError, WebSocketException) as e:
            raise SourceUnavailable(f"WebSocket: {type(e).__name__}: {e}") from e

    def close(self) -> None:
        self.ws.close()

    def results_url(self, query: dict) -> str:
        """The search's results page, for a person to open."""
        params = {k: (",".join(v) if isinstance(v, list) else str(v).lower()
                      if isinstance(v, bool) else v)
                  for k, v in query.items() if k not in ("limit", "offset")}
        return f"https://{self.host}/results?{urlencode(params)}"

    def doc_url(self, doc_id: Any) -> str:
        return f"https://{self.host}/doc/{doc_id}"

    def search(self, **query: Any) -> tuple[list[dict], int, Optional[str], str]:
        """One search. Returns (documents in the site's order, total hits, snapshot_id, url)."""
        query = {"department": self.department, "limit": PAGE_SIZE, "offset": 0,
                 **query}
        cid = str(uuid.uuid4())
        url = self.results_url(query)
        reply: dict = {}
        try:
            self.ws.send(json.dumps({"type": SEARCH_MESSAGE, "authToken": self.token,
                                     "correlationId": cid, "sync": True,
                                     "payload": {"workspaceID": "ownerlookup", "query": query}}))
            while True:                       # skip anything that is not the reply
                reply = json.loads(self.ws.recv(timeout=TIMEOUT))
                if reply.get("correlationId") == cid:
                    break
        except TimeoutError as e:
            raise SourceUnavailable("no reply from the search WebSocket") from e
        except (OSError, WebSocketException, ValueError) as e:
            raise SourceUnavailable(f"WebSocket: {type(e).__name__}: {e}") from e
        finally:
            snapshot_id = save_snapshot(query, [{"url": url, "http_status": None,
                                                 "body": json.dumps(reply)}], self.source)
        payload = reply.get("payload") or {}
        data = payload.get("data")
        if "FULFILLED" not in (reply.get("type") or "") or not isinstance(data, dict):
            raise SourceUnavailable(f"search failed: {reply.get('type')} "
                                    f"{json.dumps(payload)[:200]}")
        docs = [data["byHash"][str(i)] for i in data.get("byOrder", [])
                if str(i) in data.get("byHash", {})]
        return docs, (payload.get("meta") or {}).get("numRecords", len(docs)), snapshot_id, url


# ---------------------------------------------------------------- documents

def plain(text: Optional[str]) -> str:
    """Search highlight -> text: '<em>5241</em> S' -> '5241 S', whitespace collapsed."""
    return re.sub(r"\s+", " ", re.sub(r"</?em>", "", text or "")).strip()


def parties(doc: dict, kind: str) -> list[str]:
    return [p["name"] for p in doc.get("parties") or []
            if (p.get("type") or "").lower() == kind and clean(p.get("name"))]


def names_owner(name: str, owner_keys: list[str]) -> bool:
    """An indexed party is one of the owners: 'TURNER KATELYN M' names 'TURNER KATELYN'."""
    key = entity_key(name)
    return any(key == k or key.startswith(k + " ") for k in owner_keys)


# ------------------------------------------------------------ legal descriptions

def legal_words(text: str) -> set[str]:
    """Subdivision-name words: 'Tuscany 4th Flg' -> {'TUSCANY'}."""
    return {w for w in entity_key(text).split()
            if not re.fullmatch(r"\d+(ST|ND|RD|TH)?", w) and w not in LEGAL_STOP_WORDS}


def parcel_legal(text: Optional[str]) -> Optional[dict]:
    """'Lot 40 Blk 6 Tuscany 4th Flg' -> {lot: 40, block: '6', words: {'TUSCANY'}}."""
    if not text:
        return None
    t = entity_key(text)
    lot = re.search(r"\bLOTS?\s+(\d+)", t)
    block = re.search(r"\bB(?:LK|LOCK|L)\s+(\w+)", t)
    words = legal_words(re.sub(r"\bLOTS?\s+\d+|\bB(?:LK|LOCK|L)\s+\w+", " ", t))
    return {"lot": int(lot[1]) if lot else None, "block": block[1] if block else None,
            "words": words} if words else None


def property_of(doc: dict, parcel: Optional[dict]) -> str:
    """Whether a document's indexed legal is this parcel: 'this parcel', 'another
    property' (a subdivision legal that differs) or 'unknown' (none indexed, or a
    legal this code does not compare, e.g. metes and bounds)."""
    subs = [lg for lg in doc.get("legals") or [] if (lg.get("legalType") or "").lower()
            == "subdivision" and lg.get("subdivName")]
    if not parcel or not subs:
        return "unknown"
    for lg in subs:
        lo, hi = lg.get("subdivLowLot"), lg.get("subdivHighLot") or lg.get("subdivLowLot")
        lot_ok = (parcel["lot"] is None or lo is None
                  or (str(lo).isdigit() and str(hi).isdigit()
                      and int(lo) <= parcel["lot"] <= int(hi)))
        block_ok = (parcel["block"] is None or not lg.get("subdivBlock")
                    or entity_key(str(lg["subdivBlock"])).lstrip("0") == parcel["block"].lstrip("0"))
        if lot_ok and block_ok and legal_words(lg["subdivName"]) & parcel["words"]:
            return "this parcel"
    return "another property"


def document(doc: dict, owner_keys: list[str], parcel: Optional[dict], rec: Recorder) -> dict:
    grantors, grantees = parties(doc, "grantor"), parties(doc, "grantee")
    excerpt = next(iter((doc.get("highlights") or {}).get("ocrText") or []), None) \
        or doc.get("ocrText")
    return {
        "instrument": clean(doc.get("instrumentNumber")),
        "recorded": iso_date(doc.get("recordedDate")),
        "doc_type": plain(doc.get("docType")) or None,
        "doc_type_code": clean(doc.get("docTypeCode")),
        "doc_group": clean(doc.get("docGroup")),
        "grantors": grantors,
        "grantees": grantees,
        "legal": [plain(x) for x in doc.get("legalDescription") or [] if plain(x)],
        "property": property_of(doc, parcel),
        "consideration": parse_money(doc.get("consideration")),
        "matched_on": ("index" if any(names_owner(n, owner_keys) for n in grantors + grantees)
                       else "text"),
        "excerpt": plain(excerpt) or None,
        "record_url": rec.doc_url(doc.get("id")),
    }


def same_name(a: str, b: str) -> bool:
    """'STATTER EDWARD F' (index order) = 'Edward F. Statter' (as written)."""
    x, y = entity_key(a).split(), entity_key(b).split()
    return bool(x) and sorted(x) == sorted(y)


# ------------------------------------------------------ statement of authority

def fragment(docs: list[dict], doc_id: Any, anchor: str) -> Optional[str]:
    """The scanned-text excerpt of one document that contains `anchor`."""
    doc = next((d for d in docs if d.get("id") == doc_id), None)
    for text in ((doc or {}).get("highlights") or {}).get("ocrText") or []:
        if anchor.lower() in plain(text).lower():
            return plain(text)
    return None


def read_authorized(rec: Recorder, doc: dict) -> tuple[Optional[str], int]:
    """Section 5 of a Statement of Authority: who may act for the entity.

    Searches one form phrase in the scanned text, limited to statements recorded
    the same day; the excerpt shows ~200 characters, so each next search is the
    phrase plus everything read so far, which moves the excerpt forward. Stops at
    the form's next item ("6. The authority ...").

    Returns (text, searches made); text is None when nothing could be read, and
    ends with "..." when it was cut before the next item.
    """
    day = (iso_date(doc.get("recordedDate")) or "").replace("-", "")
    scope = dict(searchType="quickSearch", searchOcrText=True, _docTypes=rec.sta_types,
                 recordedDateRange=f"{day},{day}")
    auth = rec.authority or {}
    end = re.compile(auth.get("end", r"$^"), re.I)
    searches = 0
    for anchor in auth.get("anchors", []):
        read: Optional[str] = None
        for _ in range(auth.get("max_steps", 8)):
            docs, _, _, _ = rec.search(searchValue=f"{anchor} {read}" if read else anchor, **scope)
            searches += 1
            text = fragment(docs, doc.get("id"), anchor)
            if text is None:
                break
            start = text.lower().index(anchor.lower()) + len(anchor)
            tail = text[start:].lstrip(" :;,-")
            stop = end.search(tail)
            if stop:
                return tail[:stop.start()].strip(" ,;."), searches
            if read is not None and len(tail) <= len(read):
                break                         # the excerpt stopped moving
            read = tail
        if read:
            return read.rstrip(" ,;") + " ...", searches
    return None, searches


def people(text: Optional[str]) -> Optional[list[dict]]:
    """'Edward F. Statter, Sole Member' -> [{name, position}]; None when it is not
    one 'name, position' (several people, or text cut short)."""
    if not text or text.endswith("...") or re.search(r";|\band\b", text, re.I):
        return None
    m = re.fullmatch(r"(?P<name>.+?)(?:\s*,\s*|\s+as\s+)(?P<position>.+)", text)
    return [{"name": m["name"].strip(), "position": m["position"].strip()}] if m else None


# ------------------------------------------------------------------- lookup

def search_url(cfg: dict, term: str) -> str:
    """The site's search page for a reception number or a name, for a person to open."""
    return (f"https://{cfg['host']}/results?" + urlencode(
        {"department": cfg.get("department", "RP"), "searchType": "quickSearch",
         "searchValue": term}))


def run(cfg: dict, ctx: Context) -> None:
    """Steps 1-3 for the owner of record; failures mark only these fields.

    The Assessor's legal description tells this parcel's documents from other
    property of a person with the same name.
    """
    owner, reception = ctx.value("owner"), ctx.value("last_sale_reception")
    if not owner:
        ctx.add([Observation(field=f, status="not_provided", source=cfg["source"],
                             url=f"https://{cfg['host']}/", note="no owner of record to search")
                 for f in FIELDS])
        return
    try:
        rec = Recorder(ctx.client, cfg, getattr(ctx.state, "AUTHORITY", None))
    except SourceUnavailable as e:
        ctx.add(unavailable(cfg, f"Clerk & Recorder: {e}", reception))
        return
    try:
        ctx.add(_recorder_observations(rec, owner, reception,
                                       parcel_legal(ctx.value("legal_description"))))
    except SourceUnavailable as e:
        ctx.add(unavailable(cfg, f"Clerk & Recorder search: {e}", reception))
    finally:
        rec.close()


def is_deed(doc: dict, deed_group: str) -> bool:
    """A conveyance: the county's deed group, or a type like WARRANTY DEED (not a deed
    of trust or a release). Counties group types differently (Deed, DTT, ...)."""
    kind = doc.get("doc_type") or ""
    return doc.get("doc_group") == deed_group or bool(
        re.search(r"\bDEED\b", kind, re.I) and not re.search(r"TRUST|RELEASE", kind, re.I))


def acquisition_deed(docs: list[dict], owner_keys: list[str], reception: Optional[str],
                     reception_found: bool, reception_url: Optional[str],
                     common: dict, deed_group: str) -> Observation:
    """The deed by the Assessor's last-sale reception; without one, the newest deed
    to the owner. A reception number the Clerk & Recorder cannot find is a failed
    consistency check, never not_found: the deed exists."""
    field = "owner_acquisition_deed"
    if reception:
        at = {**common, "url": reception_url or common["url"]}
        deed = next((d for d in docs if d["instrument"] == reception), None)
        if deed is None or not reception_found:
            return Observation(field=field, status="check_failed", **at,
                               note=f"CHECK FAILED: the Assessor's last sale is reception "
                                    f"{reception}, but the Clerk & Recorder returned no document "
                                    f"with that number. The deed exists, so this is a search or "
                                    f"data error, not a missing record")
        note = f"{ACQUISITION_NOTE}; found by the Assessor's last-sale reception {reception}"
        if not any(names_owner(n, owner_keys) for n in deed["grantees"]):
            note += (f". Its grantees ({', '.join(deed['grantees'])}) are not the owner of "
                     f"record: the parcel may have changed hands without a sale the Assessor "
                     f"lists")
        return Observation(field=field, value=deed, note=note, record_url=deed["record_url"], **at)
    deeds = [d for d in docs if is_deed(d, deed_group)
             and any(names_owner(n, owner_keys) for n in d["grantees"])]
    if deeds:
        return Observation(field=field, value=deeds[0], record_url=deeds[0]["record_url"],
                           note=f"{ACQUISITION_NOTE}; the newest deed to the owner (the "
                                f"Assessor lists no last-sale reception)", **common)
    return Observation(field=field, status="not_found", **common,
                       note="no deed to the owner, and the Assessor lists no last-sale reception")


def unavailable(cfg: dict, note: str, reception: Optional[str]) -> list[Observation]:
    """Never not_found: the site could not be searched, so nothing is known."""
    if reception:
        note += f"; the Assessor's last sale is reception {reception}, so a deed exists"
    return [Observation(field=f, status="source_unavailable", source=cfg["source"],
                        url=f"https://{cfg['host']}/", note=note) for f in FIELDS]


def _recorder_observations(rec: Recorder, owner: str, reception: Optional[str],
                           parcel: Optional[dict]) -> list[Observation]:
    split = split_owner(owner)
    keys = [entity_key(p["as_recorded"]) for p in split["parties"]]
    is_entity = split["type"] in ("entity", "trust")
    found: dict[str, dict] = {}
    searched, capped = [], []

    # 1. the Assessor's last-sale reception, looked up directly: the surest way to the deed
    reception_doc = None
    reception_url = None
    if reception:
        hits, _, sid, reception_url = rec.search(searchType="quickSearch", searchValue=reception)
        reception_doc = next((d for d in hits if clean(d.get("instrumentNumber")) == reception),
                             None)
        searched.append(f"reception {reception}")
    # each party on its own: the index has one entry per party, not the combined string.
    # Scanned text is searched for business names only (a person's name matches too much)
    first_url, first_sid = None, None
    for party in split["parties"]:
        hits, total, sid, url = rec.search(searchType="quickSearch",
                                           searchValue=party["as_recorded"],
                                           **({"searchOcrText": True} if is_entity else {}))
        first_url, first_sid = first_url or url, first_sid or sid
        searched.append(repr(party["as_recorded"]))
        if total > len(hits):
            capped.append(f"only the first {len(hits)} of {total} for {party['as_recorded']!r}")
        for d in hits:
            found.setdefault(clean(d.get("instrumentNumber")), d)
    if reception_doc is not None:
        found.setdefault(reception, reception_doc)

    common = dict(source=rec.source, url=first_url or reception_url or rec.results_url({}),
                  snapshot_id=first_sid)
    docs = sorted((document(d, keys, parcel, rec) for d in found.values()),
                  key=lambda d: d["recorded"] or "", reverse=True)
    for d in docs:
        if d["instrument"] == reception and d["matched_on"] == "text":
            d["matched_on"] = "reception"
    # a person's name also finds other people's documents: drop those for another
    # property; with several owners, one with no comparable legal (e.g. a release)
    # must name every owner ('TURNER JASON A' with 'TURNER VERONICA' is someone else)
    def all_named(d: dict) -> bool:
        names = d["grantors"] + d["grantees"]
        return all(any(names_owner(n, [k]) for n in names) for k in keys)
    other = [] if is_entity else [d for d in docs if d["instrument"] != reception
                                  and d["property"] == "another property"]
    unsure = [] if is_entity or len(keys) < 2 else [
        d for d in docs if d["instrument"] != reception and d["property"] == "unknown"
        and not all_named(d)]
    docs = [d for d in docs if d not in other and d not in unsure]
    by_id = {clean(d.get("instrumentNumber")): d for d in found.values()}
    extra = [f"searched: {', '.join(searched)}"] + capped
    if other:
        extra.append(f"{len(other)} documents of the same name(s) for another property left "
                     f"out ({', '.join(d['instrument'] for d in other)})")
    if unsure:
        extra.append(f"{len(unsure)} documents left out that name only some of the owners and "
                     f"have no legal description to compare, so may be someone else's "
                     f"({', '.join(d['instrument'] for d in unsure)})")
    obs = [Observation(field="recorded_documents", value=docs,
                       note=f"{RECORDED_DOCS_NOTE}. {'; '.join(extra)}", **common)
           if docs else
           Observation(field="recorded_documents", status="not_found", **common,
                       note=f"no recorded document names {owner!r}. {'; '.join(extra)}")]

    # 2. the deed to the owner
    obs.append(acquisition_deed(docs, keys, reception, reception_doc is not None,
                                reception_url, common, rec.deed_group))
    deed = obs[-1].value if obs[-1].status == "ok" else None

    # 3. Statements of Authority recorded by the owner, newest first
    if rec.authority is None:
        obs.append(Observation(field="statement_of_authority", status="not_provided", **common,
                               note="this state has no recorded document naming who may act "
                                    "for an entity (not configured in its state.py)"))
        return obs
    sta_note_base = rec.authority.get("note", "recorded statement of who may act for the owner")
    if not is_entity:
        obs.append(Observation(field="statement_of_authority", status="not_provided", **common,
                               note="the owners are individuals; a Statement of Authority is "
                                    "recorded for an entity"))
        return obs
    statements = [d for d in docs if d["doc_type_code"] in rec.sta_types
                  and any(names_owner(n, keys) for n in d["grantors"])]
    if not statements:
        obs.append(Observation(field="statement_of_authority", status="not_found", **common,
                               note=f"no Statement of Authority recorded by {owner!r}. "
                                    + rec.authority.get("missing_note", "")))
    for sta in statements:
        try:
            text, searches = read_authorized(rec, by_id[sta["instrument"]])
        except SourceUnavailable as e:
            text, searches = None, 0
            sta_note = f"{sta_note_base}. Could not read the names: {e}"
        else:
            sta_note = sta_note_base + (f"; read with {searches} searches of the scanned text"
                                   if text else "; the names could not be read from the "
                                   "scanned text, open the document")
        persons = people(text)
        value = {**{k: sta[k] for k in ("instrument", "recorded", "doc_type", "grantors",
                                        "record_url")},
                 "authorized": text, "persons": persons}
        grantors = deed["grantors"] if deed else []
        for p in persons or []:
            match = next((g for g in grantors if same_name(g, p["name"])), None)
            if match:
                sta_note += (f". {p['name']} has the same name as the grantor ({match}) of the "
                             f"deed that conveyed the parcel to the owner ({deed['instrument']}, "
                             f"{deed['recorded']})")
        obs.append(Observation(field="statement_of_authority", value=value, note=sta_note,
                               record_url=sta["record_url"], **common))
    return obs
