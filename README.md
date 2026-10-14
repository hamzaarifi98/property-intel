# Property Intelligence Tool — Step 1: parcel resolver (Colorado)

Turns an address or APN into a Colorado parcel using the statewide
**Colorado Public Parcels** layer (OIT GIS, ArcGIS REST, no API key).
Every value comes back as an `Observation` with its source, request URL,
retrieval time, the county's "data received" date and a link to the county
record.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Run

```bash
python resolve.py "5241 S Rio Grande St, Littleton, CO 80120"
python resolve.py "99999 S Rio Grande St, Littleton, CO 80120"   # expect not_found
python resolve.py "<parcel_id from the first run>"                # expect the same parcel
```

`status` is one of:

| status | meaning |
|---|---|
| `match` | one parcel, in `parcel` |
| `candidates` | several equally good parcels (condo units, duplicate APNs across counties), in `candidates` |
| `not_found` | the source answered and has no such parcel; see `queries_tried` |
| `source_unavailable` | the source could not be reached (403, timeout, ArcGIS error). Never reported as not found |

## Tests

```bash
pytest                    # 15 offline tests against a fake ArcGIS server
LIVE=1 pytest -m live -s  # the three real checks against gis.colorado.gov
```

## Design decisions (defend these in the review)

- **No geocoder.** The address is matched as text against the layer's site
  address (`situsAdd`): a loose `LIKE '<number> %<STREET NAME>%'` query, then
  strict scoring on direction, street type, unit, ZIP and city. Lat/long is the
  centroid of the parcel polygon, not a geocoded point.
- **Ambiguity is returned, not resolved by guessing.** Equal-scoring parcels
  (typically condo units) come back as `candidates`. A unit in the input picks
  that unit. A wrong direction (N vs S) costs more than a matching ZIP earns.
- **APN search checks both `parcel_id` and `account`**, then retries with the
  separators removed. Parcel numbers are not unique statewide, so duplicates
  across counties come back as candidates.
