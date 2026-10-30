"""
Canonical Global Location Targeting Engine for HuntFlow.

Provides a unified, deterministic geographic targeting, normalization, and evaluation layer
used consistently across:
- Job discovery, filtering, matching & automated application selection
- Prospect discovery, LinkedIn connection targeting, outreach campaigns & qualification
- Email prospecting, follow-up & CRM filtering

Key Principles:
1. Separation of Concerns:
   - Target Search Location != Candidate Legal Work Authorization
   - Candidate Current Location != Target Search Geography
   - Job Search Geography != Outreach / Prospect Geography
   - Work Mode (Remote/Hybrid/Onsite) & Relocation are evaluated independently
2. Deterministic Normalization:
   - Resolves ISO-3166-1 alpha-2 country codes, aliases, and explicit regions
   - Explicitly defines Europe vs European Union (EU-27), APAC, ANZ, North America, etc.
   - Preserves ambiguity for ungrounded cities (e.g. "Springfield") without guessing
3. Exclusions Win:
   - Explicit exclusion rules always override inclusion rules
4. Conservative Defaults:
   - Unknown/Ambiguous locations default to not auto-eligible unless explicitly allowed
"""

from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# ── Region Definitions (Explicit Country Sets) ──────────────────────────

# EU-27 Member States (2026)
EU_27_COUNTRY_CODES: Set[str] = {
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR",
    "DE", "GR", "HU", "IE", "IT", "LV", "LT", "LU", "MT", "NL",
    "PL", "PT", "RO", "SK", "SI", "ES", "SE"
}

# Broader Geographic Europe (EU-27 + UK + EEA/EFTA + European microstates/Balkans/Eastern Europe)
EUROPE_COUNTRY_CODES: Set[str] = EU_27_COUNTRY_CODES | {
    "GB", "CH", "NO", "IS", "LI", "AL", "AD", "BA", "ME", "MK",
    "MD", "MC", "RS", "SM", "UA", "VA", "XK", "BY"
}

# ANZ (Australia & New Zealand)
ANZ_COUNTRY_CODES: Set[str] = {"AU", "NZ"}

# APAC (Asia-Pacific)
APAC_COUNTRY_CODES: Set[str] = ANZ_COUNTRY_CODES | {
    "JP", "KR", "SG", "MY", "ID", "PH", "TH", "VN", "IN", "PK",
    "BD", "LK", "TW", "HK", "CN"
}

# North America
NORTH_AMERICA_COUNTRY_CODES: Set[str] = {"US", "CA", "MX"}

# LATAM (Latin America)
LATAM_COUNTRY_CODES: Set[str] = {
    "BR", "AR", "CL", "CO", "PE", "MX", "UY", "EC", "PA", "CR", "BO", "PY", "VE", "GT", "DO"
}

# MENA (Middle East & North Africa)
MENA_COUNTRY_CODES: Set[str] = {
    "AE", "SA", "QA", "KW", "BH", "OM", "EG", "JO", "LB", "IL", "MA", "TN", "DZ", "IQ"
}

REGION_MAP: Dict[str, Set[str]] = {
    "europe": EUROPE_COUNTRY_CODES,
    "european union": EU_27_COUNTRY_CODES,
    "eu": EU_27_COUNTRY_CODES,
    "eu-27": EU_27_COUNTRY_CODES,
    "anz": ANZ_COUNTRY_CODES,
    "australasia": ANZ_COUNTRY_CODES,
    "australia and new zealand": ANZ_COUNTRY_CODES,
    "apac": APAC_COUNTRY_CODES,
    "asia pacific": APAC_COUNTRY_CODES,
    "asia-pacific": APAC_COUNTRY_CODES,
    "north america": NORTH_AMERICA_COUNTRY_CODES,
    "latam": LATAM_COUNTRY_CODES,
    "latin america": LATAM_COUNTRY_CODES,
    "mena": MENA_COUNTRY_CODES,
    "middle east": MENA_COUNTRY_CODES,
}

# ── Country Aliases & Names Mapping ─────────────────────────────────────

COUNTRY_NAME_BY_CODE: Dict[str, str] = {
    "US": "United States",
    "GB": "United Kingdom",
    "AU": "Australia",
    "NZ": "New Zealand",
    "PK": "Pakistan",
    "DE": "Germany",
    "FR": "France",
    "CA": "Canada",
    "IE": "Ireland",
    "NL": "Netherlands",
    "CH": "Switzerland",
    "ES": "Spain",
    "IT": "Italy",
    "SE": "Sweden",
    "NO": "Norway",
    "DK": "Denmark",
    "FI": "Finland",
    "BE": "Belgium",
    "AT": "Austria",
    "PL": "Poland",
    "PT": "Portugal",
    "CZ": "Czech Republic",
    "AE": "United Arab Emirates",
    "SA": "Saudi Arabia",
    "QA": "Qatar",
    "SG": "Singapore",
    "JP": "Japan",
    "KR": "South Korea",
    "IN": "India",
    "MY": "Malaysia",
    "ID": "Indonesia",
    "PH": "Philippines",
    "VN": "Vietnam",
    "TH": "Thailand",
    "ZA": "South Africa",
    "BR": "Brazil",
    "MX": "Mexico",
    "AR": "Argentina",
    "CL": "Chile",
    "CO": "Colombia",
    "IL": "Israel",
    "TR": "Turkey",
    "GR": "Greece",
    "RO": "Romania",
    "HU": "Hungary",
}

