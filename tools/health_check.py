"""
Health check utility for the Financial Report Generator.

Verifies:
1. API Keys configuration (Gemini, Tavily).
2. PDF rendering engines (WeasyPrint vs xhtml2pdf availability).
3. Local directory permissions (outputs, cache).
4. External network connectivity for data sources.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

# Ensure root directory is in sys.path when executed directly
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import requests

from config import settings

logger = logging.getLogger("health_check")


def check_api_keys() -> dict[str, bool]:
    results = {}
    results["gemini_api_key"] = bool(settings.gemini_api_key and not settings.gemini_api_key.startswith("your_"))
    results["tavily_api_key"] = bool(settings.tavily_api_key and not settings.tavily_api_key.startswith("your_"))
    return results


def check_pdf_engines() -> dict[str, bool]:
    results = {}
    try:
        from weasyprint import HTML  # noqa: F401
        results["weasyprint"] = True
    except Exception as exc:
        logger.debug("Weasyprint check failed: %s", exc)
        results["weasyprint"] = False

    try:
        from xhtml2pdf import pisa  # noqa: F401
        results["xhtml2pdf"] = True
    except Exception as exc:
        logger.debug("xhtml2pdf check failed: %s", exc)
        results["xhtml2pdf"] = False

    return results


def check_directories() -> dict[str, bool]:
    results = {}
    for d in [settings.output_dir, settings.cache_dir]:
        try:
            d.mkdir(parents=True, exist_ok=True)
            test_file = d / ".health_check_tmp"
            test_file.write_text("ok", encoding="utf-8")
            test_file.unlink()
            results[str(d)] = True
        except Exception:
            results[str(d)] = False
    return results


def check_connectivity() -> dict[str, bool]:
    endpoints = {
        "World Bank API": "https://apigwext.worldbank.org/dvsvc/v1.0/json/ADMINISTRATIVE_PROCUREMENT_SANCTIONS/EXTOFFDEVGRP/OPS5/EXTENDED/COUNTRY/all",
        "UN Sanctions XML": "https://scsanctions.un.org/resources/xml/en/consolidated.xml",
        "OpenSanctions API": "https://api.opensanctions.org/entities/_search",
    }
    results = {}
    for name, url in endpoints.items():
        try:
            resp = requests.get(url, timeout=5, headers={"User-Agent": "financial-agent-mvp/healthcheck"})
            results[name] = resp.status_code in (200, 401, 403, 404)  # Server reachable
        except Exception:
            results[name] = False
    return results


def run_health_check() -> bool:
    print("=== Financial Agent System Health Check ===\n")
    all_healthy = True

    # 1. API Keys
    keys = check_api_keys()
    print("[1] API Keys:")
    for k, ok in keys.items():
        print(f"    - {k}: {'[OK] Configured' if ok else '[FAIL] Missing/Default'}")
        if not ok:
            all_healthy = False

    # 2. PDF Engines
    engines = check_pdf_engines()
    print("\n[2] PDF Rendering Engines:")
    for e, ok in engines.items():
        note = " (Primary)" if e == settings.pdf_engine else ""
        print(f"    - {e}: {'[OK] Available' if ok else '[WARN] Unavailable'}{note}")
    if not (engines.get("weasyprint") or engines.get("xhtml2pdf")):
        print("    [FAIL] Error: No working PDF engine available!")
        all_healthy = False

    # 3. Directory Permissions
    dirs = check_directories()
    print("\n[3] Directory Write Permissions:")
    for d, ok in dirs.items():
        print(f"    - {d}: {'[OK] Writable' if ok else '[FAIL] Failed'}")
        if not ok:
            all_healthy = False

    # 4. Connectivity
    conn = check_connectivity()
    print("\n[4] External Data Source Reachability:")
    for src, ok in conn.items():
        print(f"    - {src}: {'[OK] Reachable' if ok else '[WARN] Unreachable/Offline'}")

    print("\n" + ("=" * 43))
    print(f"Overall Status: {'[PASS] HEALTHY' if all_healthy else '[WARN] ATTENTION REQUIRED'}\n")
    return all_healthy


if __name__ == "__main__":
    healthy = run_health_check()
    sys.exit(0 if healthy else 1)
