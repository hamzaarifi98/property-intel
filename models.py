"""Data model shared by every source in the Property Intelligence Tool.

Every source returns Observations, never bare values. An Observation records
what was found, where, when, and points at the raw response it came from
(snapshot_id, kept only when runs use SAVE_RAW=1), so any value in the final
profile can be traced to its source.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

# ok                  value found
# not_found           the source was reached and has no value for this field
# not_provided        the source has the field, but the county left it blank
# source_unavailable  the source could not be reached (403, timeout, error)
# check_failed        the sources contradict each other in a way that should not happen
#                     (e.g. no deed found for a known reception number): a bug or bad
#                     data, never a gap; the note says what disagreed
ObsStatus = Literal["ok", "not_found", "not_provided", "source_unavailable", "check_failed"]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Observation(BaseModel):
    field: str
    value: Any = None
    source: str
    url: str                                   # request that produced the value (reproducible)
    retrieved_at: datetime = Field(default_factory=utcnow)
    snapshot_id: Optional[str] = None          # raw response in results/raw/<id>.json (SAVE_RAW=1)
    status: ObsStatus = "ok"
    as_of: Optional[str] = None                # when the source's data was current, if known
    record_url: Optional[str] = None           # link to the official record page, if known
    note: Optional[str] = None                 # anything an analyst should know about this value


class ParcelCandidate(BaseModel):
    parcel_id: Optional[str] = None
    account: Optional[str] = None
    county: Optional[str] = None
    site_address: Optional[str] = None
    owner: Optional[str] = None
    score: Optional[float] = None              # address-match score (address searches only)
    match_reasons: list[str] = []
    observations: list[Observation] = []


ResolveStatus = Literal["match", "candidates", "not_found", "source_unavailable"]


class ResolveResult(BaseModel):
    input: str
    input_type: Literal["apn", "address"]
    status: ResolveStatus
    parcel: Optional[ParcelCandidate] = None   # set only when status == "match"
    candidates: list[ParcelCandidate] = []     # set when status == "candidates"
    queries_tried: list[dict] = []             # every query sent, for the "what was tried" trail
    notes: list[str] = []


# ambiguous: the source has several records for the id; none is picked
SourceStatus = Literal["ok", "not_found", "ambiguous", "source_unavailable"]


class SourceRecord(BaseModel):
    """One source's answer for one parcel, e.g. a county assessor's parcel page."""
    source: str
    input: str
    status: SourceStatus
    url: Optional[str] = None                  # request that was made
    snapshot_id: Optional[str] = None          # raw response in results/raw/<id>.json (SAVE_RAW=1)
    observations: list[Observation] = []
    notes: list[str] = []