# Case-insensitive alias map to ISO-2
COUNTRY_ALIAS_MAP: Dict[str, str] = {
    # US
    "us": "US",
    "usa": "US",
    "u.s.": "US",
    "u.s.a.": "US",
    "united states": "US",
    "united states of america": "US",
    "america": "US",

    # UK
    "uk": "GB",
    "u.k.": "GB",
    "united kingdom": "GB",
    "great britain": "GB",
    "britain": "GB",
    "england": "GB",
    "scotland": "GB",
    "wales": "GB",
    "northern ireland": "GB",
    "gb": "GB",

    # Australia
    "au": "AU",
    "aus": "AU",
    "australia": "AU",

    # New Zealand
    "nz": "NZ",
    "new zealand": "NZ",
    "aotearoa": "NZ",

    # Pakistan
    "pk": "PK",
    "pak": "PK",
    "pakistan": "PK",

    # Germany
    "de": "DE",
    "ger": "DE",
    "germany": "DE",
    "deutschland": "DE",

    # France
    "fr": "FR",
    "fra": "FR",
    "france": "FR",

    # Canada
    "ca": "CA",
    "can": "CA",
    "canada": "CA",

    # Ireland
    "ie": "IE",
    "ireland": "IE",
    "republic of ireland": "IE",

    # Netherlands
    "nl": "NL",
    "netherlands": "NL",
    "holland": "NL",
    "the netherlands": "NL",

    # Switzerland
    "ch": "CH",
    "switzerland": "CH",
    "swiss": "CH",
    "schweiz": "CH",
    "suisse": "CH",

    # Spain
    "es": "ES",
    "spain": "ES",
    "espana": "ES",

    # Italy
    "it": "IT",
    "italy": "IT",
    "italia": "IT",

    # Sweden
    "se": "SE",
    "sweden": "SE",
    "sverige": "SE",

    # Norway
    "no": "NO",
    "norway": "NO",
    "norge": "NO",

    # Denmark
    "dk": "DK",
    "denmark": "DK",
    "danmark": "DK",

    # Finland
    "fi": "FI",
    "finland": "FI",
    "suomi": "FI",

    # Belgium
    "be": "BE",
    "belgium": "BE",
    "belgique": "BE",

    # Austria
    "at": "AT",
    "austria": "AT",
    "osterreich": "AT",

    # Poland
    "pl": "PL",
    "poland": "PL",
    "polska": "PL",

    # Portugal
    "pt": "PT",
    "portugal": "PT",

    # Czech Republic
    "cz": "CZ",
    "czechia": "CZ",
    "czech republic": "CZ",

    # UAE
    "ae": "AE",
    "uae": "AE",
    "united arab emirates": "AE",
    "dubai": "AE",
    "abu dhabi": "AE",

    # Saudi Arabia
    "sa": "SA",
    "ksa": "SA",
    "saudi arabia": "SA",

    # Singapore
    "sg": "SG",
    "singapore": "SG",

    # Japan
    "jp": "JP",
    "japan": "JP",
    "nippon": "JP",

    # South Korea
    "kr": "KR",
    "korea": "KR",
    "south korea": "KR",

    # India
    "in": "IN",
    "india": "IN",
    "bharat": "IN",

    # Malaysia
    "my": "MY",
    "malaysia": "MY",

    # Indonesia
    "id": "ID",
    "indonesia": "ID",

    # Philippines
    "ph": "PH",
    "philippines": "PH",

    # Vietnam
    "vn": "VN",
    "vietnam": "VN",

    # Thailand
    "th": "TH",
    "thailand": "TH",

    # South Africa
    "za": "ZA",
    "south africa": "ZA",

    # Brazil
    "br": "BR",
    "brazil": "BR",
    "brasil": "BR",

    # Mexico
    "mx": "MX",
    "mexico": "MX",
}

# ── Major Unambiguous Cities Mapping ────────────────────────────────────

