# OwnerLookup: property intelligence by state

Turns an address or parcel id into a parcel record: owner, values, taxes, recorded
documents, zoning, flood, permits and who to contact, with every value an
`Observation` that carries its source, request URL, retrieval time and a link to
the official record.

Each state lives in its own folder (`states/<state>/`); a county is a config file
in that folder, not code. Shared code (the lookup, vendor platforms used in many
states, derived facts) lives in `core/`.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Run

```bash
python -m core.lookup "18949 E Garden Dr, Aurora, CO 80015"              # the state's resolver finds the county
python -m core.lookup "205 S Galena St #4, Aspen, CO 81611" --no-detail   # parcel record only
python -m core.lookup 2077-16-2-22-004 --county Arapahoe                  # a county id
python -m core.lookup <parcel id> --state texas --county <Name>           # another state
python -m core.config                                                     # check Colorado's configs; coverage
python -m core.config --state texas
```

`--state` defaults to `colorado`. Each run prints the result and saves it to
`results/<state>/<county>/<your input> - HH-MM - DD-MM-YY.json` (local time; a
second run in the same minute gets ` (2)`). It returns a `SourceRecord`
(`ok` / `not_found` / `ambiguous` / `source_unavailable`) of `Observation`s.

## Layout

```
core/                         shared by every state
  lookup.py                   the lookup steps, in order; the CLI
  config.py                   loads and checks a state's configs; python -m core.config
  state.py                    loads a state and describes what its state.py provides
  context.py                  what the steps share: client, configs, state, observations
  common.py                   HTTP/ArcGIS queries, snapshots, result files, parsing, addresses
  models.py                   Observation, ParcelCandidate, ResolveResult, SourceRecord (Pydantic)
  names.py                    name and address keys (entity names, street keys)
  platforms/                  vendor platforms used in many states
    arcgis.py                 ArcGIS parcels, value history, point layers, flood
    publicsearch.py           GovOS (Kofile) publicsearch Clerk & Recorder (WebSocket)
    etrakit.py                eTRAKiT permits
    accela.py                 Accela Citizen Access permits
    web.py                    HTML helpers (fetch, labels, numbers, ASP.NET forms)
  derive/                     facts computed from what the platforms found (nothing fetched)
    owners.py                 split an owner of record into parties
    property.py               owners, absentee, living area, land area, sale dates, jurisdiction
    contacts.py               contact routes and the address-owner corroboration note
states/
  colorado/                   everything Colorado-specific: see states/colorado/README.md
    state.py                  what the lookup needs from Colorado
    resolve.py                statewide parcel layer: address or id -> parcel and county
    sos/                      Secretary of State open data (business owners)
    derive/taxcheck.py        Colorado's school / non-school levy tax check
    platforms/arapahoegov.py  Arapahoe County's in-house Assessor and Treasurer sites
    config/                   defaults.toml, counties/*.toml, cities/*.toml
  texas/                      skeleton: see states/texas/README.md
results/<state>/<county>/     one result file per lookup
results/raw/                  raw responses, only with SAVE_RAW=1
```

**Where code goes.** A vendor used in more than one state (ArcGIS, publicsearch,
Accela, eTRAKiT, Tyler EagleWeb, ...) is a module in `core/platforms/`. A site one
county built for itself, a state's own registry, law or tax rules go in that state's
folder.

## A state's state.py

The lookup reads these from `states/<state>/state.py` (`core/state.py`); a state
that lacks one sets it to `None` and that step is skipped:

| name | what | Colorado |
|---|---|---|
| `NAME`, `CONFIG_DIR` | display name, config folder | |
| `PLATFORMS`, `REQUIRED` | the state's own platforms and their config keys | `arapahoegov.assessor`, `arapahoegov.treasurer` |
| `resolve(text)` | address or id -> parcel and county | statewide OIT parcel layer |
| `entity(owner, client)` | business owner in the state's registry | Secretary of State open data |
| `site_entities(...)` | businesses registered at an address (occupants) | Secretary of State open data |
| `tax_check(ctx, cfg)` | does the bill follow from values and rates | school / non-school levies |
| `AUTHORITY` | recorded document naming who may act for an entity | Statement of Authority (C.R.S. 38-30-172) |

## Shared platforms

| platform | what it reads | config keys |
|---|---|---|
| `arcgis.parcels` | an ArcGIS parcel layer: one parcel by id; the field map turns their names into ours | `url`, `source`, `search_fields`, `fields`; optional `filter`, `record_url`, `record_url_field`, `point_fields`, `date_fields`, `money_fields`, `as_of_field`, `merge_rows`, `blank_note`, `notes` |
| `arcgis.value_history` | value-by-year layers | `url` (with `{layer}`), `source`, `id_field`, `layers` |
| `arcgis.point` | any layer at the parcel's point: jurisdiction, zoning | `url`, `source`, `fields` |
| `arcgis.flood` | FEMA flood zone, SFHA on the polygon, FIRM panel | `source`, `zones_url`, `panels_url`; optional `panel_link_field` |
| `publicsearch` | Clerk & Recorder: documents, the deed to the owner, the state's `AUTHORITY` document | `host`, `source`; optional `department`, `deed_group`, `sta_doc_types` |
| `etrakit` | eTRAKiT permits by address, each permit opened | `url`, `source` |
| `accela` | Accela Citizen Access site-wide search | `agency`, `source` |

