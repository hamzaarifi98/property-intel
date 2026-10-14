"""Constants for the Arapahoe County adapter (arapahoe.py).

URLs, layer ids, source names, field maps and notes for each step of the
lookup, in the same order as the steps. Change a source here, not in the code.
"""
import re

COUNTY = "Arapahoe"
SOURCE = "Arapahoe County Assessor"

# One file per CLI run: results/arapahoe/<input> - 15-42 - 13-10-26.json
# (hour-minute - day-month-year, local time; "/" is not allowed in file names)
RESULTS_SUBDIR = "arapahoe"
RESULT_TIME_FORMAT = "%H-%M - %d-%m-%y"

# ------------------------------------------------------------- 1. county GIS layer
GIS_URL = ("https://gis.arapahoegov.com/arcgis/rest/services/"
           "InfoService/MapServer/12/query")
GIS_SOURCE = "Arapahoe County GIS (InfoService parcel layer)"
GIS_PAGE_SIZE = 1000  # layer maxRecordCount

# Our field name -> layer field name (copied from the layer's ?f=json).
# Names match resolve.FIELDS where the statewide layer has the same thing.
GIS_FIELDS = {
    "parcel_id": "PARCEL_ID",              # AIN
    "account": "PIN",                      # blank in the statewide layer for Arapahoe
    "site_address": "Situs_Address",
    "site_city": "City",
    "site_zip": "Zip",
    "owner": "Owner",
    "owner_mail_street": "Owner_Mail_Address",
    "owner_mail_city": "Owner_City",
    "owner_mail_state": "Owner_State",
    "owner_mail_zip": "Owner_Zip",
    "owner_mail_country": "Owner_Cntry",
    "neighborhood_code": "Neighborhood_Code",
    "neighborhood_name": "Neighborhood",
    "land_use_code": "PUC_Code",
    "land_use_desc": "PUC",
    "appraised_total": "Appr_Value",
    "appraised_building": "Imp_Value",
    "appraised_land": "Land_Value",
    "assessed_total": "Assd_Value",
    "taxable_total": "Taxable",
    "last_sale_date": "Sale_Date",
    "last_sale_price": "Price",
    "last_sale_reception": "REC_NMBR",
    "gis_area_sqft": "GIS_AREA",
    "latitude": "Coordinate_Y",
    "longitude": "Coordinate_X",
}
DATE_FIELDS = {"last_sale_date"}           # epoch milliseconds in the layer
GIS_NOTES = {
    "owner": "Primary surface owner (Colorado separates surface and mineral rights)",
    "neighborhood_code": "The statewide layer carries this in subCode for Arapahoe",
    "neighborhood_name": "The statewide layer carries this in subName for Arapahoe",
    "last_sale_date": "The Assessor's page can show a different date for the same reception "
                      "number; see the sales observation",
    "gis_area_sqft": "Polygon area computed by GIS, not the Assessor's land area",
    "latitude": "County-provided point inside the parcel",
    "longitude": "County-provided point inside the parcel",
}
DEED_SEARCH_URL = ("https://arapahoe.co.publicsearch.us/results?department=RP"
                   "&searchType=quickSearch&searchValue={}")

# ------------------------------------------------------ 2. Assessor parcel page
PAGE_URL = "https://parcelsearch.arapahoegov.com/PPINum.aspx"
PAGE_SOURCE = "Arapahoe County Assessor parcel page (parcelsearch.arapahoegov.com)"
# The server has answered a bare client with 403; it serves browser-like requests.
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/129.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
NOT_FOUND_TEXT = "No matching records were found"
FOUND_MARKER = "ucParcelHeader_lblAinTxt"

# Our field name -> label id on the page (ucParcelHeader_<id>); only what the layer lacks
HEADER_FIELDS = {
    "ownership_type": "lblOwnershipTypeTxt",
    "land_acres": "lblAcreageTxt",
    "legal_description": "lblLegalDescTxt",
}
NUMBER_FIELDS = {"land_acres"}

# (title label id, {our field: value label id}), ids prefixed ucParcelValue_
VALUE_GROUPS = [
    ("lblAssessValueTitle", {"assessed_building": "lblAssdBuilding",
                             "assessed_land": "lblAssdLand"}),
    ("lblSchoolValueTitle", {"assessed_school_total": "lblSchoolTotal",
                             "assessed_school_building": "lblSchoolBuilding",
                             "assessed_school_land": "lblSchoolLand"}),
]

