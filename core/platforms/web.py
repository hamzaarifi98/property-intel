"""Helpers for platforms that read HTML pages."""
from __future__ import annotations

import re
from typing import Any, Callable, Optional

import httpx
from bs4 import BeautifulSoup

from core.models import Observation
from core.common import SourceUnavailable, clean, save_snapshot

# Several county servers answer bare clients with 403; they serve browser-like requests.
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/129.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def fetch_text(url: str, params: dict, source: str,
               client: httpx.Client) -> tuple[str, Optional[str], str]:
    """GET one page with browser-like headers. Returns (text, snapshot_id, request_url)."""
    pages: list[dict] = []
    try:
        try:
            r = client.get(url, params=params, headers=HEADERS, follow_redirects=True)
        except httpx.HTTPError as e:
            raise SourceUnavailable(f"{type(e).__name__}: {e}") from e
        pages.append({"url": str(r.request.url), "http_status": r.status_code, "body": r.text})
        if r.status_code != 200:
            raise SourceUnavailable(f"HTTP {r.status_code}")
    finally:
        snapshot_id = save_snapshot(params, pages, source)
    return r.text, snapshot_id, str(r.request.url)


def label(soup: BeautifulSoup, id_: str, sep: str = " ") -> Optional[str]:
    el = soup.find(id=id_)
    return clean(el.get_text(sep, strip=True)) if el else None


def to_number(value: Optional[str]) -> Any:
    """'4.00' -> 4.0, '1907' -> 1907; anything else is returned unchanged."""
    if value is None:
        return None
    s = value.replace(",", "")
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    if re.fullmatch(r"-?\d*\.\d+", s):
        return float(s)
    return value


def snake(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def year_in(text: Optional[str]) -> Optional[str]:
    m = re.search(r"\b(19|20)\d{2}\b", text or "")
    return m.group(0) if m else None


def adder(obs: list[Observation], common: dict, blank_note: str) -> Callable[..., None]:
    """add(field, value, note, as_of): ok when the value is set, otherwise not_provided."""
    def add(field: str, value: Any, note: Optional[str] = None, as_of: Optional[str] = None) -> None:
        if value is None or value == []:
            obs.append(Observation(field=field, status="not_provided", note=note or blank_note,
                                   as_of=as_of, **common))
        else:
            obs.append(Observation(field=field, value=value, note=note, as_of=as_of, **common))
    return add


def form_state(soup: BeautifulSoup) -> dict:
    """An ASP.NET form's fields as the browser would post them (no buttons)."""
    data = {i["name"]: i.get("value", "") for i in soup.select("input[name]")
            if i.get("type") not in ("submit", "button", "image", "checkbox")}
    for s in soup.select("select[name]"):
        opt = s.find("option", selected=True) or s.find("option")
        data[s["name"]] = opt.get("value", "") if opt else ""
    return data
