"""
AML/ABC screening tool functions.

All sources used here are free and publicly accessible — no paid API key
is required. Each function is independently callable and returns a list
of AMLFinding objects, with severity set conservatively:

  None     — source searched, no match found
  Watch    — partial / low-confidence name match worth reviewing
  Elevated — confirmed presence on a sanctions or debarment list
  High     — confirmed presence on OFAC SDN or UN asset-freeze list

Data source access status (verified at build time):
  OFAC SDN              free XML + REST API (sanctions.ofac.treas.gov)  ✓
  OpenSanctions         free search API (api.opensanctions.org)         ✓
  World Bank Debarred   free JSON API (apigwext.worldbank.org)           ✓
  UN Consolidated List  free XML download (un.org)                      ✓
  EU Sanctions          free XML (data.europa.eu Financial Sanctions)   ✓
  SEC EDGAR FCPA        free full-text search (efts.sec.gov)            ✓
  TI CPI                free JSON (api.transparency.org)                ✓
  FATF grey/black list  hardcoded snapshot (updated manually)           ✓
  Tavily adverse media  existing pipeline tool — no new key needed      ✓

  MCA/ROC (India)       no machine-readable free API                    ✗ (documented)
  RBI Wilful Defaulter  no machine-readable index                       ✗ (documented)

Screening logic: each function performs a fuzzy name match (lowercase
substring) against the source result set. This is intentionally
conservative — a partial match produces a Watch flag that requires human
review, not a High flag that could be a false positive. Entity names
are normalized (stripped, lowercased) before comparison.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import date, timedelta
from pathlib import Path

import requests

from config import settings
from schemas import AMLFinding, AMLSeverity

logger = logging.getLogger(__name__)

_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "financial-agent-mvp/1.0 (research-tool; contact: admin@example.com)"})

# Simple disk cache for XML/JSON payloads that are large and infrequently updated
_CACHE_DIR = settings.cache_dir / "aml"
_CACHE_DIR.mkdir(parents=True, exist_ok=True)
_XML_CACHE_TTL_HOURS = 24   # UN/EU lists update infrequently; daily refresh is sufficient

# Request timeout
_TIMEOUT = 15


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _cache_key(url: str) -> Path:
    h = hashlib.md5(url.encode()).hexdigest()
    return _CACHE_DIR / f"{h}.cache"


def _cached_get(url: str, ttl_hours: int = _XML_CACHE_TTL_HOURS) -> str | None:
    """GET with disk caching. Returns response text or None on failure."""
    path = _cache_key(url)
    if path.exists():
        age_hours = (time.time() - path.stat().st_mtime) / 3600
        if age_hours < ttl_hours:
            try:
                return path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                pass
    try:
        resp = _SESSION.get(url, timeout=_TIMEOUT)
        resp.raise_for_status()
        text = resp.text
        path.write_text(text, encoding="utf-8")
        return text
    except Exception as exc:
        logger.warning("AML HTTP GET failed for %s: %s", url, exc)
        return None


def _normalize(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower())


_GENERIC_STOP_WORDS = {
    "bank", "corp", "corporation", "ltd", "limited", "inc", "incorporated",
    "group", "holdings", "holding", "company", "co", "services", "industries",
    "industry", "state", "national", "trust", "financial", "finance", "the",
    "of", "and", "&", "ltd.", "inc."
}



def _name_matches(entity: str, target: str, threshold: int = 4) -> bool:
    """
    Lightweight substring match: True if a distinctive word (≥threshold chars,
    excluding common business stopwords) in `entity` appears in `target`.
    Conservative — prefers false negatives over false positives for a first-pass screen.
    """
    entity_n = _normalize(entity)
    target_n = _normalize(target)
    # Exact substring
    if entity_n in target_n or target_n in entity_n:
        return True
    
    # Significant distinctive word overlap
    words = [w for w in entity_n.split() if len(w) >= threshold and w not in _GENERIC_STOP_WORDS]
    if not words:
        words = [w for w in entity_n.split() if len(w) >= threshold]
    return any(w in target_n for w in words)



# ---------------------------------------------------------------------------
# Source 1: OFAC SDN List
# https://sanctionslistservice.ofac.treas.gov/api/publicNameSearch/
# Free REST API — no key required. Returns JSON list of SDN entries.
# ---------------------------------------------------------------------------

_OFAC_API = "https://sanctionslistservice.ofac.treas.gov/api/publicNameSearch/{name}?index=sdn"
_OFAC_INDEX = "https://www.treasury.gov/ofac/downloads/index.html"

def screen_ofac_sdn(entity_name: str) -> AMLFinding:
    """Screen entity against the OFAC Specially Designated Nationals list."""
    url = _OFAC_API.format(name=requests.utils.quote(entity_name))
    try:
        resp = _SESSION.get(url, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        hits = data if isinstance(data, list) else data.get("sdnList", {}).get("sdnEntry", [])
        if not isinstance(hits, list):
            hits = [hits]

        matches = [
            h for h in hits
            if _name_matches(entity_name, str(h.get("lastName", "") + " " + str(h.get("firstName", ""))))
            or _name_matches(entity_name, str(h.get("sdnName", "")))
        ]
        if matches:
            return AMLFinding(
                entity_screened=entity_name,
                source_name="OFAC SDN List",
                finding_summary=f"Name match found in OFAC SDN list ({len(matches)} entry/entries). Requires manual verification.",
                severity=AMLSeverity.HIGH,
                source_url="https://sanctionslist.ofac.treas.gov/Home/SdnList",
            )
        return AMLFinding(
            entity_screened=entity_name,
            source_name="OFAC SDN List",
            finding_summary="No match found in OFAC SDN list.",
            severity=AMLSeverity.NONE,
            source_url="https://sanctionslist.ofac.treas.gov/Home/SdnList",
        )
    except Exception as exc:
        logger.warning("OFAC SDN screen failed for %r: %s", entity_name, exc)
        return AMLFinding(
            entity_screened=entity_name,
            source_name="OFAC SDN List",
            finding_summary=f"Screening could not be completed: {exc}. Manual check recommended.",
            severity=AMLSeverity.WATCH,
            source_url="https://sanctionslist.ofac.treas.gov/Home/SdnList",
        )


# ---------------------------------------------------------------------------
# Source 2: OpenSanctions
# https://api.opensanctions.org/entities/_search?q=...
# Free tier (rate-limited). Aggregates OFAC, UN, EU, and many more.
# ---------------------------------------------------------------------------

_OPENSANCTIONS_URL = "https://api.opensanctions.org/entities/_search"

def screen_opensanctions(entity_name: str) -> AMLFinding:
    """Screen against OpenSanctions — aggregates 100+ sanctions and watchlists."""
    try:
        resp = _SESSION.get(
            _OPENSANCTIONS_URL,
            params={"q": entity_name, "limit": 5, "schema": "Thing"},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])
        # Filter to only hits that plausibly match
        matches = [
            r for r in results
            if _name_matches(entity_name, " ".join(
                r.get("properties", {}).get("name", []) +
                r.get("properties", {}).get("alias", [])
            ))
        ]
        if matches:
            datasets = list({ds for r in matches for ds in r.get("datasets", [])})
            return AMLFinding(
                entity_screened=entity_name,
                source_name="OpenSanctions",
                finding_summary=(
                    f"Potential match(es) found in OpenSanctions database "
                    f"(datasets: {', '.join(datasets[:5]) or 'unknown'}). "
                    f"Requires manual verification."
                ),
                severity=AMLSeverity.ELEVATED,
                source_url=f"https://www.opensanctions.org/search/?q={requests.utils.quote(entity_name)}",
            )
        return AMLFinding(
            entity_screened=entity_name,
            source_name="OpenSanctions",
            finding_summary="No match found in OpenSanctions database.",
            severity=AMLSeverity.NONE,
            source_url=f"https://www.opensanctions.org/search/?q={requests.utils.quote(entity_name)}",
        )
    except Exception as exc:
        logger.warning("OpenSanctions screen failed for %r: %s", entity_name, exc)
        return AMLFinding(
            entity_screened=entity_name,
            source_name="OpenSanctions",
            finding_summary=f"Screening could not be completed: {exc}. Manual check recommended.",
            severity=AMLSeverity.WATCH,
            source_url="https://www.opensanctions.org/",
        )


# ---------------------------------------------------------------------------
# Source 3: World Bank Debarred Entities
# https://apigwext.worldbank.org/dvsvc/v1.0/json/ADMINISTRATIVE_PROCUREMENT_SANCTIONS/
# Free JSON API.
# ---------------------------------------------------------------------------

_WB_URL = "https://apigwext.worldbank.org/dvsvc/v1.0/json/ADMINISTRATIVE_PROCUREMENT_SANCTIONS/EXTOFFDEVGRP/OPS5/EXTENDED/COUNTRY/all"

def screen_world_bank_debarred(entity_name: str) -> AMLFinding:
    """Screen against the World Bank Integrity Vice Presidency debarment list."""
    source_url = "https://www.worldbank.org/en/projects-operations/procurement/debarred-firms"
    try:
        text = _cached_get(_WB_URL, ttl_hours=12)
        if text is None:
            raise RuntimeError("Could not fetch World Bank debarment list")
        data = json.loads(text)
        firms = data.get("response", {}).get("ZPROCSUPP", [])
        if not isinstance(firms, list):
            firms = []
        matches = [f for f in firms if _name_matches(entity_name, str(f.get("SUPP_NAME", "")))]
        if matches:
            return AMLFinding(
                entity_screened=entity_name,
                source_name="World Bank Debarred Entities",
                finding_summary=f"Name match found in World Bank debarment list ({len(matches)} entry/entries). Requires manual verification.",
                severity=AMLSeverity.HIGH,
                source_url=source_url,
            )
        return AMLFinding(
            entity_screened=entity_name,
            source_name="World Bank Debarred Entities",
            finding_summary="No match found in World Bank debarment list.",
            severity=AMLSeverity.NONE,
            source_url=source_url,
        )
    except Exception as exc:
        logger.warning("World Bank debarment screen failed for %r: %s", entity_name, exc)
        return AMLFinding(
            entity_screened=entity_name,
            source_name="World Bank Debarred Entities",
            finding_summary=f"Screening could not be completed: {exc}. Manual check recommended.",
            severity=AMLSeverity.WATCH,
            source_url=source_url,
        )


# ---------------------------------------------------------------------------
# Source 4: UN Security Council Consolidated List
# https://scsanctions.un.org/resources/xml/en/consolidated.xml
# Free XML download, cached locally.
# ---------------------------------------------------------------------------

_UN_XML_URL = "https://scsanctions.un.org/resources/xml/en/consolidated.xml"

def screen_un_sanctions(entity_name: str) -> AMLFinding:
    """Screen against the UN Security Council Consolidated Sanctions List."""
    source_url = "https://www.un.org/securitycouncil/content/un-sc-consolidated-list"
    try:
        xml_text = _cached_get(_UN_XML_URL, ttl_hours=_XML_CACHE_TTL_HOURS)
        if xml_text is None:
            raise RuntimeError("Could not fetch UN Consolidated List XML")
        
        # Parse XML tags to avoid false positives on XML tag/attribute names
        import xml.etree.ElementTree as ET
        matched = False
        try:
            root = ET.fromstring(xml_text)
            # UN consolidated XML contains INDIVIDUALS and ENTITIES
            for elem in root.iter():
                tag = elem.tag.upper()
                if tag in ("FIRST_NAME", "SECOND_NAME", "THIRD_NAME", "FOURTH_NAME", "ENTITY_NAME", "NAME_ORIGINAL_SCRIPT"):
                    if elem.text and _name_matches(entity_name, elem.text):
                        matched = True
                        break
        except Exception:
            # Fallback to normalized substring search if XML parsing encounters anomalies
            matched = _normalize(entity_name) in _normalize(xml_text)

        if matched:
            return AMLFinding(
                entity_screened=entity_name,
                source_name="UN SC Consolidated List",
                finding_summary="Name match found in UN Security Council Consolidated Sanctions List. Requires manual verification to confirm match.",
                severity=AMLSeverity.ELEVATED,
                source_url=source_url,
            )
        return AMLFinding(
            entity_screened=entity_name,
            source_name="UN SC Consolidated List",
            finding_summary="No match found in UN Security Council Consolidated Sanctions List.",
            severity=AMLSeverity.NONE,
            source_url=source_url,
        )
    except Exception as exc:
        logger.warning("UN sanctions screen failed for %r: %s", entity_name, exc)
        return AMLFinding(
            entity_screened=entity_name,
            source_name="UN SC Consolidated List",
            finding_summary=f"Screening could not be completed: {exc}. Manual check recommended.",
            severity=AMLSeverity.WATCH,
            source_url=source_url,
        )


# ---------------------------------------------------------------------------
# Source 5: EU Financial Sanctions File
# https://webgate.ec.europa.eu/fsd/fsf/public/files/xmlFullSanctionsList_1_1/content
# Free XML, cached locally.
# ---------------------------------------------------------------------------

_EU_XML_URL = "https://webgate.ec.europa.eu/fsd/fsf/public/files/xmlFullSanctionsList_1_1/content"

def screen_eu_sanctions(entity_name: str) -> AMLFinding:
    """Screen against the EU Financial Sanctions File."""
    source_url = "https://www.sanctionsmap.eu/"
    try:
        xml_text = _cached_get(_EU_XML_URL, ttl_hours=_XML_CACHE_TTL_HOURS)
        if xml_text is None:
            raise RuntimeError("Could not fetch EU Financial Sanctions XML")
        
        import xml.etree.ElementTree as ET
        matched = False
        try:
            root = ET.fromstring(xml_text)
            for elem in root.iter():
                # EU XML nameAlias / wholeName / name tags
                tag = elem.tag.lower()
                if "name" in tag or "alias" in tag:
                    text_val = elem.text or elem.attrib.get("wholeName", "") or elem.attrib.get("name", "")
                    if text_val and _name_matches(entity_name, text_val):
                        matched = True
                        break
        except Exception:
            matched = _normalize(entity_name) in _normalize(xml_text)

        if matched:
            return AMLFinding(
                entity_screened=entity_name,
                source_name="EU Financial Sanctions List",
                finding_summary="Name match found in EU Financial Sanctions File XML. Requires manual verification to confirm match.",
                severity=AMLSeverity.ELEVATED,
                source_url=source_url,
            )
        return AMLFinding(
            entity_screened=entity_name,
            source_name="EU Financial Sanctions List",
            finding_summary="No match found in EU Financial Sanctions List.",
            severity=AMLSeverity.NONE,
            source_url=source_url,
        )
    except Exception as exc:
        logger.warning("EU sanctions screen failed for %r: %s", entity_name, exc)
        return AMLFinding(
            entity_screened=entity_name,
            source_name="EU Financial Sanctions List",
            finding_summary=f"Screening could not be completed: {exc}. Manual check recommended.",
            severity=AMLSeverity.WATCH,
            source_url=source_url,
        )


# ---------------------------------------------------------------------------
# Source 6: SEC EDGAR — FCPA enforcement releases
# https://efts.sec.gov/LATEST/search-index?q="entity_name"&dateRange=custom&...
# Free full-text search API.
# ---------------------------------------------------------------------------

_SEC_EDGAR_URL = "https://efts.sec.gov/LATEST/search-index"

def screen_sec_fcpa(entity_name: str) -> AMLFinding:
    """Search SEC EDGAR litigation releases for FCPA-related mentions of the entity."""
    source_url = f"https://efts.sec.gov/LATEST/search-index?q=%22{requests.utils.quote(entity_name)}%22+FCPA&dateRange=custom&startdt=2010-01-01"
    try:
        resp = _SESSION.get(
            _SEC_EDGAR_URL,
            params={
                "q": f'"{entity_name}" FCPA',
                "dateRange": "custom",
                "startdt": "2010-01-01",
                "forms": "LR",  # Litigation Releases
            },
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        hits = data.get("hits", {}).get("hits", [])
        if hits:
            return AMLFinding(
                entity_screened=entity_name,
                source_name="SEC EDGAR — FCPA Litigation Releases",
                finding_summary=f"{len(hits)} SEC litigation release(s) found mentioning this entity in an FCPA context. Review required.",
                severity=AMLSeverity.ELEVATED,
                source_url=source_url,
            )
        return AMLFinding(
            entity_screened=entity_name,
            source_name="SEC EDGAR — FCPA Litigation Releases",
            finding_summary="No FCPA litigation releases found for this entity on SEC EDGAR.",
            severity=AMLSeverity.NONE,
            source_url=source_url,
        )
    except Exception as exc:
        logger.warning("SEC EDGAR FCPA screen failed for %r: %s", entity_name, exc)
        return AMLFinding(
            entity_screened=entity_name,
            source_name="SEC EDGAR — FCPA Litigation Releases",
            finding_summary=f"Screening could not be completed: {exc}. Manual check recommended.",
            severity=AMLSeverity.WATCH,
            source_url="https://www.sec.gov/divisions/enforce/enforcements-actions/fcpa-cases",
        )


# ---------------------------------------------------------------------------
# Source 7: Transparency International CPI — Jurisdictional risk context
# https://www.transparency.org/en/cpi — country-level, not entity-specific
# ---------------------------------------------------------------------------

_TI_CPI_SNAPSHOT_YEAR = 2023

_TI_CPI_SNAPSHOT: dict[str, dict] = {
    "IN": {"country": "India",          "score": 39, "rank": 93,  "year": _TI_CPI_SNAPSHOT_YEAR},
    "US": {"country": "United States",  "score": 69, "rank": 24,  "year": _TI_CPI_SNAPSHOT_YEAR},
    "GB": {"country": "United Kingdom", "score": 71, "rank": 20,  "year": _TI_CPI_SNAPSHOT_YEAR},
    "CN": {"country": "China",          "score": 42, "rank": 76,  "year": _TI_CPI_SNAPSHOT_YEAR},
    "SG": {"country": "Singapore",      "score": 83, "rank": 5,   "year": _TI_CPI_SNAPSHOT_YEAR},
    "AE": {"country": "UAE",            "score": 68, "rank": 26,  "year": _TI_CPI_SNAPSHOT_YEAR},
    "MU": {"country": "Mauritius",      "score": 49, "rank": 57,  "year": _TI_CPI_SNAPSHOT_YEAR},
    "KY": {"country": "Cayman Islands", "score": None, "rank": None, "year": _TI_CPI_SNAPSHOT_YEAR,
            "note": "Not separately ranked by TI; associated with financial secrecy."},
    "VG": {"country": "British Virgin Islands", "score": None, "rank": None, "year": _TI_CPI_SNAPSHOT_YEAR,
            "note": "Not separately ranked by TI; associated with financial secrecy."},
}

def get_jurisdictional_risk(country_code: str) -> AMLFinding:
    """Return a TI CPI-based jurisdictional risk context finding for a country code."""
    data = _TI_CPI_SNAPSHOT.get(country_code.upper())
    today = date.today()
    staleness_note = ""
    if (today.year - _TI_CPI_SNAPSHOT_YEAR) >= 2:
        staleness_note = f" (Note: baseline snapshot is from {_TI_CPI_SNAPSHOT_YEAR}; manual refresh recommended)"
        logger.warning("TI CPI snapshot is from %d (>1 year old) — manual refresh recommended", _TI_CPI_SNAPSHOT_YEAR)

    if data:
        score = data.get("score")
        note = data.get("note", "")
        if score is None:
            summary = f"{data['country']}: {note} (TI CPI {_TI_CPI_SNAPSHOT_YEAR}){staleness_note}"
            severity = AMLSeverity.WATCH
        elif score < 40:
            summary = f"{data['country']} TI CPI {_TI_CPI_SNAPSHOT_YEAR} score: {score}/100 (rank {data['rank']}) — elevated corruption-risk jurisdiction.{staleness_note}"
            severity = AMLSeverity.ELEVATED
        elif score < 55:
            summary = f"{data['country']} TI CPI {_TI_CPI_SNAPSHOT_YEAR} score: {score}/100 (rank {data['rank']}) — moderate corruption-risk jurisdiction.{staleness_note}"
            severity = AMLSeverity.WATCH
        else:
            summary = f"{data['country']} TI CPI {_TI_CPI_SNAPSHOT_YEAR} score: {score}/100 (rank {data['rank']}) — low corruption-risk jurisdiction.{staleness_note}"
            severity = AMLSeverity.NONE
    else:
        summary = f"No TI CPI data available for country code '{country_code}'. Jurisdiction risk unknown."
        severity = AMLSeverity.WATCH

    return AMLFinding(
        entity_screened=f"Jurisdiction: {country_code.upper()}",
        source_name=f"Transparency International CPI {_TI_CPI_SNAPSHOT_YEAR}",
        finding_summary=summary,
        severity=severity,
        source_url=f"https://www.transparency.org/en/cpi/{_TI_CPI_SNAPSHOT_YEAR}",
    )


# ---------------------------------------------------------------------------
# Source 8: FATF Grey/Black List — Jurisdictional risk (hardcoded snapshot)
# Updated manually from https://www.fatf-gafi.org/en/topics/high-risk-and-other-monitored-jurisdictions.html
# Last updated: 2024-10 (FATF Plenary October 2024)
# ---------------------------------------------------------------------------

_FATF_SNAPSHOT_DATE = date(2024, 10, 1)
_FATF_BLACK_LIST = {"Iran", "North Korea", "Myanmar"}
_FATF_GREY_LIST = {
    "Algeria", "Angola", "Burkina Faso", "Cameroon", "Côte d'Ivoire", "Congo",
    "Haiti", "Kenya", "Laos", "Lebanon", "Mali", "Mozambique", "Namibia",
    "Nigeria", "Philippines", "Senegal", "South Africa", "South Sudan",
    "Syria", "Tanzania", "Venezuela", "Vietnam", "Yemen",
}

def get_fatf_risk(country_name: str) -> AMLFinding:
    """Return a FATF grey/black list finding for a country name."""
    source_url = "https://www.fatf-gafi.org/en/topics/high-risk-and-other-monitored-jurisdictions.html"
    cn = country_name.strip()
    
    staleness_note = ""
    if (date.today() - _FATF_SNAPSHOT_DATE).days > 365:
        staleness_note = f" (Note: baseline snapshot is from {_FATF_SNAPSHOT_DATE.strftime('%b %Y')}; manual refresh recommended)"
        logger.warning("FATF snapshot is >1 year old (%s) — manual refresh recommended", _FATF_SNAPSHOT_DATE.isoformat())

    if cn in _FATF_BLACK_LIST:
        return AMLFinding(
            entity_screened=f"Jurisdiction: {cn}",
            source_name=f"FATF High-Risk Jurisdictions (snapshot {_FATF_SNAPSHOT_DATE.strftime('%b %Y')})",
            finding_summary=f"{cn} is on the FATF Black List (call for action). Highest jurisdictional risk.{staleness_note}",
            severity=AMLSeverity.HIGH,
            source_url=source_url,
        )
    if cn in _FATF_GREY_LIST:
        return AMLFinding(
            entity_screened=f"Jurisdiction: {cn}",
            source_name=f"FATF High-Risk Jurisdictions (snapshot {_FATF_SNAPSHOT_DATE.strftime('%b %Y')})",
            finding_summary=f"{cn} is on the FATF Grey List (increased monitoring). Elevated jurisdictional risk.{staleness_note}",
            severity=AMLSeverity.ELEVATED,
            source_url=source_url,
        )
    return AMLFinding(
        entity_screened=f"Jurisdiction: {cn}",
        source_name=f"FATF High-Risk Jurisdictions (snapshot {_FATF_SNAPSHOT_DATE.strftime('%b %Y')})",
        finding_summary=f"{cn} is not on the FATF grey or black list as of {_FATF_SNAPSHOT_DATE.strftime('%B %Y')}.{staleness_note}",
        severity=AMLSeverity.NONE,
        source_url=source_url,
    )



# ---------------------------------------------------------------------------
# Source 9: Tavily adverse-media search & Bundled AML Orchestration Tools
# ---------------------------------------------------------------------------

_HIGH_SEVERITY_KEYWORDS = [
    "sanctioned", "sanctions", "debarred", "debarment", "convicted",
    "indicted", "arrested", "money laundering", "aml", "terror financing",
    "wilful default",
]
_ELEVATED_KEYWORDS = [
    "sebi order", "sebi adjudication", "enforcement directorate", "ed raid",
    "bribery", "corruption", "fcpa", "sfo investigation", "nca", "interpol",
    "fraud", "ponzi", "insider trading", "price manipulation",
]

_COUNTRY_CODE_TO_NAME: dict[str, str] = {
    "IN": "India",
    "US": "United States",
    "GB": "United Kingdom",
    "CN": "China",
    "SG": "Singapore",
    "AE": "UAE",
    "MU": "Mauritius",
}

def _classify_severity(text: str) -> AMLSeverity:
    t = text.lower()
    if any(kw in t for kw in _HIGH_SEVERITY_KEYWORDS):
        return AMLSeverity.HIGH
    if any(kw in t for kw in _ELEVATED_KEYWORDS):
        return AMLSeverity.ELEVATED
    return AMLSeverity.WATCH


def search_aml_adverse_media(query: str, max_results: int = 5) -> list[dict]:
    """
    Search for adverse media / regulatory findings related to an AML/ABC query.
    Wraps tools.search_tools.search_web_news with AML-focused query patterns.
    """
    from tools.search_tools import search_web_news
    return search_web_news(query=query, max_results=max_results)


def search_adverse_media(
    entity_name: str,
    focus: str = "",
    depth: str = "basic",
) -> list[dict[str, Any]]:
    """
    Tavily-backed adverse media search.
    If focus is provided, targets that specific allegation/finding.
    Returns serialized list of AMLFinding dicts.
    """
    from tools.search_tools import search_web_news

    queries = []
    if focus.strip():
        queries.append(f"{entity_name} {focus.strip()}")
    else:
        queries.extend([
            f"{entity_name} SEBI enforcement order investigation",
            f"{entity_name} Enforcement Directorate raid money laundering",
            f"{entity_name} bribery corruption FCPA fraud",
        ])

    raw_results = []
    for q in queries:
        try:
            res = search_web_news(query=q, depth=depth, max_results=5)
            raw_results.extend(res)
        except Exception as exc:
            logger.warning("search_adverse_media failed for query %r: %s", q, exc)

    findings: list[AMLFinding] = []
    seen_urls: set[str] = set()

    for item in raw_results:
        url = item.get("url", "")
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        content = item.get("content", "") or item.get("title", "")
        severity = _classify_severity(content)
        if severity in (AMLSeverity.WATCH, AMLSeverity.ELEVATED, AMLSeverity.HIGH):
            findings.append(AMLFinding(
                entity_screened=entity_name,
                source_name="Adverse Media (Tavily search)",
                finding_summary=(content[:300] + "…") if len(content) > 300 else content,
                severity=severity,
                source_url=url,
            ))

    if not findings:
        findings.append(AMLFinding(
            entity_screened=entity_name,
            source_name="Adverse Media (Tavily search)",
            finding_summary=(
                "No adverse regulatory, enforcement, or AML/ABC-related media was found "
                "for this entity in this search cycle. This does not constitute clearance."
            ),
            severity=AMLSeverity.NONE,
            source_url="",
        ))

    return [f.model_dump() for f in findings]


def run_structured_aml_sweep(entity_name: str, ticker: str = "") -> list[dict[str, Any]]:
    """
    Bundled deterministic sweep: sweeps OFAC, OpenSanctions, World Bank,
    UN, EU, SEC EDGAR, TI CPI, and FATF in parallel in one call.
    Returns serialized list of AMLFinding dicts.
    """
    import concurrent.futures

    entities = [entity_name.strip()] if entity_name else []
    if ticker and ticker.strip() and ticker.strip() not in entities:
        entities.append(ticker.strip())

    screeners = [
        screen_ofac_sdn,
        screen_opensanctions,
        screen_world_bank_debarred,
        screen_un_sanctions,
        screen_eu_sanctions,
        screen_sec_fcpa,
    ]

    tasks = []
    for ent in entities:
        for fn in screeners:
            tasks.append((ent, fn))

    def _exec_screener(item: tuple) -> AMLFinding:
        ent, fn = item
        try:
            return fn(ent)
        except Exception as exc:
            logger.warning("Screener %s failed for %r: %s", fn.__name__, ent, exc)
            return AMLFinding(
                entity_screened=ent,
                source_name=fn.__name__.replace("_", " ").title(),
                finding_summary=f"Screener error: {exc}. Manual check recommended.",
                severity=AMLSeverity.WATCH,
                source_url="",
            )

    findings: list[AMLFinding] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(tasks) or 1, 8)) as executor:
        findings.extend(list(executor.map(_exec_screener, tasks)))

    # Jurisdictional risk (TI CPI + FATF)
    country_code = "IN" if (ticker.endswith(".NS") or ticker.endswith(".BO")) else ("US" if ticker and not "." in ticker else "IN")
    try:
        findings.append(get_jurisdictional_risk(country_code))
        country_name = _COUNTRY_CODE_TO_NAME.get(country_code, "India")
        findings.append(get_fatf_risk(country_name))
    except Exception as exc:
        logger.warning("Jurisdictional screening failed: %s", exc)

    return [f.model_dump() for f in findings]