# Everything the page adds; reported as unavailable when the page can't be read
DETAIL_FIELDS = ["ownership_type", "land_acres", "legal_description", "value_tax_year",
                 "assessed_building", "assessed_land", "assessed_school_total",
                 "assessed_school_building", "assessed_school_land", "mill_levy",
                 "sales", "buildings", "building_areas", "living_area_sqft", "basement_sqft",
                 "land_lines", "land_sqft"]

# Area lines (building_areas descriptions): floors above grade count as living area
LIVING_AREA = re.compile(r"\b(first|second|third|main|upper|ground)\s+floor\b|\bhalf\s+story\b"
                         r"|\bfloor\s+\d\b|\bliving\b", re.I)
NOT_LIVING_AREA = re.compile(r"basement|garage|patio|porch|deck|terrace|carport|balcony", re.I)
BASEMENT = re.compile(r"basement", re.I)
BASEMENT_TOTAL = re.compile(r"basement\s+total", re.I)   # when given, the finished part is in it
SQFT_PER_ACRE = 43_560

# ------------------------------------------------------- 3. value history (GIS)
CAMA_URL = ("https://gis.arapahoegov.com/arcgis/rest/services/"
            "CustomCAMA_WM/MapServer/{}/query")
HISTORY_SOURCE = "Arapahoe County GIS (CustomCAMA value-change layers)"
# Layer id -> {tax year: layer field}. Each layer is one reappraisal's old-vs-new
# comparison. Colorado reappraises in odd years; an even year repeats the year before.
VALUE_CHANGE_LAYERS = {
    75: {2023: "tax_val", 2022: "pr_val"},                            # Value Change 2023
    76: {2021: "F2021_Value", 2020: "F2020_Aumentum_Value",
         2018: "F2018_Aumentum_Value"},                                # Value Change 2021
    78: {2017: "F2017Value", 2016: "F2016Value"},                     # Value Change 2017
}

# ------------------------------------------------------------- 4. Treasurer
TREASURER_URL = "https://taxsearch.arapahoegov.com/ReReport.aspx"
PRIOR_YEAR_URL = "https://taxsearch.arapahoegov.com/PrevYear.aspx"
TAX_BILL_URL = "https://taxsearch.arapahoegov.com/FileDownload.ashx?AIN={}&PIN={}"
TREASURER_SOURCE = "Arapahoe County Treasurer tax search (taxsearch.arapahoegov.com)"
TREASURER_FOUND = "ContentPlaceHolder1_lblAIN"
TREASURER_NOT_FOUND = "No matching record was found"
TREASURER_NOTE = ("certified by the Treasurer, but not a Certificate of Taxes Due "
                  "(the document used at closing)")
# Our field -> label id (ContentPlaceHolder1_<id>); the full-year column of the tax detail
TAX_MONEY_FIELDS = {
    "tax_taxable_value": "lblTaxableValue",
    "tax_taxable_school_value": "lblTaxableSchoolValue",  # school levies use this one
    "tax_billed": "lblOrigTaxAmt",                 # "Assessed Tax" row
    "tax_exemptions": "lblFullExAmt",
    "tax_special_assessments": "lblOrigSAAmt",
    "tax_fees": "lblOrigFeeAmt",
    "tax_interest": "lblFullInterest",
    "tax_paid": "lblPaidTotal",
    "tax_due": "lblTaxFull",
}
TAX_FLAGS = {                                      # Y/N on the page
    "tax_prior_years_due": "lblPriorYear",
    "tax_bankruptcy": "lblBankruptcy",
    "tax_treasurers_assessment": "lblTreasAssess",
    "tax_lien": "lblTaxLien",
    "tax_treasurers_deed": "lblTreasDeed",
}
# Taxing authorities whose levy applies to the school assessed value
SCHOOL_AUTHORITY = re.compile(r"\bSCHOOL\b|\bSCH\b", re.I)
PRIOR_MONEY_FIELDS = {"tax_prior_year_billed": "lblOrigTotalAmt",
                      "tax_prior_year_paid": "lblPaidTotal"}
TAX_FIELDS = ["tax_year", "tax_rate", *TAX_MONEY_FIELDS, *TAX_FLAGS, "tax_bill_pdf_url"]
PRIOR_TAX_FIELDS = ["tax_prior_year", "tax_prior_year_rate", *PRIOR_MONEY_FIELDS]

