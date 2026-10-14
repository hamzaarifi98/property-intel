"""Constants for the City of Littleton building permits (permits.py)."""

# eTRAKiT (CentralSquare), an ASP.NET WebForms page: the search and each permit's
# detail are form posts that carry the page's __VIEWSTATE.
PERMITS_URL = "https://permit9.littletonco.gov/etrakit/Search/permit.aspx"
PERMITS_SOURCE = "City of Littleton permits (eTRAKiT, permit9.littletonco.gov)"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/129.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
# Form controls (name attributes)
SEARCH_BY = "ctl00$cplMain$ddSearchBy"
SEARCH_OPER = "ctl00$cplMain$ddSearchOper"
SEARCH_TEXT = "ctl00$cplMain$txtSearchString"
SEARCH_BUTTON = "ctl00$cplMain$btnSearch"
RESULTS_GRID = "ctl00$cplMain$rgSearchRslts"          # a row click posts "RowClick;<index>"
RESULTS_TABLE_ID = "ctl00_cplMain_rgSearchRslts_ctl00"
BY_ADDRESS = "Permit_Main.SITE_ADDR"
BEGINS_WITH = "BEGINS WITH"
MAX_DETAILS = 40                                      # permit pages read per lookup, at most

# Permit Info labels: our field -> label id suffix (cplMain_ctl07_<id>)
PERMIT_LABELS = {
    "type": "lblPermitType", "subtype": "lblPermitSubtype", "scope": "lblPermitDesc",
    "status": "lblPermitStatus", "applied": "lblPermitAppliedDate",
    "approved": "lblPermitApprovedDate", "issued": "lblPermitIssuedDate",
    "finaled": "lblPermitFinaledDate", "expires": "lblPermitExpirationDate",
    "description": "lblPermitNotes",
}
DATE_LABELS = {"applied", "approved", "issued", "finaled", "expires"}
# Site Info labels (cplMain_ctl08_<id>)
BUSINESS_LABEL = "lblSiteTract"                       # shown as "Business Name:"
APN_LABEL = "lblParcelNumberLbl"                      # the value is in the next column

PERMITS_NOTE = ("building, right-of-way and other permits at the site address, newest first. "
                "Contacts (applicant, contractor, owner) are not shown to public users of "
                "eTRAKiT, so no contractor is named here")
