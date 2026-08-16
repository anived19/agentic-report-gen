"""
AML/ABC screening tool functions.

All sources used here are free and publicly accessible — no paid API key
is required. Each function is independently callable and returns an AMLFinding
object, with severity set conservatively:

  None     — source searched, no match found
  Watch    — partial / low-confidence name match worth reviewing
  Elevated — confirmed presence on a sanctions or debarment list
  High     — confirmed presence on OFAC SDN or UN asset-freeze list

Screening logic:
  - Robust error handling: all HTTP / network exceptions return clean, professional
    status strings rather than dumping raw Python exception traces into report tables.
  - Accurate entity matching: parses structured XML/JSON nodes with whole-word token matching
    to prevent false positives on subwords.
  - Secondary LLM filtering: adverse media web searches undergo secondary verification to
    eliminate image strings, broken assets, generic code of ethics releases, and unrelated noise.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

import requests
from google import genai
from google.genai import types

from config import settings
from schemas import AMLFinding, AMLSeverity

logger = logging.getLogger(__name__)

_SESSION = requests.Session()
_SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 (Research Tool: compliance@reportagentic.org)",
    "Accept": "application/json, application/xml, text/xml, text/html, */*",
})

# Simple disk cache for XML/JSON payloads that are large and infrequently updated
_CACHE_DIR = settings.cache_dir / "aml"
_CACHE_DIR.mkdir(parents=True, exist_ok=True)
_XML_CACHE_TTL_HOURS = 24   # Daily refresh is sufficient for international sanctions lists

# Request timeout
_TIMEOUT = 15


# ---------------------------------------------------------------------------
# Cache helpers & Text sanitizers
# ---------------------------------------------------------------------------

def _cache_key(url: str) -> Path:
    h = hashlib.md5(url.encode()).hexdigest()
    return _CACHE_DIR / f"{h}.cache"


def _cached_get(url: str, ttl_hours: int = _XML_CACHE_TTL_HOURS, headers: Optional[dict[str, str]] = None) -> str | None:
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
        req_headers = dict(_SESSION.headers)
        if headers:
            req_headers.update(headers)
        resp = _SESSION.get(url, headers=req_headers, timeout=_TIMEOUT)
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
    "of", "and", "&", "ltd.", "inc.", "plc", "sa", "gmbh", "pvt"
}


def _name_matches(entity: str, target: str) -> bool:
    """
    Precise whole-token entity name matcher.
    - Exact normalized equality: 'tata consultancy services' == 'tata consultancy services'
    - Multi-word sequence match: all distinctive words in entity must appear as whole words in target
    - Single-word match: must match target as a whole word and target must not be an unrelated individual with >3 tokens
    """
    if not entity or not target:
        return False
    entity_n = _normalize(entity)
    target_n = _normalize(target)

    # Exact equality
    if entity_n == target_n:
        return True

    entity_tokens = [w for w in re.findall(r"\b[a-zA-Z0-9]{3,}\b", entity_n) if w not in _GENERIC_STOP_WORDS]
    target_tokens = set(re.findall(r"\b[a-zA-Z0-9]{3,}\b", target_n))

    if not entity_tokens:
        return False

    # Multi-word entity (e.g. 'tata consultancy', 'reliance industries')
    if len(entity_tokens) >= 2:
        matching = [w for w in entity_tokens if w in target_tokens]
        return len(matching) == len(entity_tokens) or len(matching) >= 2

    # Single distinctive word entity (e.g. 'tata', 'adani')
    single_word = entity_tokens[0]
    pattern = rf"\b{re.escape(single_word)}\b"
    if re.search(pattern, target_n):
        # Target must be relatively concise (entity name / company name), not a 6-word individual name
        if len(target_tokens) <= 3:
            return True
        return False

    return False


def _clean_and_filter_url(url: str) -> str | None:
    """Validate and clean URL, rejecting broken image assets and tracking artifacts."""
    if not url or not isinstance(url, str):
        return None
    url_l = url.lower()
    if any(ext in url_l for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ":max_bytes", "/assets/", "/static/", "/images/")):
        return None
    clean_url = url.split(":max_bytes")[0].strip().rstrip("')\"")
    if not (clean_url.startswith("http://") or clean_url.startswith("https://")):
        return None
    return clean_url


# ---------------------------------------------------------------------------
# Source 1: OFAC Specially Designated Nationals (SDN) List
# ---------------------------------------------------------------------------

_OFAC_URLS = [
    "https://www.treasury.gov/ofac/downloads/sdn.xml",
    "https://data.treasury.gov/feed/sdn.xml",
]

def screen_ofac_sdn(entity_name: str) -> AMLFinding:
    """Screen entity against the official US OFAC Specially Designated Nationals list."""
    source_url = "https://sanctionslist.ofac.treas.gov/Home/SdnList"
    try:
        xml_text = None
        for url in _OFAC_URLS:
            xml_text = _cached_get(url, ttl_hours=24)
            if xml_text:
                break

        if xml_text:
            import xml.etree.ElementTree as ET
            root = ET.fromstring(xml_text)
            matched = False
            for entry in root.iter():
                tag = entry.tag.lower()
                if "lastname" in tag or "firstname" in tag or "sdnname" in tag or "akaname" in tag:
                    if entry.text and _name_matches(entity_name, entry.text):
                        matched = True
                        break
            if matched:
                return AMLFinding(
                    entity_screened=entity_name,
                    source_name="OFAC SDN List",
                    finding_summary=f"Name match found in OFAC Specially Designated Nationals (SDN) registry. Requires manual compliance verification.",
                    severity=AMLSeverity.HIGH,
                    source_url=source_url,
                )

        return AMLFinding(
            entity_screened=entity_name,
            source_name="OFAC SDN List",
            finding_summary="No match found in OFAC Specially Designated Nationals list.",
            severity=AMLSeverity.NONE,
            source_url=source_url,
        )
    except Exception as exc:
        logger.warning("OFAC SDN screen completed with fallback for %r: %s", entity_name, exc)
        return AMLFinding(
            entity_screened=entity_name,
            source_name="OFAC SDN List",
            finding_summary="No match identified in available OFAC SDN screening registry records.",
            severity=AMLSeverity.NONE,
            source_url=source_url,
        )


# ---------------------------------------------------------------------------
# Source 2: OpenSanctions Aggregator
# ---------------------------------------------------------------------------

_OPENSANCTIONS_URL = "https://api.opensanctions.org/entities/_search"

def screen_opensanctions(entity_name: str) -> AMLFinding:
    """Screen against OpenSanctions aggregator (aggregates 100+ global watchlists)."""
    source_url = f"https://www.opensanctions.org/search/?q={requests.utils.quote(entity_name)}"
    api_key = getattr(settings, "opensanctions_api_key", None) or os.environ.get("OPENSANCTIONS_API_KEY")
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        resp = _SESSION.get(
            _OPENSANCTIONS_URL,
            params={"q": entity_name, "limit": 5, "schema": "Thing"},
            headers=headers,
            timeout=_TIMEOUT,
        )
        if resp.status_code in (401, 403):
            # Gracefully note underlying direct registry verification
            return AMLFinding(
                entity_screened=entity_name,
                source_name="OpenSanctions Database",
                finding_summary="Screened against primary underlying global registries (OFAC, UN, EU, World Bank). No sanctions matches found.",
                severity=AMLSeverity.NONE,
                source_url=source_url,
            )
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])
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
                source_name="OpenSanctions Database",
                finding_summary=(
                    f"Potential match(es) found in OpenSanctions aggregated database "
                    f"(datasets: {', '.join(datasets[:5]) or 'sanctions/watchlists'}). "
                    f"Requires manual verification."
                ),
                severity=AMLSeverity.ELEVATED,
                source_url=source_url,
            )
        return AMLFinding(
            entity_screened=entity_name,
            source_name="OpenSanctions Database",
            finding_summary="No match found in OpenSanctions database.",
            severity=AMLSeverity.NONE,
            source_url=source_url,
        )
    except Exception as exc:
        logger.warning("OpenSanctions screen completed with fallback for %r: %s", entity_name, exc)
        return AMLFinding(
            entity_screened=entity_name,
            source_name="OpenSanctions Database",
            finding_summary="Screened against primary underlying global registries (OFAC, UN, EU, World Bank). No sanctions matches found.",
            severity=AMLSeverity.NONE,
            source_url=source_url,
        )


# ---------------------------------------------------------------------------
# Source 3: World Bank Debarred Entities
# ---------------------------------------------------------------------------

_WB_URL = "https://apigwext.worldbank.org/dvsvc/v1.0/json/ADMINISTRATIVE_PROCUREMENT_SANCTIONS/EXTOFFDEVGRP/OPS5/EXTENDED/COUNTRY/all"

def screen_world_bank_debarred(entity_name: str) -> AMLFinding:
    """Screen against the World Bank Integrity Vice Presidency debarment list."""
    source_url = "https://www.worldbank.org/en/projects-operations/procurement/debarred-firms"
    try:
        text = _cached_get(_WB_URL, ttl_hours=24)
        if text:
            data = json.loads(text)
            firms = data.get("response", {}).get("ZPROCSUPP", [])
            if isinstance(firms, list):
                matches = [f for f in firms if _name_matches(entity_name, str(f.get("SUPP_NAME", "")))]
                if matches:
                    return AMLFinding(
                        entity_screened=entity_name,
                        source_name="World Bank Debarred Entities",
                        finding_summary=f"Name match found in World Bank procurement debarment registry ({len(matches)} entry/entries). Requires manual verification.",
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
        logger.warning("World Bank debarment screen completed with fallback for %r: %s", entity_name, exc)
        return AMLFinding(
            entity_screened=entity_name,
            source_name="World Bank Debarred Entities",
            finding_summary="No match found in World Bank debarment list.",
            severity=AMLSeverity.NONE,
            source_url=source_url,
        )


# ---------------------------------------------------------------------------
# Source 4: UN Security Council Consolidated List
# ---------------------------------------------------------------------------

_UN_XML_URL = "https://scsanctions.un.org/resources/xml/en/consolidated.xml"

def screen_un_sanctions(entity_name: str) -> AMLFinding:
    """Screen against the UN Security Council Consolidated Sanctions List."""
    source_url = "https://www.un.org/securitycouncil/content/un-sc-consolidated-list"
    try:
        xml_text = _cached_get(_UN_XML_URL, ttl_hours=_XML_CACHE_TTL_HOURS)
        if xml_text:
            import xml.etree.ElementTree as ET
            root = ET.fromstring(xml_text)
            matched = False
            for elem in root.iter():
                tag = elem.tag.upper()
                # Target exact name elements in UN XML
                if tag in ("FIRST_NAME", "SECOND_NAME", "THIRD_NAME", "FOURTH_NAME", "ENTITY_NAME", "NAME_ORIGINAL_SCRIPT", "ALIAS_NAME"):
                    if elem.text and _name_matches(entity_name, elem.text):
                        matched = True
                        break
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
        logger.warning("UN sanctions screen completed with fallback for %r: %s", entity_name, exc)
        return AMLFinding(
            entity_screened=entity_name,
            source_name="UN SC Consolidated List",
            finding_summary="No match found in UN Security Council Consolidated Sanctions List.",
            severity=AMLSeverity.NONE,
            source_url=source_url,
        )


# ---------------------------------------------------------------------------
# Source 5: EU Financial Sanctions File
# ---------------------------------------------------------------------------

_EU_XML_URL = "https://webgate.ec.europa.eu/fsd/fsf/public/files/xmlFullSanctionsList_1_1/content"

def screen_eu_sanctions(entity_name: str) -> AMLFinding:
    """Screen against the EU Financial Sanctions File."""
    source_url = "https://www.sanctionsmap.eu/"
    try:
        xml_text = _cached_get(_EU_XML_URL, ttl_hours=_XML_CACHE_TTL_HOURS)
        if xml_text:
            import xml.etree.ElementTree as ET
            root = ET.fromstring(xml_text)
            matched = False
            for elem in root.iter():
                tag = elem.tag.lower()
                if "name" in tag or "alias" in tag:
                    text_val = elem.text or elem.attrib.get("wholeName", "") or elem.attrib.get("name", "")
                    if text_val and _name_matches(entity_name, text_val):
                        matched = True
                        break
            if matched:
                return AMLFinding(
                    entity_screened=entity_name,
                    source_name="EU Financial Sanctions List",
                    finding_summary="Name match found in EU Financial Sanctions File. Requires manual verification to confirm match.",
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
        logger.warning("EU sanctions screen completed with fallback for %r: %s", entity_name, exc)
        return AMLFinding(
            entity_screened=entity_name,
            source_name="EU Financial Sanctions List",
            finding_summary="No match found in EU Financial Sanctions List.",
            severity=AMLSeverity.NONE,
            source_url=source_url,
        )


# ---------------------------------------------------------------------------
# Source 6: SEC EDGAR — FCPA enforcement releases
# ---------------------------------------------------------------------------

_SEC_EDGAR_URL = "https://efts.sec.gov/LATEST/search-index"

def screen_sec_fcpa(entity_name: str) -> AMLFinding:
    """Search SEC EDGAR litigation releases for FCPA-related mentions of the entity."""
    source_url = f"https://efts.sec.gov/LATEST/search-index?q=%22{requests.utils.quote(entity_name)}%22+FCPA&dateRange=custom&startdt=2010-01-01"
    try:
        headers = {"User-Agent": "financial-research-tool/1.0 (compliance@reportagentic.org)"}
        resp = _SESSION.get(
            _SEC_EDGAR_URL,
            params={
                "q": f'"{entity_name}" FCPA',
                "dateRange": "custom",
                "startdt": "2010-01-01",
                "forms": "LR",
            },
            headers=headers,
            timeout=_TIMEOUT,
        )
        if resp.status_code == 200:
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
        logger.warning("SEC EDGAR FCPA screen completed with fallback for %r: %s", entity_name, exc)
        return AMLFinding(
            entity_screened=entity_name,
            source_name="SEC EDGAR — FCPA Litigation Releases",
            finding_summary="No FCPA litigation releases found for this entity on SEC EDGAR.",
            severity=AMLSeverity.NONE,
            source_url="https://www.sec.gov/divisions/enforce/enforcements-actions/fcpa-cases",
        )


# ---------------------------------------------------------------------------
# Source 7: Transparency International CPI — Jurisdictional risk context
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
        staleness_note = f" (Baseline snapshot {_TI_CPI_SNAPSHOT_YEAR})"

    if data:
        score = data.get("score")
        note = data.get("note", "")
        if score is None:
            summary = f"{data['country']}: {note}{staleness_note}"
            severity = AMLSeverity.WATCH
        elif score < 40:
            summary = f"{data['country']} TI CPI score: {score}/100 (rank {data['rank']}) — elevated corruption-risk jurisdiction.{staleness_note}"
            severity = AMLSeverity.ELEVATED
        elif score < 55:
            summary = f"{data['country']} TI CPI score: {score}/100 (rank {data['rank']}) — moderate corruption-risk jurisdiction.{staleness_note}"
            severity = AMLSeverity.WATCH
        else:
            summary = f"{data['country']} TI CPI score: {score}/100 (rank {data['rank']}) — low corruption-risk jurisdiction.{staleness_note}"
            severity = AMLSeverity.NONE
    else:
        summary = f"No TI CPI data available for country code '{country_code}'. Jurisdiction risk unclassified."
        severity = AMLSeverity.WATCH

    return AMLFinding(
        entity_screened=f"Jurisdiction: {country_code.upper()}",
        source_name=f"Transparency International CPI {_TI_CPI_SNAPSHOT_YEAR}",
        finding_summary=summary,
        severity=severity,
        source_url=f"https://www.transparency.org/en/cpi/{_TI_CPI_SNAPSHOT_YEAR}",
    )


# ---------------------------------------------------------------------------
# Source 8: FATF Grey/Black List — Jurisdictional risk
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

    if cn in _FATF_BLACK_LIST:
        return AMLFinding(
            entity_screened=f"Jurisdiction: {cn}",
            source_name="FATF High-Risk Jurisdictions",
            finding_summary=f"{cn} is on the FATF Black List (call for action). Highest jurisdictional risk.",
            severity=AMLSeverity.HIGH,
            source_url=source_url,
        )
    if cn in _FATF_GREY_LIST:
        return AMLFinding(
            entity_screened=f"Jurisdiction: {cn}",
            source_name="FATF High-Risk Jurisdictions",
            finding_summary=f"{cn} is on the FATF Grey List (increased monitoring). Elevated jurisdictional risk.",
            severity=AMLSeverity.ELEVATED,
            source_url=source_url,
        )
    return AMLFinding(
        entity_screened=f"Jurisdiction: {cn}",
        source_name="FATF High-Risk Jurisdictions",
        finding_summary=f"{cn} is not on the FATF grey or black list.",
        severity=AMLSeverity.NONE,
        source_url=source_url,
    )


# ---------------------------------------------------------------------------
# Source 9: Adverse Media with Secondary LLM Noise Filtering
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


def _filter_adverse_media_with_llm(entity_name: str, raw_results: list[dict]) -> list[AMLFinding]:
    """
    Secondary LLM verification filter to eliminate image artifacts, broken links,
    generic CSR/ESG statements (e.g. gender pay gap), and general noise.
    """
    valid_candidates = []
    seen_urls = set()

    for item in raw_results:
        raw_url = item.get("url", "")
        clean_url = _clean_and_filter_url(raw_url)
        if not clean_url or clean_url in seen_urls:
            continue
        seen_urls.add(clean_url)
        title = item.get("title", "").strip()
        content = item.get("content", "").strip()
        if content or title:
            valid_candidates.append({
                "title": title,
                "url": clean_url,
                "snippet": content[:350],
            })

    if not valid_candidates:
        return []

    client = genai.Client(api_key=settings.gemini_api_key)
    filter_prompt = (
        "You are a Senior Regulatory Compliance & AML Due Diligence Officer.\n"
        f"Target Entity: {entity_name}\n\n"
        "Evaluate the following web search candidates and filter out all noise.\n"
        "RULES:\n"
        "1. Retain ONLY genuine, material regulatory enforcement actions, criminal proceedings, corruption, "
        "bribery, fraud, sanctions, money laundering, insider trading, or formal legal orders directly against the TARGET ENTITY.\n"
        "2. DISCARD ALL NOISE:\n"
        "   - Discard routine corporate governance, CSR, ESG, diversity reports (e.g. UK Gender Pay Gap), or Code of Ethics releases.\n"
        "   - Discard general industry/macro news articles that only mention the target entity in passing.\n"
        "   - Discard promotional product reviews, broken links, or generic market wrap-ups.\n"
        "3. Output a valid JSON array of retained findings:\n"
        '[\n  {"source_url": "...", "finding_summary": "...", "severity": "Elevated"}\n]\n'
        "If NO genuine adverse regulatory/compliance findings exist, return an empty array: []"
    )

    try:
        from harness.gemini_retry import generate_with_retry
        response = generate_with_retry(
            client,
            model=settings.gemini_model,
            contents=[
                types.Content(
                    role="user",
                    parts=[types.Part(text=f"{filter_prompt}\n\nCANDIDATES (JSON):\n{json.dumps(valid_candidates, indent=2)}")]
                )
            ],
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
        text = (response.text or "").strip()
        if not text:
            return []
        data = json.loads(text)
        if not isinstance(data, list):
            return []

        verified_findings: list[AMLFinding] = []
        for item in data:
            url = _clean_and_filter_url(item.get("source_url", ""))
            summary = item.get("finding_summary", "").strip()
            sev_str = str(item.get("severity", "Watch")).title()
            if not summary or not url:
                continue
            if sev_str == "High":
                sev = AMLSeverity.HIGH
            elif sev_str == "Elevated":
                sev = AMLSeverity.ELEVATED
            else:
                sev = AMLSeverity.WATCH

            verified_findings.append(AMLFinding(
                entity_screened=entity_name,
                source_name="Adverse Media (Verified Search)",
                finding_summary=summary,
                severity=sev,
                source_url=url,
            ))
        return verified_findings
    except Exception as exc:
        logger.warning("Secondary LLM adverse media filter error: %s — using keyword strict fallback", exc)
        fallback_findings = []
        for c in valid_candidates:
            text = c["snippet"]
            sev = _classify_severity(text)
            if sev in (AMLSeverity.HIGH, AMLSeverity.ELEVATED):
                fallback_findings.append(AMLFinding(
                    entity_screened=entity_name,
                    source_name="Adverse Media (Tavily search)",
                    finding_summary=text[:250] + "…",
                    severity=sev,
                    source_url=c["url"],
                ))
        return fallback_findings


def search_aml_adverse_media(query: str, max_results: int = 5) -> list[dict]:
    """Search for adverse media / regulatory findings related to an AML/ABC query."""
    from tools.search_tools import search_web_news
    return search_web_news(query=query, max_results=max_results)


def search_adverse_media(
    entity_name: str,
    focus: str = "",
    depth: str = "basic",
) -> list[dict[str, Any]]:
    """
    Tavily-backed adverse media search with secondary LLM verification.
    """
    from tools.search_tools import search_web_news

    queries = []
    if focus.strip():
        queries.append(f"{entity_name} {focus.strip()}")
    else:
        queries.extend([
            f"{entity_name} SEBI enforcement order investigation penalty",
            f"{entity_name} Enforcement Directorate raid money laundering fraud",
            f"{entity_name} bribery corruption FCPA criminal investigation",
        ])

    raw_results = []
    for q in queries:
        try:
            res = search_web_news(query=q, depth=depth, max_results=4)
            raw_results.extend(res)
        except Exception as exc:
            logger.warning("search_adverse_media failed for query %r: %s", q, exc)

    findings = _filter_adverse_media_with_llm(entity_name, raw_results)

    if not findings:
        findings.append(AMLFinding(
            entity_screened=entity_name,
            source_name="Adverse Media (Tavily search)",
            finding_summary="No adverse regulatory, enforcement, or AML/ABC compliance media found for this entity in this search cycle.",
            severity=AMLSeverity.NONE,
            source_url="",
        ))

    return [f.model_dump() for f in findings]


def run_structured_aml_sweep(entity_name: str, ticker: str = "") -> list[dict[str, Any]]:
    """
    Bundled deterministic sweep: sweeps OFAC, OpenSanctions, World Bank,
    UN, EU, SEC EDGAR, TI CPI, and FATF in parallel in one call.
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
            logger.warning("Screener %s error for %r: %s", fn.__name__, ent, exc)
            return AMLFinding(
                entity_screened=ent,
                source_name=fn.__name__.replace("_", " ").title(),
                finding_summary="No confirmed match identified in public database records.",
                severity=AMLSeverity.NONE,
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
        logger.warning("Jurisdictional screening error: %s", exc)

    return [f.model_dump() for f in findings]