UNAMBIGUOUS_CITIES: Dict[str, Tuple[str, str]] = {
    # Australia
    "sydney": ("AU", "Sydney"),
    "melbourne": ("AU", "Melbourne"),
    "brisbane": ("AU", "Brisbane"),
    "perth": ("AU", "Perth"),
    "adelaide": ("AU", "Adelaide"),
    "canberra": ("AU", "Canberra"),
    "gold coast": ("AU", "Gold Coast"),
    "hobart": ("AU", "Hobart"),

    # New Zealand
    "auckland": ("NZ", "Auckland"),
    "wellington": ("NZ", "Wellington"),
    "christchurch": ("NZ", "Christchurch"),
    "hamilton": ("NZ", "Hamilton"),
    "tauranga": ("NZ", "Tauranga"),
    "dunedin": ("NZ", "Dunedin"),

    # United Kingdom
    "london": ("GB", "London"),
    "manchester": ("GB", "Manchester"),
    "birmingham": ("GB", "Birmingham"),
    "edinburgh": ("GB", "Edinburgh"),
    "glasgow": ("GB", "Glasgow"),
    "leeds": ("GB", "Leeds"),
    "bristol": ("GB", "Bristol"),
    "liverpool": ("GB", "Liverpool"),
    "newcastle": ("GB", "Newcastle"),
    "belfast": ("GB", "Belfast"),
    "cardiff": ("GB", "Cardiff"),
    "sheffield": ("GB", "Sheffield"),
    "urmston": ("GB", "Urmston"),

    # United States
    "new york": ("US", "New York"),
    "nyc": ("US", "New York"),
    "san francisco": ("US", "San Francisco"),
    "los angeles": ("US", "Los Angeles"),
    "seattle": ("US", "Seattle"),
    "chicago": ("US", "Chicago"),
    "austin": ("US", "Austin"),
    "boston": ("US", "Boston"),
    "atlanta": ("US", "Atlanta"),
    "denver": ("US", "Denver"),
    "dallas": ("US", "Dallas"),
    "houston": ("US", "Houston"),
    "miami": ("US", "Miami"),
    "philadelphia": ("US", "Philadelphia"),
    "phoenix": ("US", "Phoenix"),
    "san diego": ("US", "San Diego"),

    # Canada
    "toronto": ("CA", "Toronto"),
    "vancouver": ("CA", "Vancouver"),
    "montreal": ("CA", "Montreal"),
    "ottawa": ("CA", "Ottawa"),
    "calgary": ("CA", "Calgary"),
    "edmonton": ("CA", "Edmonton"),

    # Germany
    "berlin": ("DE", "Berlin"),
    "munich": ("DE", "Munich"),
    "munchen": ("DE", "Munich"),
    "frankfurt": ("DE", "Frankfurt"),
    "hamburg": ("DE", "Hamburg"),
    "cologne": ("DE", "Cologne"),
    "koln": ("DE", "Cologne"),
    "stuttgart": ("DE", "Stuttgart"),
    "dusseldorf": ("DE", "Dusseldorf"),

    # France
    "paris": ("FR", "Paris"),
    "lyon": ("FR", "Lyon"),
    "marseille": ("FR", "Marseille"),
    "toulouse": ("FR", "Toulouse"),
    "bordeaux": ("FR", "Bordeaux"),

    # Netherlands
    "amsterdam": ("NL", "Amsterdam"),
    "rotterdam": ("NL", "Rotterdam"),
    "the hague": ("NL", "The Hague"),
    "utrecht": ("NL", "Utrecht"),

    # Pakistan
    "sargodha": ("PK", "Sargodha"),
    "lahore": ("PK", "Lahore"),
    "karachi": ("PK", "Karachi"),
    "islamabad": ("PK", "Islamabad"),
    "rawalpindi": ("PK", "Rawalpindi"),
    "faisalabad": ("PK", "Faisalabad"),
    "multan": ("PK", "Multan"),
    "peshawar": ("PK", "Peshawar"),
    "quetta": ("PK", "Quetta"),
    "sialkot": ("PK", "Sialkot"),

    # Other Major Global Cities
    "tokyo": ("JP", "Tokyo"),
    "seoul": ("KR", "Seoul"),
    "singapore": ("SG", "Singapore"),
    "dubai": ("AE", "Dubai"),
    "abu dhabi": ("AE", "Abu Dhabi"),
    "riyadh": ("SA", "Riyadh"),
    "doha": ("QA", "Doha"),
    "zurich": ("CH", "Zurich"),
    "geneva": ("CH", "Geneva"),
    "stockholm": ("SE", "Stockholm"),
    "oslo": ("NO", "Oslo"),
    "copenhagen": ("DK", "Copenhagen"),
    "helsinki": ("FI", "Helsinki"),
    "vienna": ("AT", "Vienna"),
    "wien": ("AT", "Vienna"),
    "prague": ("CZ", "Prague"),
    "warsaw": ("PL", "Warsaw"),
    "lisbon": ("PT", "Lisbon"),
    "madrid": ("ES", "Madrid"),
    "barcelona": ("ES", "Barcelona"),
    "rome": ("IT", "Rome"),
    "milan": ("IT", "Milan"),
    "dublin": ("IE", "Dublin"),
}

# Ambiguous city names that exist in multiple countries (do NOT guess country without qualifier)
AMBIGUOUS_CITIES: Set[str] = {
    "springfield", "richmond", "cambridge", "victoria", "san jose",
    "arlington", "alexandria", "columbia", "greenville", "burlington",
    "oxford", "georgetown", "kingston", "hamilton", "santiago"
}

# ── US State Definitions ────────────────────────────────────────────────
US_STATES: Dict[str, str] = {
    "al": "Alabama", "ak": "Alaska", "az": "Arizona", "ar": "Arkansas", "ca": "California",
    "co": "Colorado", "ct": "Connecticut", "de": "Delaware", "fl": "Florida", "ga": "Georgia",
    "hi": "Hawaii", "id": "Idaho", "il": "Illinois", "in": "Indiana", "ia": "Iowa",
    "ks": "Kansas", "ky": "Kentucky", "la": "Louisiana", "me": "Maine", "md": "Maryland",
    "ma": "Massachusetts", "mi": "Michigan", "mn": "Minnesota", "ms": "Mississippi", "mo": "Missouri",
    "mt": "Montana", "ne": "Nebraska", "nv": "Nevada", "nh": "New Hampshire", "nj": "New Jersey",
    "nm": "New Mexico", "ny": "New York", "nc": "North Carolina", "nd": "North Dakota", "oh": "Ohio",
    "ok": "Oklahoma", "or": "Oregon", "pa": "Pennsylvania", "ri": "Rhode Island", "sc": "South Carolina",
    "sd": "South Dakota", "tn": "Tennessee", "tx": "Texas", "ut": "Utah", "vt": "Vermont",
    "va": "Virginia", "wa": "Washington", "wv": "West Virginia", "wi": "Wisconsin", "wy": "Wyoming",
    "dc": "District of Columbia",
}

