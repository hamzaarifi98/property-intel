"""Constants for the Colorado Secretary of State open-data layer (sos.py).

Every dataset is on the state's Socrata portal, data.colorado.gov, refreshed
daily, no key needed (SOCRATA_APP_TOKEN, if set, raises the rate limit).
Column names are copied from each dataset's /api/views/<id>.json.
"""
import re

SOS_SOURCE = "Colorado Secretary of State open data (data.colorado.gov)"
SOCRATA_URL = "https://data.colorado.gov/resource/{}.json"
SOCRATA_LIMIT = 200                     # rows per query; more is reported, not paged

# One CLI run: results/sos/<input> - 15-42 - 13-10-26.json
RESULTS_SUBDIR = "colorado/sos"
RESULT_TIME_FORMAT = "%H-%M - %d-%m-%y"

# --------------------------------------------------------------- datasets
ENTITIES = "4ykn-tg5h"                  # Business Entities in Colorado
HISTORY = "casm-dbbj"                   # Business Entity Transaction History
TRADE_NAMES = "u7sb-g482"               # Trade Names for Businesses in Colorado
UCC_DEBTORS = "8upq-58vz"               # UCC Debtor Information
UCC_FILINGS = "wffy-3uut"               # UCC Filing Information
UCC_COLLATERAL = "4am6-w6u4"            # UCC Collateral Information

# Not checked: www.sos.state.co.us answers scripted requests with 403 (Cloudflare);
# the link opens in a browser
SOS_RECORD_URL = "https://www.sos.state.co.us/biz/BusinessEntityDetail.do?masterFileId={}"

# ----------------------------------------------------------- name matching
# Filings that count as the yearly periodic report
REPORT_FILINGS = {"File Report", "Statement of Older Periodic Report"}
# The Trade Names dataset lists every entity's own name with this form; it is not a DBA
OWN_NAME_FORM = "Entity Type"
# Agent names are compared on their words without punctuation, "&"/AND and P.C./LLC/...;
# a firm that adds a name partner keeps its first words ("Folkestad Fazekas Barrick &
# Patoile" -> "... Barrick Patoile & James"), so a name that starts with the other
# name's words, at least this many, is the same firm
AGENT_RENAME_MIN_WORDS = 3

# ------------------------------------------------------------------ fields
ENTITY_FIELD = "owner_entity"
# The other fields (entity_filings, entity_last_report, entity_trade_names,
# entity_ucc_filings, entity_related, entity_address_owner) are looked up only
# when exactly one entity has the name

# ------------------------------------------------------------------- notes
ENTITY_NOTE = ("Colorado does not list LLC members or managers; the person who can act "
               "for the entity is in a Statement of Authority at the county Clerk & Recorder")
FILINGS_NOTE = "every filing with the Secretary of State, newest first"
REPORT_NOTE = ("date of the last periodic report; an entity that misses it becomes "
               "delinquent")
TRADE_NAMES_NOTE = ("names the entity has registered to do business under, besides its own "
                    "name")
NO_TRADE_NAMES_NOTE = "no trade names besides the entity name"
UCC_NOTE = ("secured-debt notices (UCC) naming the entity as debtor; they cover personal "
            "property and fixtures, not mortgages (deeds of trust are at the county "
            "Clerk & Recorder). in_effect: not terminated and not past its lapse date")
RELATED_NOTE = ("other entities with the same street number, street name and ZIP as this "
                "one's principal address; at a shared office this can include unrelated "
                "businesses. same_agent: the same registered agent, compared after "
                "normalizing the name (punctuation, &, P.C., a later-added name partner)")
ADDRESS_OWNER_NOTE = ("corroboration only, not a contact: the owner of the parcel at the "
                      "entity's principal address (taken from the Secretary of State record), "
                      "looked up in the statewide parcel layer. It ties the address to a name; "
                      "it says nothing about who controls the entity. The Statement of "
                      "Authority at the county Clerk & Recorder is the record of who can act")
SITE_ENTITIES_NOTE = ("entities whose principal address is this street number, street name and "
                      "ZIP: businesses that say they operate here (possible occupants). The "
                      "address is self-reported to the Secretary of State")
AGENT_ADDRESS_NOTE = ("the principal address is the registered agent's office, so it says "
                      "nothing about the owner")
