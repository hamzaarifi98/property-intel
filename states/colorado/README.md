# Colorado

Everything Colorado-specific. Shared code is in `core/` (see the root README).

```
state.py                  what the lookup needs from Colorado (core/state.py)
resolve.py                statewide OIT parcel layer: address or id -> parcel and county
sos/                      Secretary of State open data: business owners, filings, UCC
derive/taxcheck.py        school / non-school levy tax check
platforms/arapahoegov.py  Arapahoe County's in-house Assessor and Treasurer sites
config/defaults.toml      every county: FEMA flood, Secretary of State
config/counties/          arapahoe.toml (every step), boulder.toml, pitkin.toml
config/cities/            littleton.toml (zoning: Littleton GIS; permits: eTRAKiT)
```

Configured counties: `python -m core.config`. The platform survey of all 64 counties
is `Colorado County Platform Survey.pdf` in the project root.

## Statewide parcel layer (resolve.py)

```bash
python -m states.colorado.resolve "5241 S Rio Grande St, Littleton, CO 80120"
python -m states.colorado.resolve "<parcel_id>"
```

The **Colorado Public Parcels** layer (OIT GIS, ArcGIS REST, no API key) has **44 of
64 counties**; the other 20 (Alamosa, Baca, Bent, Chaffee, Cheyenne, Conejos,
Costilla, Crowley, Delta, Fremont, Jackson, Kiowa, Kit Carson, Las Animas, Lincoln,
Mineral, Montrose, Otero, Phillips, Prowers) need their own `[parcel]` source, and an
address there can't be resolved until one exists. Counties without their own parcel
layer can use this one (`filter = "countyName = '<County>'"`; see boulder.toml).

`status` is `match` (one parcel), `candidates` (several equally good: condo units,
duplicate APNs across counties), `not_found` or `source_unavailable`.

- **No geocoder.** The address is matched as text against the site address
  (`situsAdd`): a loose `LIKE '<number> %<STREET NAME>%'` query, then strict scoring on
  direction, street type, unit, ZIP and city.
- **Ambiguity is returned, not guessed.** A unit in the input picks that unit. A wrong
  direction (N vs S) costs more than a matching ZIP earns.
- **APN search checks `parcel_id` and `account`**, then retries without separators.
- **Data checks:** money fields are text and parsed to numbers; a `$0` sale is flagged
  as not a market sale; `owner` is the surface owner (Colorado separates surface and
  mineral rights); a parcel can have one row per owner (`merge_rows`).
- **Refreshed yearly.** Every value carries the county's "data received" date in
  `as_of` (ISO).

## Secretary of State (sos/)

```bash
python -m states.colorado.sos.sos "5241 S RIO GRANDE STREET LLC"
```

The lookup's `entity` and `site_entities` steps. Data from the Secretary of State's
datasets on `data.colorado.gov` (Socrata, refreshed daily, no key;
`SOCRATA_APP_TOKEN` raises the rate limit). A CLI run saves
`results/colorado/sos/<name> - HH-MM - DD-MM-YY.json`.

| field | dataset | what |
|---|---|---|
| `owner_entity` | Business Entities (`4ykn-tg5h`) | status, type, formed, principal/mailing address, registered agent, SOS link |
| `entity_filings`, `entity_last_report` | Transaction History (`casm-dbbj`) | every filing, newest first; date of the last periodic report |
| `entity_trade_names` | Trade Names (`u7sb-g482`) | names registered to do business under; the own-name row ("Entity Type") is left out |
| `entity_ucc_filings` | UCC Debtor / Filing / Collateral (`8upq-58vz`, `wffy-3uut`, `4am6-w6u4`) | secured-debt filings naming the entity: filed, lapse date, terminated, `in_effect`, collateral |
| `entity_related` | Business Entities | other entities at the same principal address, with `same_agent` |
| `entity_address_owner` | statewide parcels | owner of the parcel at the principal address; corroboration only |
| `site_entities` | Business Entities | entities whose principal address is a parcel's site address |

- **Name matching:** punctuation is dropped (`5241 S. Rio Grande Street, LLC` =
  `5241 S RIO GRANDE STREET LLC`). Several entities with one name -> `ambiguous`, none
  picked. A name without LLC/INC/CORP/... is not searched. UCC debtors are matched
  without the suffix and each filing shows the debtor name as filed.
- **Colorado does not list LLC members.** The record of who can act is the Statement of
  Authority at the county Clerk & Recorder (below). `entity_address_owner` and
  `entity_related` are leads, not proof; the address owner is never a contact, and a
  shared surname is noted as corroboration of the address only.
- **Agent names are normalized before matching** (punctuation, `&`/AND, P.C./LLC/...).
  A firm that adds a name partner keeps its first words, so a name that starts with
  all the words (3 or more) of the other is the same firm.
- **Filing gaps are a note, not a flag:** a calendar year with no periodic report.
- **UCC is not mortgages:** UCC filings cover personal property and fixtures.
- `sos.state.co.us` is behind a Cloudflare check, so it is only linked, not read.

## Statement of Authority (state.py AUTHORITY)

An entity that holds Colorado real property records a Statement of Authority (C.R.S.
38-30-172); section 5 names "each person authorized to execute instruments ... on
behalf of the entity". The recorder's index lists only the entity, so the names are
read from the scanned text: the search returns ~200 characters around a match, so it
searches the form's wording ("on behalf of the entity is"), limited to statements
recorded that day, then that wording plus what it has read, until the form's next
item. The note says it is OCR and names a matching deed grantor.

## Tax check (derive/taxcheck.py)

Colorado taxes school levies on a separate school assessed value, so the check is
school value x school mills + taxable value x the other mills, against the bill. The
levies come from the treasurer platform's hooks (Arapahoe: the Assessor's `Levy.aspx`
and the tax statement PDF). When the Treasurer page's values don't give the bill, the
statement's tax per authority shows the values the bill used and the difference is
flagged (Arapahoe's page shows the next year's assessed value beside the bill). Not
reproduced either way: `check_failed` with both numbers.

## Arapahoe (config/counties/arapahoe.toml, platforms/arapahoegov.py)

Every step configured: the county's own parcel layer (InfoService/12), its in-house
Assessor page (`parcelsearch.arapahoegov.com`, read by ASP.NET label ids), value-change
layers (CustomCAMA_WM 75/76/78), its in-house Treasurer (`taxsearch.arapahoegov.com`),
jurisdiction (InfoService/11), zoning (the county's layer for unincorporated land,
Littleton's for Littleton), its copy of FEMA's flood layers (links the panel PDF),
publicsearch, and permits (Accela for unincorporated land, eTRAKiT for Littleton).
Its GIS sale date is the recording date and can differ from the Assessor's sale date
for the same reception number; both are kept and labelled.