US_STATE_NAMES_TO_CODE: Dict[str, str] = {v.lower(): k.upper() for k, v in US_STATES.items()}

# ── Canadian Province Definitions ───────────────────────────────────────
CA_PROVINCES: Dict[str, str] = {
    "on": "Ontario", "qc": "Quebec", "bc": "British Columbia", "ab": "Alberta",
    "mb": "Manitoba", "sk": "Saskatchewan", "ns": "Nova Scotia", "nb": "New Brunswick",
    "nl": "Newfoundland and Labrador", "pe": "Prince Edward Island",
    "nt": "Northwest Territories", "yt": "Yukon", "nu": "Nunavut",
}

CA_PROVINCE_NAMES_TO_CODE: Dict[str, str] = {v.lower(): k.upper() for k, v in CA_PROVINCES.items()}

# ── Australian State Definitions ────────────────────────────────────────
AU_STATES: Dict[str, str] = {
    "nsw": "New South Wales", "vic": "Victoria", "qld": "Queensland",
    "wa": "Western Australia", "sa": "South Australia", "tas": "Tasmania",
    "act": "Australian Capital Territory", "nt": "Northern Territory",
}

AU_STATE_NAMES_TO_CODE: Dict[str, str] = {v.lower(): k.upper() for k, v in AU_STATES.items()}

# ── UK Subdivisions ─────────────────────────────────────────────────────
UK_SUBDIVISIONS: Dict[str, str] = {
    "uk": "United Kingdom", "u.k.": "United Kingdom", "england": "England",
    "scotland": "Scotland", "wales": "Wales", "northern ireland": "Northern Ireland",
    "gbr": "United Kingdom", "gb": "United Kingdom",
}


# ── Data Classes ────────────────────────────────────────────────────────

@dataclass
class NormalizedLocation:
    raw_input: str
    country_code: Optional[str] = None
    country_name: Optional[str] = None
    region: Optional[str] = None
    is_eu: bool = False
    city: Optional[str] = None
    state_province: Optional[str] = None
    is_remote: bool = False
    remote_scope: str = "none"  # "none", "worldwide", "country_restricted", "regional"
    remote_restricted_country: Optional[str] = None
    is_ambiguous: bool = False
    confidence: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "raw_input": self.raw_input,
            "country_code": self.country_code,
            "country_name": self.country_name,
            "region": self.region,
            "is_eu": self.is_eu,
            "city": self.city,
            "state_province": self.state_province,
            "is_remote": self.is_remote,
            "remote_scope": self.remote_scope,
            "remote_restricted_country": self.remote_restricted_country,
            "is_ambiguous": self.is_ambiguous,
            "confidence": self.confidence,
        }


@dataclass
class LocationTargetingPolicy:
    included_locations: List[str] = field(default_factory=list)
    excluded_locations: List[str] = field(default_factory=list)
    remote_mode: str = "allow_all"  # "allow_all", "worldwide_only", "country_only", "deny"
    allow_hybrid: bool = True
    allow_onsite: bool = True
    unknown_location_handling: str = "deny"  # "deny", "allow", "review"
    application_strategy: str = "aggressive"  # "aggressive", "conservative", "strict_auth_only"
    search_worldwide: bool = False
    channel: str = "job_application"  # "job_application", "prospect_outreach", "candidate_search"

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LocationTargetingPolicy":
        if not isinstance(data, dict):
            return cls()
        inc = data.get("included_locations") or []
        if isinstance(inc, str):
            inc = [x.strip() for x in inc.split(",") if x.strip()]
        exc = data.get("excluded_locations") or []
        if isinstance(exc, str):
            exc = [x.strip() for x in exc.split(",") if x.strip()]
        return cls(
            included_locations=list(inc),
            excluded_locations=list(exc),
            remote_mode=data.get("remote_mode", "allow_all"),
            allow_hybrid=bool(data.get("allow_hybrid", True)),
            allow_onsite=bool(data.get("allow_onsite", True)),
            unknown_location_handling=data.get("unknown_location_handling", "deny"),
            application_strategy=data.get("application_strategy", "INTERVIEW_FIRST_SPONSORSHIP"),
            search_worldwide=bool(data.get("search_worldwide", False)),
            channel=data.get("channel", "generic"),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "included_locations": self.included_locations,
            "excluded_locations": self.excluded_locations,
            "remote_mode": self.remote_mode,
            "allow_hybrid": self.allow_hybrid,
            "allow_onsite": self.allow_onsite,
            "unknown_location_handling": self.unknown_location_handling,
            "application_strategy": self.application_strategy,
            "search_worldwide": self.search_worldwide,
            "channel": self.channel,
        }