# --------------------------------------------------------------- 5. location
LAYER_QUERY_URL = "https://gis.arapahoegov.com/arcgis/rest/services/{}/MapServer/{}/query"
JURISDICTION_URL = LAYER_QUERY_URL.format("InfoService", 11)
COUNTY_ZONING_URL = LAYER_QUERY_URL.format("InfoService", 60)     # unincorporated county
FLOOD_URL = LAYER_QUERY_URL.format("ArapaMAP", 380)               # FEMA Flood Hazards
FLOOD_PANEL_URL = LAYER_QUERY_URL.format("ArapaMAP", 386)         # DFIRM Panels
LOCATION_SOURCE = "Arapahoe County GIS (ArapaMAP / InfoService layers)"
LITTLETON_ZONING_URL = ("https://ltngiswa.littletonco.gov/server/rest/services/"
                        "City/LittletonParcelZoning/MapServer/2/query")
LITTLETON_SOURCE = "City of Littleton GIS (LittletonParcelZoning, Zoning Districts)"
NO_BFE = -9999                                    # FEMA's "no base flood elevation"
POINT_NOTE = "at the county's point inside the parcel"
ZONING_FIELDS = ["zoning_code", "zoning_desc", "zoning_code_url"]
FLOOD_FIELDS = ["flood_zone", "flood_zone_subtype", "flood_sfha", "flood_bfe"]
FLOOD_PANEL_FIELDS = ["flood_panel", "flood_panel_url"]

# ------------------------------------------- 8. permits, unincorporated county
# Arapahoe County Public Works & Development uses Accela Citizen Access. The module
# searches and each permit's page require a login; the site-wide search does not,
# and lists each record's date, number, project name, type and status.
COUNTY_PERMITS_URL = "https://aca-prod.accela.com/arapahoe/Cap/GlobalSearchResults.aspx"
COUNTY_PERMITS_SOURCE = ("Arapahoe County Public Works & Development permits "
                         "(Accela Citizen Access, aca-prod.accela.com/arapahoe)")
COUNTY_PERMITS_TABLE_ID = "ctl00_PlaceHolderMain_CapView_gdvPermitList"
COUNTY_PERMITS_NOTE = ("records the portal's site-wide search finds for the site address, newest "
                       "first. The list does not show each record's address, and the record "
                       "pages (with contacts such as the contractor) need a login")
# Who issues permits, by jurisdiction (InfoService/11)
PERMIT_ISSUERS = {"UNINCORPORATED": "Arapahoe County (Public Works & Development)"}

# ------------------------------------------------------ 7. Clerk & Recorder
# A GovOS (Kofile) "publicsearch" site. No REST API: the page runs every search over
# a WebSocket, authenticated by the token it embeds (window.__ort) and its cookie.
RECORDER_HOST = "arapahoe.co.publicsearch.us"
RECORDER_SOURCE = "Arapahoe County Clerk & Recorder (arapahoe.co.publicsearch.us)"
RECORDER_DEPARTMENT = "RP"                       # Real Property records
RECORDER_SEARCH = "@kofile/FETCH_DOCUMENTS/v4"   # the page's search message
RECORDER_PAGE_SIZE = 50                          # the page's own page size
RECORDER_TIMEOUT = 30                            # seconds per WebSocket reply
STA_DOC_TYPES = ["STA"]                          # Statement of Authority
DEED_GROUP = "Deed"
# Section 5 of the statutory Statement of Authority form (C.R.S. 38-30-172):
# "The name and position of each person authorized to execute instruments ... on
# behalf of the entity is: <names>". The search only returns ~200 characters of
# scanned text around a match, so the names are read forward from this phrase.
STA_ANCHORS = ["behalf of the entity is", "on behalf of the entity", "real property on behalf of"]
STA_END = (r"\b\d{1,2}\s*[.,:]?\s*The\s+(?:authority|limitations)|\bThe\s+(?:authority|limitations)\b"
           r"|\bSTATE\s+OF\b|\bSubscribed\b|\bOther\s+matters\b")
STA_MAX_STEPS = 8                                # searches per statement, at most
RECORDER_FIELDS = ["recorded_documents", "owner_acquisition_deed", "statement_of_authority"]
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
STA_NOTE = ("Statement of Authority recorded by the owner: the person(s) who may sign for it. "
            "authorized is read from the scanned text (OCR); check it against the document")
