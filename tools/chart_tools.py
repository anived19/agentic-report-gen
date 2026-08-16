"""
Renders the 6-month price trend (with 50/200-day MA reference lines) as a
base64 PNG data URI, ready to drop straight into an <img> tag.

Kept isolated from pdf_tools.py so a charting failure can't take down the
rest of report compilation — the chart is a visual nice-to-have, never a
source of numbers the report's actual content depends on.
"""
from __future__ import annotations

import base64
import io
import logging

import matplotlib

matplotlib.use("Agg")  # headless — no display available in a CLI/server context
import matplotlib.pyplot as plt  # noqa: E402  (must follow matplotlib.use())

from schemas import MarketMetrics

logger = logging.getLogger(__name__)

_LINE_COLOR = "#1a3c6e"
_MA50_COLOR = "#c07d18"
_MA200_COLOR = "#8a1f1f"


def generate_price_chart_base64(metrics: MarketMetrics) -> str | None:
    """Returns a `data:image/png;base64,...` URI, or None if there's no trend data to plot."""
    if not metrics.outlook_price_trend:
        logger.info("No price trend data for %s — skipping chart.", metrics.ticker)
        return None

    dates = [p.date for p in metrics.outlook_price_trend]
    closes = [p.close for p in metrics.outlook_price_trend]

    fig, ax = plt.subplots(figsize=(7.5, 3.2))
    ax.plot(dates, closes, color=_LINE_COLOR, linewidth=1.6, label="Close price")

    if metrics.fifty_day_ma is not None:
        ax.axhline(metrics.fifty_day_ma, color=_MA50_COLOR, linestyle="--", linewidth=1, label="50-day MA")
    if metrics.two_hundred_day_ma is not None:
        ax.axhline(metrics.two_hundred_day_ma, color=_MA200_COLOR, linestyle="--", linewidth=1, label="200-day MA")

    ax.set_title(f"{metrics.company_name or metrics.ticker} — {metrics.outlook_months}-Month Price Trend", fontsize=11)
    ax.set_ylabel(f"Price ({metrics.currency or ''})", fontsize=9)
    ax.tick_params(axis="x", rotation=30, labelsize=8)
    ax.tick_params(axis="y", labelsize=8)
    ax.legend(fontsize=8, loc="best")
    ax.grid(alpha=0.25)
    fig.tight_layout()

    buf = io.BytesIO()
    try:
        fig.savefig(buf, format="png", dpi=140, bbox_inches="tight")
    except Exception as exc:
        logger.warning("Chart rendering failed for %s: %s", metrics.ticker, exc)
        plt.close(fig)
        return None
    finally:
        plt.close(fig)

    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"