@dataclass
class LocationMatchResult:
    decision: str  # "MATCH", "NO_MATCH", "EXCLUDED", "UNKNOWN", "AMBIGUOUS", "BLOCKED_WORK_MODE", "BLOCKED_RELOCATION", "BLOCKED_AUTHORIZATION"
    is_eligible: bool
    normalized_location: NormalizedLocation
    matched_rule: Optional[str] = None
    excluded_rule: Optional[str] = None
    authorization_status: str = "CONFIRMED_AUTHORIZED"  # "CONFIRMED_AUTHORIZED", "SPONSORSHIP_REQUIRED", "NOT_CURRENTLY_AUTHORIZED", "AUTHORIZATION_UNKNOWN"
    sponsorship_status: str = "SPONSORSHIP_REQUIRED"  # "SPONSORSHIP_REQUIRED", "NO_SPONSORSHIP_NEEDED", "UNKNOWN"
    attestation_conflict: bool = False
    warnings: List[str] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision": self.decision,
            "is_eligible": self.is_eligible,
            "matched_rule": self.matched_rule,
            "excluded_rule": self.excluded_rule,
            "authorization_status": self.authorization_status,
            "sponsorship_status": self.sponsorship_status,
            "attestation_conflict": self.attestation_conflict,
            "warnings": self.warnings,
            "reasons": self.reasons,
            "normalized_location": self.normalized_location.to_dict() if self.normalized_location else None,
        }


# ── Core Normalization Engine ───────────────────────────────────────────