## Steps

| step | config section | fields |
|---|---|---|
| 1 parcel | `[parcel]` (required) | owner, mailing address, values, last sale, site address, ids; decides ok / not_found / ambiguous |
| 2 assessor | `[assessor]` | legal, values by kind, sales, buildings, building areas, land lines, mill levy |
| 3 value history | `[value_history]` | `appraised_total_history`, one per tax year |
| 4 treasurer | `[treasurer]` | tax year, rate, billed/paid/due, flags, prior year; then the state's `tax_check` |
| 5 jurisdiction | `[jurisdiction]`, then `[jurisdictions.<NAME>]` | `jurisdiction`; zoning from that jurisdiction's `zoning` (or its city config) |
| flood | `[flood]` (state default: FEMA) | `flood_zone`, `flood_sfha`, `flood_sfha_on_parcel`, `flood_panel`, ... |
| 6 entity | `[entity]` (state default) | the state's business registry fields, for a business owner |
| 7 recorder | `[recorder]` | `recorded_documents`, `owner_acquisition_deed`, `statement_of_authority` |
| 8 permits | `[jurisdictions.<NAME>] permits` (or the city config) | `permits` |
| 9 site entities | `[entity]` | `site_entities` (possible occupants) |
| 10 contact routes | - | `contact_routes`: owner, registered agent, contractor, occupant; only routes found |

Derived (`core/derive/`, any county): `owners` (parties, type, tenancy; ET AL kept as a
flag), `owner_absentee` (a mailing address without a unit at the site's building counts
as the site), `living_area_sqft` and `basement_sqft`, `land_sqft` (Assessor's, else acres
x 43,560; compared with the GIS area), labels on the two sale dates (against the
Clerk's recording date), and a note when the postal city is not the jurisdiction.

A step the county has no section for is skipped and named in the record's notes
(`not configured for Boulder County: assessor, value_history, treasurer, jurisdiction`).

## Adding a county

1. Copy a county file in `states/<state>/config/counties/` (Colorado's `boulder.toml`
   is statewide parcels + a recorder; `arapahoe.toml` has every step) to
   `<county>.toml`, named after the county in lowercase with `_` for spaces.
2. For each step, pick the platform the county's website runs and fill its keys. An
   ArcGIS layer's field names are at `<layer url>?f=json`.
3. `python -m core.config --state <state>` checks it; `python -m core.lookup ...` runs it.

A county whose website runs a platform not built yet needs that platform once (in
`core/platforms/` if the vendor is used elsewhere, else in the state's `platforms/`),
listed in `PLATFORMS` in `core/lookup.py` and `REQUIRED` in `core/config.py` (or in
the state's `state.py`); every other county on it is then config only.

## Adding a state

Copy `states/texas/` (a skeleton: an empty `state.py`, FEMA flood in `defaults.toml`,
empty `counties/` and `cities/`) to `states/<state>/` and fill `state.py` as statewide
sources are built. Until it has a `resolve`, lookups need `--county` and a county id.

## Behaviour

- **Each step fails on its own.** A step that can't run marks only its own fields
  `source_unavailable`, with the reason.
- **Blank is not "not found".** A field the source left blank is `not_provided`;
  `not_found` means the source answered and has nothing.
- **Contradictions fail loudly.** `check_failed` marks an observation whose sources
  disagree in a way that should not happen (a known reception number with no deed, a
  bill the values and levies don't reproduce); the record's `notes` repeat it.
- **"Not found" only after a search.** A note says a document wasn't found only when
  its source was searched; when the source was down or isn't configured, it says so.
- **Clerk & Recorder search** (`publicsearch`): the Assessor's last-sale reception first,
  then each owner party on its own (the index has one entry per party). A person's
  name also finds other people's documents, so each document's indexed legal
  description is compared with the parcel's (`property`); another property's are left
  out, and with several owners an `unknown` one must name all of them. Deeds are
  told by their type (WARRANTY DEED, ...), since counties group types differently.
- **Permits** come from whoever issues them (the jurisdiction's config). eTRAKiT and
  Accela don't show permit contacts to the public, so no contractor is named.
- **ArcGIS returns query errors with HTTP 200** and an `"error"` key; this is treated
  as `source_unavailable`. Pagination follows `exceededTransferLimit`.
- **Raw responses are opt-in.** With `SAVE_RAW=1` every raw response is saved to
  `results/raw/<snapshot_id>.json`, so any value can be traced to it.
- **Browser-like headers** for HTML sites: several answer bare clients with 403. Many
  county sites also block non-US networks; run lookups from a US host.

## Tests

`tests/test_resolve.py` is empty for now; `pytest.ini` defines a `live` marker for
checks against real servers (`LIVE=1 pytest -m live -s`).
