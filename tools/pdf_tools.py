"""
Markdown -> HTML -> PDF compilation.

Engine selection: WeasyPrint gives the best CSS fidelity but depends on
system-level Pango/Cairo/GDK-PixBuf libraries that don't install cleanly
on every machine (Windows in particular — see ARCHITECTURE.md). xhtml2pdf
is pure-pip with weaker CSS support but zero native-dependency risk. This
module tries `settings.pdf_engine` first and automatically falls back to
the other one if the primary engine fails to import or errors at render
time, so a broken WeasyPrint install on someone's laptop doesn't block
report generation entirely.
"""
from __future__ import annotations

import logging
from pathlib import Path

import markdown as md
from jinja2 import Environment, FileSystemLoader, select_autoescape

from config import settings
from schemas import FinalReport
from tools.chart_tools import generate_price_chart_base64

logger = logging.getLogger(__name__)

_MD_EXTENSIONS = ["tables", "fenced_code", "nl2br", "sane_lists"]


def _render_markdown_to_html(markdown_text: str) -> str:
    return md.markdown(markdown_text, extensions=_MD_EXTENSIONS)


def _render_full_html(report: FinalReport) -> str:
    env = Environment(
        loader=FileSystemLoader(str(settings.templates_dir)),
        autoescape=select_autoescape(["html"]),
    )
    template = env.get_template("report_template.html")

    css_path = settings.static_dir / "report.css"
    css_text = css_path.read_text(encoding="utf-8") if css_path.exists() else ""

    chart_data_uri = generate_price_chart_base64(report.market_metrics)
    body_html = _render_markdown_to_html(report.markdown_body)

    return template.render(
        title=f"{report.company_name or report.ticker} - Stock Sentiment & Outlook Report",
        ticker=report.ticker,
        company_name=report.company_name or report.ticker,
        editorial_goal=report.editorial_goal,
        generated_at=report.generated_at.isoformat(),
        chart_data_uri=chart_data_uri,
        kpi_cards=report.kpi_cards,
        body_html=body_html,
        css=css_text,
        has_aml=report.aml_result is not None,
        outlook_months=report.market_metrics.outlook_months,
    )


def _write_pdf_weasyprint(html: str, output_path: Path) -> None:
    # Imported lazily so a missing/broken system install only breaks this
    # function, not the whole module (and therefore not the xhtml2pdf path).
    from weasyprint import HTML

    HTML(string=html, base_url=str(settings.templates_dir)).write_pdf(str(output_path))


def _write_pdf_xhtml2pdf(html: str, output_path: Path) -> None:
    from xhtml2pdf import pisa

    with open(output_path, "wb") as f:
        result = pisa.CreatePDF(src=html, dest=f)
    if result.err:
        raise RuntimeError(f"xhtml2pdf reported {result.err} error(s) while rendering the PDF")


_ENGINES = {
    "weasyprint": _write_pdf_weasyprint,
    "xhtml2pdf": _write_pdf_xhtml2pdf,
}


def compile_pdf(report: FinalReport, output_path: Path | None = None) -> Path:
    """
    Render `report` to a PDF file and return its path.

    Tries settings.pdf_engine first; on any failure (import error, render
    error), automatically retries with the other engine rather than
    failing the whole pipeline over a rendering-backend issue.
    """
    if output_path is None:
        safe_ticker = report.ticker.replace(".", "_")
        output_path = settings.output_dir / f"{safe_ticker}_{report.generated_at.isoformat()}.pdf"
    output_path = Path(output_path)

    html = _render_full_html(report)

    primary = settings.pdf_engine if settings.pdf_engine in _ENGINES else "weasyprint"
    fallback = "xhtml2pdf" if primary == "weasyprint" else "weasyprint"

    try:
        _ENGINES[primary](html, output_path)
        logger.info("PDF rendered via %s -> %s", primary, output_path)
        return output_path
    except Exception as exc:
        logger.warning("PDF engine '%s' failed (%s) — falling back to '%s'", primary, exc, fallback)

    _ENGINES[fallback](html, output_path)
    logger.info("PDF rendered via fallback %s -> %s", fallback, output_path)
    return output_path