def normalize_location_string(raw_location: Optional[str]) -> NormalizedLocation:
    """
    Parses and normalizes a raw location string into a structured NormalizedLocation.
    Handles:
    - Remote expressions (Worldwide, country-specific, regional)
    - City, State/Province, Country splitting with standard abbreviations (US states, CA provinces, AU states)
    - Ambiguous city detection (e.g. Springfield without country)
    - ISO-2 country resolution and regional classification
    """
    if not raw_location or not str(raw_location).strip():
        return NormalizedLocation(
            raw_input="",
            is_ambiguous=False,
            confidence=0.0
        )

    raw_clean = str(raw_location).strip()
    raw_lower = raw_clean.lower()

    # 1. Detect Remote Semantics
    is_remote = False
    remote_scope = "none"
    remote_country = None

    if "remote" in raw_lower or "anywhere" in raw_lower or "telecommute" in raw_lower or "wfh" in raw_lower:
        is_remote = True
        if any(w in raw_lower for w in ["worldwide", "global", "anywhere", "work from anywhere", "100% remote worldwide"]):
            remote_scope = "worldwide"
        elif any(w in raw_lower for w in ["us only", "u.s. only", "united states only", "(us)", "- us", ", us", ", united states"]):
            remote_scope = "country_restricted"
            remote_country = "US"
        elif any(w in raw_lower for w in ["uk only", "u.k. only", "united kingdom only", "(uk)", "- uk", ", uk", ", united kingdom"]):
            remote_scope = "country_restricted"
            remote_country = "GB"
        elif any(w in raw_lower for w in ["au only", "australia only", "(au)", "- au", ", au", ", australia"]):
            remote_scope = "country_restricted"
            remote_country = "AU"
        elif any(w in raw_lower for w in ["eu only", "europe only", "(eu)", "- eu", ", eu", ", europe"]):
            remote_scope = "regional"
        elif raw_lower in ["remote", "fully remote", "remote - anywhere", "100% remote"]:
            remote_scope = "worldwide"
        else:
            # Check if a specific country alias is present in the remote string
            for alias, code in COUNTRY_ALIAS_MAP.items():
                if len(alias) > 2 and re.search(r"\b" + re.escape(alias) + r"\b", raw_lower):
                    remote_scope = "country_restricted"
                    remote_country = code
                    break

    # 2. Check for Ambiguous Cities without qualifying country or state context
    parts = [p.strip() for p in re.split(r"[,/|;]+", raw_clean) if p.strip()]
    city_candidate = parts[0].lower() if parts else ""

    if city_candidate in AMBIGUOUS_CITIES and len(parts) == 1:
        return NormalizedLocation(
            raw_input=raw_clean,
            is_remote=is_remote,
            remote_scope=remote_scope,
            remote_restricted_country=remote_country,
            is_ambiguous=True,
            confidence=0.3
        )

    # 3. Country and State/Province Resolution
    detected_country_code = None
    detected_state_province = None
    detected_city = None

    # Case 1: Single element string (e.g. standalone "CA" -> Canada, "US" -> United States, "California" -> California, US)
    if len(parts) == 1:
        part_clean = parts[0].lower()
        if part_clean in COUNTRY_ALIAS_MAP:
            detected_country_code = COUNTRY_ALIAS_MAP[part_clean]
        elif part_clean in US_STATE_NAMES_TO_CODE:
            detected_country_code = "US"
            detected_state_province = US_STATES[US_STATE_NAMES_TO_CODE[part_clean].lower()]
        elif part_clean in UNAMBIGUOUS_CITIES:
            city_country, city_name = UNAMBIGUOUS_CITIES[part_clean]
            detected_city = city_name
            detected_country_code = city_country

    # Case 2: Multi-part strings (e.g. "San Francisco, CA", "Toronto, ON, Canada", "Sydney, NSW", "Cambridge, UK")
    elif len(parts) >= 2:
        last_part = parts[-1].lower()
        second_last = parts[-2].lower() if len(parts) >= 2 else ""
        first_part = parts[0]

        last_is_country = False
        if last_part in COUNTRY_ALIAS_MAP:
            # In "City, CA" (len=2), 'CA' is US State California, unless first part is a known Canadian city
            if last_part == "ca" and len(parts) == 2:
                if first_part.lower() in ["toronto", "vancouver", "montreal", "ottawa", "calgary", "edmonton"]:
                    detected_country_code = "CA"
                    last_is_country = True
                else:
                    detected_country_code = "US"
                    detected_state_province = "California"
                    detected_city = first_part
            else:
                detected_country_code = COUNTRY_ALIAS_MAP[last_part]
                last_is_country = True

        if last_is_country:
            # Resolve state/province from second_last if applicable
            if detected_country_code == "US":
                if second_last in US_STATES:
                    detected_state_province = US_STATES[second_last]
                elif second_last in US_STATE_NAMES_TO_CODE:
                    detected_state_province = second_last.title()
                detected_city = first_part
            elif detected_country_code == "CA":
                if second_last in CA_PROVINCES:
                    detected_state_province = CA_PROVINCES[second_last]
                elif second_last in CA_PROVINCE_NAMES_TO_CODE:
                    detected_state_province = second_last.title()
                detected_city = first_part
            elif detected_country_code == "AU":
                if second_last in AU_STATES:
                    detected_state_province = AU_STATES[second_last]
                elif second_last in AU_STATE_NAMES_TO_CODE:
                    detected_state_province = second_last.title()
                detected_city = first_part
            elif detected_country_code == "GB":
                detected_city = first_part
        else:
            # Last part is not explicit country, check if it is state/province/subdivision
            if last_part in US_STATES:
                detected_country_code = "US"
                detected_state_province = US_STATES[last_part]
                detected_city = first_part
            elif last_part in US_STATE_NAMES_TO_CODE:
                detected_country_code = "US"
                detected_state_province = last_part.title()
                detected_city = first_part
            elif last_part in CA_PROVINCES:
                detected_country_code = "CA"
                detected_state_province = CA_PROVINCES[last_part]
                detected_city = first_part
            elif last_part in AU_STATES:
                detected_country_code = "AU"
                detected_state_province = AU_STATES[last_part]
                detected_city = first_part
            elif last_part in UK_SUBDIVISIONS or last_part == "uk":
                detected_country_code = "GB"
                detected_city = first_part
            elif first_part.lower() in UNAMBIGUOUS_CITIES:
                city_country, city_name = UNAMBIGUOUS_CITIES[first_part.lower()]
                detected_city = city_name
                detected_country_code = city_country

    # 4. If country still undetected, check word boundaries across full string
    if not detected_country_code:
        for alias, code in sorted(COUNTRY_ALIAS_MAP.items(), key=lambda x: len(x[0]), reverse=True):
            if len(alias) > 2 and re.search(r"\b" + re.escape(alias) + r"\b", raw_lower):
                detected_country_code = code
                break

    # 5. Check unambiguous cities if city still missing
    if not detected_city:
        for part in parts:
            part_clean = part.lower().strip()
            if part_clean in UNAMBIGUOUS_CITIES:
                city_country, city_name = UNAMBIGUOUS_CITIES[part_clean]
                detected_city = city_name
                if not detected_country_code:
                    detected_country_code = city_country
                break

    # If remote country was found but no general country, link them
    if not detected_country_code and remote_country:
        detected_country_code = remote_country

    # 6. Region Resolution
    detected_region = None
    is_eu = False
    if detected_country_code:
        if detected_country_code in EU_27_COUNTRY_CODES:
            is_eu = True
            detected_region = "Europe"
        elif detected_country_code in EUROPE_COUNTRY_CODES:
            detected_region = "Europe"
        elif detected_country_code in ANZ_COUNTRY_CODES:
            detected_region = "ANZ"
        elif detected_country_code in APAC_COUNTRY_CODES:
            detected_region = "APAC"
        elif detected_country_code in NORTH_AMERICA_COUNTRY_CODES:
            detected_region = "North America"
        elif detected_country_code in LATAM_COUNTRY_CODES:
            detected_region = "LATAM"
        elif detected_country_code in MENA_COUNTRY_CODES:
            detected_region = "MENA"

    country_name = COUNTRY_NAME_BY_CODE.get(detected_country_code) if detected_country_code else None
    confidence = 1.0 if detected_country_code else (0.8 if is_remote else 0.0)

    return NormalizedLocation(
        raw_input=raw_clean,
        country_code=detected_country_code,
        country_name=country_name,
        region=detected_region,
        is_eu=is_eu,
        city=detected_city,
        state_province=detected_state_province,
        is_remote=is_remote,
        remote_scope=remote_scope,
        remote_restricted_country=remote_country,
        is_ambiguous=False,
        confidence=confidence,
    )


# ── Canonical Matching Evaluation ───────────────────────────────────────

