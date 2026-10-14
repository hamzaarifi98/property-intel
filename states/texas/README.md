# Texas

Nothing built yet. This folder holds everything Texas-specific; shared vendor
platforms (ArcGIS, GovOS publicsearch, Accela, eTRAKiT) are in `core/platforms/`.

```
states/texas/state.py              what the shared lookup needs from Texas (core/state.py)
states/texas/config/defaults.toml  sections every Texas county gets (FEMA flood)
states/texas/config/counties/      one <county>.toml per county
states/texas/config/cities/        city zoning and permits
states/texas/platforms/            platforms only Texas uses (a county's own site)
```

To start:

1. **A county config.** Copy `states/colorado/config/counties/boulder.toml` to
   `states/texas/config/counties/<county>.toml` and point `[parcel]` at the county's
   (or appraisal district's) ArcGIS parcel layer. Many Texas Clerks use GovOS
   publicsearch (`<county>.tx.publicsearch.us`): configure `[recorder]` with that host.
2. **Check and run.**
   `python -m core.config --state texas` and
   `python -m core.lookup <parcel id> --state texas --county <Name>`.
3. **Statewide pieces, in `state.py`, as they are built:** `resolve` (address to parcel and
   county; until it exists, lookups need `--county` and an id), `entity` (business
   owners), `tax_check`, `AUTHORITY` (Texas has no Colorado-style Statement of
   Authority; leave it `None` unless a recorded equivalent is used).
4. **New platforms:** a vendor used outside Texas too goes in `core/platforms/`; a site
   only one Texas county uses goes in `states/texas/platforms/` and is listed in
   `PLATFORMS` and `REQUIRED` in `state.py`.

Texas property data is organised differently from Colorado's: values come from a
county appraisal district (CAD), taxes from the county tax assessor-collector (and
often other taxing units), and deeds from the County Clerk.