- **Blank ≠ not found.** A field the county left blank is `not_provided`
  (with the county's sharing preference if set); `not_found` is reserved for
  "the source has nothing".
- **Data checks:** money fields are text in this layer and are parsed to
  numbers; a `$0` sale is flagged as not a market sale; `owner` is labelled as
  the surface owner (Colorado separates surface and mineral rights).
- **ArcGIS returns query errors with HTTP 200** and an `"error"` key; this is
  treated as `source_unavailable`. Pagination follows `exceededTransferLimit`.
- **Raw responses are opt-in.** With `SAVE_RAW=1` every raw response is saved
  to `results/raw/<snapshot_id>.json` before parsing, so any value can be traced
  to it; otherwise `snapshot_id` is `null` and nothing but the run's result is written.
- **Known limitation:** the statewide layer is refreshed yearly. Owner, values
  and sales must be confirmed against the county assessor (next step: the
  Arapahoe adapter). Every value carries `as_of` so the age is visible.

## Step 2: Arapahoe County Assessor adapter

```bash
python -m core.colorado.arapahoe.arapahoe 2077-16-2-22-004               # AIN
python -m core.colorado.arapahoe.arapahoe 034472207                      # PIN
python -m core.colorado.arapahoe.arapahoe "5241 S Rio Grande St, Littleton, CO 80120"   # resolves statewide first
python -m core.colorado.arapahoe.arapahoe 2077-16-2-22-004 --no-detail   # JSON only, no HTML
```

Each run prints the result and saves it to one file,
`results/arapahoe/<your input> - HH-MM - DD-MM-YY.json` (local time), e.g.
`results/arapahoe/5241 S Rio Grande St, Littleton, CO 80120 - 15-47 - 13-10-26.json`.
A second run in the same minute gets ` (2)` instead of overwriting.

Returns a `SourceRecord` (`ok` / `not_found` / `ambiguous` / `source_unavailable`).
Observations use the statewide field names where they overlap (`owner`,
`assessed_total`, `last_sale_price`, ...), and each one names which of the two
sources it came from:

1. **County GIS layer (primary, JSON).** `gis.arapahoegov.com/arcgis/rest/services/InfoService/MapServer/12`,
   the county's own ArcGIS parcel layer (~234k parcels, used by its ArapaMAP viewer).
   Owner, mailing address, appraised/assessed/taxable values, last sale, PIN, location.
   It decides `ok` / `not_found` / `ambiguous`, so a lookup works even when the HTML page doesn't.
2. **Assessor parcel page (detail, HTML).** `parcelsearch.arapahoegov.com/PPINum.aspx?PPINum=<AIN>`.
   Only what the layer lacks: sales history with deed links, buildings, areas,
   land lines, assessed building/land split, school value, mill levy, tax year,
   legal description. There is no API behind the page; values are read by its
   ASP.NET label ids. Derived: `living_area_sqft` (floors above grade; basement,
   garage, patio and porches excluded), `basement_sqft` (apart), and `land_sqft`
   (the land lines in SF, else acres x 43,560 with the rounding stated). `land_sqft`
   is primary; the note compares the GIS polygon (`gis_area_sqft`), which is secondary.
3. **Value history (JSON).** The Assessor's value-change layers on the county GIS
   (`CustomCAMA_WM/MapServer/75, 76, 78`): `appraised_total_history`, one
   observation per tax year (2016-2023) with the year in `as_of`, plus the current
   value under the page's tax year. Colorado reappraises in odd years, so an even
   year repeats the year before.
4. **Treasurer (HTML).** `taxsearch.arapahoegov.com/ReReport.aspx?PIN=<PIN>` and
   `PrevYear.aspx`: `tax_year`, `tax_rate`, `tax_billed`, `tax_paid`, `tax_due`,
   exemptions/special assessments/fees/interest, the Y/N flags (`tax_prior_years_due`,
   `tax_lien`, `tax_bankruptcy`, `tax_treasurers_deed`, ...), `tax_bill_pdf_url`,
   and the prior year's billed/paid tax.
   **Tax check** (`taxcheck.py`): `tax_levies`, the levy per taxing authority (the
   Assessor's `Levy.aspx`, linked from the mill levy), and `tax_check`. Colorado
   taxes school levies on a separate school assessed value, so the check is
   school value x school mills + taxable value x the other mills, against
   `tax_billed`. When the Treasurer page's values don't give the bill, the tax
   statement PDF (tax per authority) shows the values the bill used, and the
   difference is flagged; e.g. the page shows the 2026 assessed value next to the
   2025 bill. Not reproduced either way: `check_failed` with both numbers. Billed
   tax plus special assessments, fees and interest is also checked against paid + due.
5. **Location (JSON).** Point queries at the county's point inside the parcel:
   `jurisdiction` (InfoService/11; its note says when the postal city is not the
   jurisdiction, e.g. an Aurora address in unincorporated county), FEMA `flood_zone`/`flood_sfha`/`flood_panel`
   (ArapaMAP/380, 386), and zoning from the City of Littleton's GIS
   (`ltngiswa.littletonco.gov`, LittletonParcelZoning/2) or the county's zoning
   layer for unincorporated land (InfoService/60). `flood_sfha_on_parcel` sends
   the parcel polygon (by POST) to catch high-risk flood areas on part of the lot.
6. **Owner (derived) and owner entity (JSON).** `owners`: the owner of record split
   into parties (`owners.py`: on `,`, `&`, AND; people from LAST FIRST to First Last;
   a business or trust name is never split), with the type (`individuals`, `entity`,
   ...) and the Assessor's tenancy (e.g. Joint Tenancy). `owner_absentee`: false
   when the mailing address is the site address. When the owner is a business, the
   statewide Secretary of State layer below (Step 3).
7. **Clerk & Recorder (WebSocket).** `arapahoe.co.publicsearch.us` has no REST API:
   its page sends each search as a JSON message over `wss://…/ws`, with a token the
   page embeds and its session cookie; `recorder.py` does the same.
   It looks up the Assessor's last-sale reception number first, then each owner
   party on its own (the index has one entry per party; a combined string like
   `TURNER JASON, TURNER KATELYN` finds nothing). A person's name also finds other
   people's documents, so each document's indexed legal description is compared
   with the parcel's (`property`: this parcel / another property / unknown);
   another property's are left out, and with several owners an `unknown` one must
   name all of them. `recorded_documents`, `owner_acquisition_deed` (by reception
   number; a reception the Clerk can't find is `check_failed`, never `not_found`),
   and `statement_of_authority` (entity owners only). The deed's recording date
   labels the two sale dates: the GIS `last_sale_date` is the recording date, the
   Assessor's `sales[].date` its sale date, with `recorded` added to that sale.
   The index lists only the entity on a Statement of Authority, so the
   names are read from the scanned text: the search returns ~200 characters around
   a match, so it searches the form's wording ("on behalf of the entity is"),
   limited to statements recorded that day, then that wording plus what it has read,
   until the form's next item. The note says it is OCR and names a matching deed
   grantor.
8. **Permits (HTML), from whoever issues them.** Littleton: the City's eTRAKiT
   (`permit9.littletonco.gov`), every permit at the site address with dates, scope,
   APN and last inspection. Unincorporated county: Arapahoe County Public Works &
   Development's Accela portal (`aca-prod.accela.com/arapahoe`, `permits.py`); its
   module searches and record pages need a login, so the site-wide search's list is
   read (date, number, project, type, status). Neither shows contacts to the public,
   so no contractor is named. Other cities: `not_provided`, naming the city.
9. **Site entities (JSON).** `site_entities`: Secretary of State entities whose
   principal address is the site address (possible occupants).
10. **Contact routes.** `contact_routes`, one per route found, each with its `role`,
    `basis` and the source of its main evidence: `owner` (the people and tenancy,
    or the person in an entity's Statement of Authority; `absentee`),
    `registered_agent`, `contractor` and `occupant` (an active site entity, plus any
    permit naming it). A role with no one found has no route. The owner of the
    entity's principal-address parcel is never a route.

`--no-detail` skips steps 2-10.

- **Each step fails on its own.** A step that can't run marks only its own
  fields `source_unavailable`, with the reason.
- **Contradictions fail loudly.** `check_failed` marks an observation whose sources
  disagree in a way that should not happen (a known reception number with no deed,
  a bill that the values and levies don't reproduce); the record's `notes` repeat it.
  It is never reported as `not_found`.
- **Zoning and permits for other cities are not queried yet** (`not_provided`,
  naming the city). Littleton and unincorporated land are covered.
- **Colorado does not list LLC members**, so the person behind an LLC comes from
  its Statement of Authority (step 7), never from an address.

- **A failed detail page doesn't fail the lookup.** Its fields come back as
  `source_unavailable` (blocked, down or changed layout) or `not_found`, with the reason.
- **Browser-like headers for the page.** It has returned 403 to bare clients.
- **Hidden rows are skipped.** The page hides inapplicable building fields
  (e.g. bedrooms on a warehouse) with `display:none`.
- **Known gaps:** the GIS layer has no "data as of" date (its values matched the
  page when checked). Its sale date can differ from the page's for the same
  reception number (e.g. 2019-06-19 vs 2019-06-17); both are kept and labelled
  against the Clerk & Recorder's recording date (step 7).
- `arapahoeco.gov` (the county website) has no data API; the property data lives on `*.arapahoegov.com`.

## Step 3: Colorado Secretary of State open-data layer

```bash
python -m core.colorado.sos.sos "5241 S RIO GRANDE STREET LLC"
```

Statewide, so any county adapter can call `core.colorado.sos.sos.lookup(owner, client)`
(Arapahoe's step 6 does). All data comes from the Secretary of State's datasets on
`data.colorado.gov` (Socrata, refreshed daily, no key; `SOCRATA_APP_TOKEN` raises the
rate limit). Each run saves `results/sos/<name> - HH-MM - DD-MM-YY.json`.

| field | dataset | what |
|---|---|---|
| `owner_entity` | Business Entities (`4ykn-tg5h`) | status, type, formed, principal/mailing address, registered agent, SOS link |
| `entity_filings`, `entity_last_report` | Transaction History (`casm-dbbj`) | every filing, newest first; date of the last periodic report |
| `entity_trade_names` | Trade Names (`u7sb-g482`) | names registered to do business under; the dataset's own-name row ("Entity Type") is left out |
| `entity_ucc_filings` | UCC Debtor / Filing / Collateral (`8upq-58vz`, `wffy-3uut`, `4am6-w6u4`) | secured-debt filings naming the entity: filed, lapse date, terminated, `in_effect`, collateral |
| `entity_related` | Business Entities | other entities at the same principal address (street number, street name, ZIP), with `same_agent` |
| `entity_address_owner` | statewide parcels (`resolve.by_address`, source: Colorado Public Parcels) | owner of the parcel at the principal address; corroboration only |
| `site_entities` | Business Entities | `site_entities(address, zip)`: entities whose principal address is a parcel's site address (called by the county adapter) |

- **Name matching:** punctuation is dropped (`5241 S. Rio Grande Street, LLC` =
  `5241 S RIO GRANDE STREET LLC`). Several entities with one name → `ambiguous`,
  none picked, and the other fields are not looked up. A name without
  LLC/INC/CORP/... is not searched. UCC debtors are matched without the suffix
  (filings often write `STATTER RANCH` for STATTER RANCH, INC.) and each filing
  shows the debtor name as filed.
- **The person behind an LLC is not in these datasets.** `entity_address_owner`
  and `entity_related` are leads, not proof; the record of who can act is the
  Statement of Authority at the county Clerk & Recorder. Both are skipped when the
  principal address is the registered agent's office, a PO box, or outside Colorado.
  The address owner is never a contact: when it shares a surname with the person in
  the Statement of Authority, the county adapter notes that as corroboration of the
  address, and infers no relationship.
- **Agent names are normalized before matching** (punctuation, `&`/AND, P.C./LLC/…).
  A firm that adds a name partner keeps its first words, so a name that starts with
  all the words (3 or more) of the other is the same firm: `Folkestad Fazekas Barrick
  & Patoile, P.C.` = `Folkestad Fazekas Barrick Patoile & James, P.C.`.
- **Filing gaps are a note, not a flag:** a calendar year with no periodic report
  between two reports (2021-12-21, then 2023-01-25) is noted on `entity_filings`.
- **Dates are ISO** (`2026-04-21`), including the statewide layer's `as_of`, which
  the layer gives as `4/21/2026`.
- **UCC is not mortgages.** UCC filings cover personal property and fixtures;
  deeds of trust are recorded with the county.
- The SOS website itself (`sos.state.co.us`) is behind a Cloudflare bot check, so
  it is only linked (`record_url`), not read.

## Files

```
models.py                           Observation, ParcelCandidate, ResolveResult, SourceRecord (Pydantic)
resolve.py                          query_layer, by_apn, by_address, resolve + CLI
core/colorado/arapahoe/arapahoe.py  Arapahoe adapter: lookup(ain_or_pin) = county GIS JSON + parcel
                                    page, value history, Treasurer, zoning/flood, SOS entity; CLI
core/colorado/arapahoe/settings.py  Arapahoe constants: URLs, layer ids, field maps, notes
owners.py                           split an owner of record into parties (people, entity, trust)
core/colorado/arapahoe/recorder.py  Clerk & Recorder over its WebSocket: documents, deed, Statement of Authority
core/colorado/arapahoe/taxcheck.py  levies per authority and the tax check (Levy.aspx, tax statement PDF)
core/colorado/arapahoe/permits.py   Arapahoe County (unincorporated) permits, Accela site-wide search
core/colorado/arapahoe/contacts.py  contact_routes and the address-owner corroboration note (no fetching)
core/colorado/littleton/permits.py  City of Littleton eTRAKiT permits by site address
core/colorado/littleton/settings.py Littleton constants: form controls, label ids, notes
core/colorado/sos/sos.py            statewide Secretary of State open-data layer: lookup(name); CLI
core/colorado/sos/settings.py       SOS constants: dataset ids, name matching, notes
tests/test_resolve.py               offline tests (mock server) + live tests (LIVE=1)
results/arapahoe/                   one result file per Arapahoe run
results/sos/                        one result file per SOS run
results/raw/                        raw responses, only with SAVE_RAW=1
```