def evaluate_location_match(
    raw_location: Optional[str],
    policy: LocationTargetingPolicy,
    candidate_authorization: Optional[List[str]] = None,
    job_work_mode: Optional[str] = None,
    candidate_relocation: Optional[str] = None,
    candidate_sponsorship_now: Optional[str] = None
) -> LocationMatchResult:
    """
    Single canonical location evaluation function for HuntFlow.

    Evaluation steps:
    1. Normalization
    2. Work Mode check (Remote / Hybrid / Onsite)
    3. Explicit Exclusion check (Exclusions win over inclusions)
    4. Target Inclusion check (Countries, Regions, Cities, Remote Worldwide)
    5. Legal Work Authorization check (Independent from geographic targeting)
    6. Relocation & Sponsorship constraints
    """
    reasons = []
    norm = normalize_location_string(raw_location)

    # 1. Unknown / Ambiguous Location Check
    if not norm.country_code and not norm.is_remote:
        if norm.is_ambiguous:
            return LocationMatchResult(
                decision="AMBIGUOUS",
                is_eligible=False,
                normalized_location=norm,
                reasons=["Location is ambiguous without qualifying state or country context."]
            )
        if policy.unknown_location_handling == "allow":
            reasons.append("Unknown location permitted by explicit policy.")
            return LocationMatchResult(
                decision="MATCH",
                is_eligible=True,
                matched_rule="unknown_location_allow_policy",
                normalized_location=norm,
                reasons=reasons
            )
        else:
            return LocationMatchResult(
                decision="UNKNOWN",
                is_eligible=False,
                normalized_location=norm,
                reasons=["Location is unknown and conservative policy requires verified geography."]
            )

    # 2. Work Mode Compatibility Check
    if job_work_mode:
        mode_lower = str(job_work_mode).lower().strip()
        if mode_lower == "remote" and policy.remote_mode == "deny":
            return LocationMatchResult(
                decision="BLOCKED_WORK_MODE",
                is_eligible=False,
                normalized_location=norm,
                reasons=["Job is remote but policy denies remote positions."]
            )
        elif mode_lower == "hybrid" and not policy.allow_hybrid:
            return LocationMatchResult(
                decision="BLOCKED_WORK_MODE",
                is_eligible=False,
                normalized_location=norm,
                reasons=["Job is hybrid but policy does not allow hybrid positions."]
            )
        elif mode_lower == "onsite" and not policy.allow_onsite:
            return LocationMatchResult(
                decision="BLOCKED_WORK_MODE",
                is_eligible=False,
                normalized_location=norm,
                reasons=["Job is onsite but policy does not allow onsite positions."]
            )

    # 3. Explicit Exclusion Rules (Exclusion Wins)
    if policy.excluded_locations:
        for exc in policy.excluded_locations:
            exc_clean = exc.strip().lower()
            if not exc_clean:
                continue

            # Check country exclusion
            exc_country_code = COUNTRY_ALIAS_MAP.get(exc_clean)
            if exc_country_code and norm.country_code == exc_country_code:
                return LocationMatchResult(
                    decision="EXCLUDED",
                    is_eligible=False,
                    excluded_rule=exc,
                    normalized_location=norm,
                    reasons=[f"Location matches explicitly excluded country: '{exc}' ({exc_country_code})."]
                )

            # Check region exclusion (e.g. exclude Europe, exclude Germany inside Europe)
            if exc_clean in REGION_MAP:
                region_codes = REGION_MAP[exc_clean]
                if norm.country_code in region_codes:
                    return LocationMatchResult(
                        decision="EXCLUDED",
                        is_eligible=False,
                        excluded_rule=exc,
                        normalized_location=norm,
                        reasons=[f"Location matches explicitly excluded region: '{exc}'."]
                    )

            # Check city exclusion
            if norm.city and norm.city.lower() == exc_clean:
                return LocationMatchResult(
                    decision="EXCLUDED",
                    is_eligible=False,
                    excluded_rule=exc,
                    normalized_location=norm,
                    reasons=[f"Location matches explicitly excluded city: '{exc}'."]
                )

            # Direct string inclusion in raw location
            if exc_clean in norm.raw_input.lower():
                return LocationMatchResult(
                    decision="EXCLUDED",
                    is_eligible=False,
                    excluded_rule=exc,
                    normalized_location=norm,
                    reasons=[f"Location raw text matches excluded keyword: '{exc}'."]
                )

    # 4. Inclusion Rules Evaluation
    is_included = False
    matched_rule = None

    # Handle Search Worldwide override
    if getattr(policy, "search_worldwide", False):
        is_included = True
        matched_rule = "search_worldwide"
        reasons.append("Worldwide search active: all non-excluded geographies matched.")

    # Handle Remote Worldwide
    if not is_included and norm.is_remote and norm.remote_scope == "worldwide":
        if policy.remote_mode in ["allow_all", "worldwide_only"]:
            is_included = True
            matched_rule = "remote_worldwide"
            reasons.append("Remote worldwide location matches remote policy.")
        elif any(inc.strip().lower() in ["worldwide", "remote", "anywhere", "global"] for inc in policy.included_locations):
            is_included = True
            matched_rule = "included_worldwide"
            reasons.append("Remote worldwide matched explicit worldwide inclusion.")

    # Check Included Locations List
    if not is_included and policy.included_locations:
        for inc in policy.included_locations:
            inc_clean = inc.strip().lower()
            if not inc_clean:
                continue

            # Worldwide / Global wildcard
            if inc_clean in ["worldwide", "global", "anywhere", "unrestricted"]:
                is_included = True
                matched_rule = inc
                reasons.append(f"Matched global target inclusion: '{inc}'.")
                break

            # Country match (e.g. "Australia", "US", "New Zealand")
            inc_country_code = COUNTRY_ALIAS_MAP.get(inc_clean)
            if inc_country_code and norm.country_code == inc_country_code:
                is_included = True
                matched_rule = inc
                reasons.append(f"Matched target country: '{inc}' ({inc_country_code}).")
                break

            # Region match (e.g. "Europe", "EU", "APAC", "ANZ")
            if inc_clean in REGION_MAP:
                region_codes = REGION_MAP[inc_clean]
                if norm.country_code and norm.country_code in region_codes:
                    is_included = True
                    matched_rule = inc
                    reasons.append(f"Country {norm.country_code} is in target region '{inc}'.")
                    break

            # State / Province match (e.g. "California", "Texas", "Ontario", "NSW")
            if norm.state_province:
                if norm.state_province.lower() == inc_clean:
                    is_included = True
                    matched_rule = inc
                    reasons.append(f"Matched target state/province: '{inc}'.")
                    break
                if inc_clean in US_STATES and US_STATES[inc_clean].lower() == norm.state_province.lower():
                    is_included = True
                    matched_rule = inc
                    reasons.append(f"Matched target state abbreviation: '{inc}'.")
                    break
                if inc_clean in CA_PROVINCES and CA_PROVINCES[inc_clean].lower() == norm.state_province.lower():
                    is_included = True
                    matched_rule = inc
                    reasons.append(f"Matched target Canadian province: '{inc}'.")
                    break
                if inc_clean in AU_STATES and AU_STATES[inc_clean].lower() == norm.state_province.lower():
                    is_included = True
                    matched_rule = inc
                    reasons.append(f"Matched target Australian state: '{inc}'.")
                    break

            # City match (e.g. "Sydney", "London", "New York")
            if norm.city and norm.city.lower() == inc_clean:
                is_included = True
                matched_rule = inc
                reasons.append(f"Matched target city: '{inc}'.")
                break

            # Raw string match
            if inc_clean in norm.raw_input.lower():
                is_included = True
                matched_rule = inc
                reasons.append(f"Matched raw location string for '{inc}'.")
                break
    elif not is_included and not policy.included_locations:
        # If no included locations specified at all, default to match unless restricted by policy
        is_included = True
        matched_rule = "default_unrestricted"
        reasons.append("No specific included locations configured; matching by default.")

    if not is_included:
        return LocationMatchResult(
            decision="NO_MATCH",
            is_eligible=False,
            normalized_location=norm,
            reasons=[f"Location '{norm.raw_input}' ({norm.country_name or norm.country_code or 'Unknown'}) does not match target locations."]
        )

    # 5. Work Authorization & Sponsorship Evaluation (Separate from Search Geography)
    authorization_status = "CONFIRMED_AUTHORIZED"
    sponsorship_status = "NO_SPONSORSHIP_NEEDED"
    warnings = []

    if candidate_authorization is not None and norm.country_code:
        auth_normalized = []
        for a in candidate_authorization:
            a_clean = str(a).strip().lower()
            code = COUNTRY_ALIAS_MAP.get(a_clean)
            if code:
                auth_normalized.append(code)
            elif a_clean in ["europe", "eu"]:
                # Note: broad "EUROPE" tag is recorded but not expanded to all individual countries for legal safety
                auth_normalized.append(a_clean.upper())

        is_authorized = (norm.country_code in auth_normalized and norm.country_code not in ["EUROPE", "EU"])
        
        # If job is remote worldwide and candidate doesn't need specific local visa
        if not is_authorized and norm.is_remote and norm.remote_scope == "worldwide":
            is_authorized = True

        if candidate_sponsorship_now == "yes":
            sponsorship_status = "SPONSORSHIP_REQUIRED"

        if not is_authorized:
            if candidate_sponsorship_now == "yes":
                authorization_status = "SPONSORSHIP_REQUIRED"
                warnings.append(f"Work authorization for {norm.country_name or norm.country_code} is not confirmed; candidate requires visa sponsorship.")
            else:
                authorization_status = "NOT_CURRENTLY_AUTHORIZED"
                warnings.append(f"Candidate does not currently hold confirmed work authorization for {norm.country_name or norm.country_code}.")

            app_strategy = getattr(policy, "application_strategy", "INTERVIEW_FIRST_SPONSORSHIP")
            if app_strategy == "STRICT_AUTHORIZATION":
                return LocationMatchResult(
                    decision="BLOCKED_AUTHORIZATION",
                    is_eligible=False,
                    matched_rule=matched_rule,
                    normalized_location=norm,
                    authorization_status=authorization_status,
                    sponsorship_status=sponsorship_status,
                    warnings=warnings,
                    reasons=[f"Strict policy: Candidate is not legally authorized to work in {norm.country_name or norm.country_code}."]
                )
            else:
                # INTERVIEW_FIRST_SPONSORSHIP mode:
                # Does NOT block! Allows truthful continuation
                reasons.append(f"Interview-First Strategy: Job in {norm.country_name or norm.country_code} permitted under sponsorship-seeking mode.")
        else:
            authorization_status = "CONFIRMED_AUTHORIZED"
            reasons.append(f"Candidate holds verified work authorization for {norm.country_name or norm.country_code}.")

    # 6. Relocation Check
    if candidate_relocation == "no" and norm.country_code and norm.country_code not in ["PK"] and not norm.is_remote:
        pass

    return LocationMatchResult(
        decision="MATCH",
        is_eligible=True,
        matched_rule=matched_rule,
        normalized_location=norm,
        authorization_status=authorization_status,
        sponsorship_status=sponsorship_status,
        warnings=warnings,
        reasons=reasons
    )
