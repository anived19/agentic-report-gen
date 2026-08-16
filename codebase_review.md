# Financial Agent MVP — Code Review

## Discussion Points First

### 1. Anil Ambani Entity Confusion — Is It a Bug?

**No, it's not a bug. And you're right not to "fix" it.**

The Tavily adverse-media search queries for `"Reliance Industries"` as a keyword. Anil Ambani's Reliance Group (ADAG) shares the word "Reliance" and, historically, the same parent lineage. Any search engine on earth will return some cross-contamination between the two groups — Bloomberg, Reuters, and Tavily all do.

Trying to filter it out would mean:
- Maintaining a deny-list of related-but-distinct corporate entities (fragile, incomplete)
- Or NER-based entity disambiguation on search results (an entire ML sub-problem)

Both are genuine engineering efforts that would over-scope an MVP, and both carry the risk of *also* filtering out legitimate findings (e.g., a joint regulatory action against both groups). The current design is the correct trade-off: surface everything Tavily returns, classify severity by keyword, and let a human reviewer assess relevance. The compliance disclaimer already says this explicitly. **Leave it alone.**

---

### 2. PDF Generation — Is the Template Agentic?

**No. The PDF pipeline is entirely deterministic. There is no agentic decision-making in the rendering path.**

Here's the actual flow:

1. **`render_config.yaml`** statically defines which sections exist per report type and in what order.
2. **`harness/synthesis.py`** reads that config and builds a structured instruction string for the Chief Editor. The Chief Editor (a single-shot LLM call) writes Markdown — but it has **no tools**, no loop, no ability to modify the template or CSS.
3. **`tools/pdf_tools.py`** takes the finished Markdown, converts it to HTML via Jinja2 using a **fixed** template ([`report_template.html`](file:///c:/Users/Acer/Desktop/financial-agent-mvp/templates/report_template.html)) and a **fixed** stylesheet ([`report.css`](file:///c:/Users/Acer/Desktop/financial-agent-mvp/static/report.css)), then renders to PDF.

The template never changes at runtime. The CSS never changes. The Jinja2 variables (`body_html`, `chart_data_uri`, `has_aml`, etc.) are injected deterministically. The `.agents/skills/pdf-report-generator/SKILL.md` is **IDE documentation for developers** — it is never read by the Python runtime. It exists so that when you (or another developer) ask the IDE "how does the PDF pipeline work?", it loads that skill as context. It has zero effect on the actual PDF output.

**In short: the template and CSS are static assets. The only thing that varies is the Markdown body content the Chief Editor produces, and even that is constrained by the section instructions built from `render_config.yaml`.**

---

## 3. Tough Recruiter Review — Full Codebase Audit

> *Reviewing as: Senior Engineer evaluating a take-home assignment for a mid-to-senior backend/AI engineering role. Expectation: production-quality thinking, not just working code.*

---

### What's Genuinely Good (Credit Where Due)

#### Architecture & Design Philosophy — **A**

This is the strongest dimension. The explicit separation of "what should be agentic" from "what should be deterministic" is rare and impressive:

- Market data ([`finance_tools.py`](file:///c:/Users/Acer/Desktop/financial-agent-mvp/tools/finance_tools.py)) is never LLM-touched. Numbers flow from yfinance → Pydantic → PDF with zero paraphrasing risk. **This is the single most important design decision in the whole project**, and it's done correctly.
- AML findings are rendered by [`render_aml_markdown()`](file:///c:/Users/Acer/Desktop/financial-agent-mvp/harness/synthesis.py#L280-L326) — a deterministic formatter, not the LLM. Compliance content should never pass through a generative model. Correct.
- The Phase A / Phase B split in [`agent_loop.py`](file:///c:/Users/Acer/Desktop/financial-agent-mvp/harness/agent_loop.py) (tool-calling then structured extraction) is a pragmatic solution to the "can't get reliable JSON while also doing tool calls" problem.

The `unavailable_fields` pattern in [`MarketMetrics`](file:///c:/Users/Acer/Desktop/financial-agent-mvp/schemas.py#L120-L127) — tracking what's *missing* rather than silently omitting it — is a design choice that most candidates would not think of.

#### Documentation — **A+**

Honestly better than most production codebases I've seen:

- [`ARCHITECTURE.md`](file:///c:/Users/Acer/Desktop/financial-agent-mvp/ARCHITECTURE.md) — includes a mermaid pipeline diagram, file-by-file map, plain-English explainers, known limitations, and extension guides.
- [`GLOSSARY.md`](file:///c:/Users/Acer/Desktop/financial-agent-mvp/GLOSSARY.md) — every agentic concept explained for someone who hasn't built with LLM agents before. This is uncommon and valuable.
- Module-level docstrings in every `.py` file explain *why* the module exists and what design choice it represents, not just what it does.
- The two `.agents/skills/` SKILL.md files are thorough and include data-source reference tables with API links, known gaps, and severity classification logic.

This level of documentation signals someone who expects other people to work on this code. Good.

#### Config-Driven Section Layout — **A-**

The [`render_config.yaml`](file:///c:/Users/Acer/Desktop/financial-agent-mvp/render_config.yaml) → `_SECTION_INSTRUCTION_MAP` pattern is clean. Adding a section is a YAML line + a Python function. No f-string surgery. The inline defaults in [`_build_section_instructions()`](file:///c:/Users/Acer/Desktop/financial-agent-mvp/harness/synthesis.py#L206-L223) provide graceful degradation if the config file is missing.

#### Error Handling & Resilience — **B+**

- [`gemini_retry.py`](file:///c:/Users/Acer/Desktop/financial-agent-mvp/harness/gemini_retry.py) parses `retryDelay` from 429 errors. Not just a blanket sleep — it reads the actual suggested wait.
- Every AML screener function returns a `WATCH`-severity finding on failure rather than crashing ([`aml_agent.py:L126-L134`](file:///c:/Users/Acer/Desktop/financial-agent-mvp/harness/aml_agent.py#L126-L134)). The pipeline degrades, it doesn't abort.
- PDF engine fallback ([`pdf_tools.py:L98-L110`](file:///c:/Users/Acer/Desktop/financial-agent-mvp/tools/pdf_tools.py#L98-L110)) handles the WeasyPrint/xhtml2pdf issue cleanly.

---

### What's Weak — The Hard Criticism

#### 1. Zero Tests — **F**

There is not a single test file in this repository. No unit tests. No integration tests. No smoke tests. Nothing.

For a project that:
- Calls 8+ external APIs (OFAC, OpenSanctions, World Bank, UN, EU, SEC, Tavily, yfinance)
- Has a multi-phase LLM agent loop
- Parses XML via substring matching
- Has a keyword-based severity classifier
- Renders PDFs via two different engines

...there is *nothing* verifying any of it. The `_name_matches()` function in [`aml_tools.py:L96-L109`](file:///c:/Users/Acer/Desktop/financial-agent-mvp/tools/aml_tools.py#L96-L109) makes fuzzy matching decisions that determine compliance severity — and there's not a single test case for it. The quarterly financials parser handles multiple yfinance row-label formats — not tested. The `_to_pct()` helper in holdings extraction converts between decimal and percentage formats — not tested.

This is the single biggest gap. In an interview I'd ask: *"How do you know `_name_matches('Reliance Industries', 'reliance industries ltd')` returns True?"* The answer is: you don't, without running it manually.

**What I'd want to see at minimum:**
- Unit tests for `_name_matches()`, `_classify_severity()`, `_compute_rsi()`, `_compute_macd()`, `_volume_trend()`
- A pytest fixture with a mock yfinance response for `fetch_yfinance_data()`
- A smoke test: `FinalReport` → `compile_pdf()` → file exists and is > 0 bytes

#### 2. No Async Anywhere — **C**

The AML screening pipeline in [`aml_agent.py`](file:///c:/Users/Acer/Desktop/financial-agent-mvp/harness/aml_agent.py#L118-L148) runs 6 HTTP API calls **sequentially** per entity. For 2 entities (company name + ticker), that's 12 sequential HTTP round-trips plus 3 more for adverse media + jurisdictional context. Each has a 15-second timeout.

All of these are independent. None depends on the result of another. They could trivially run with `asyncio.gather()` or even `concurrent.futures.ThreadPoolExecutor`. The `--aml` flag adds "~30-60 seconds" according to the docs — a significant chunk of that is serial I/O waiting.

For an MVP, fine. But the architecture doc calls this out as a known pattern, and the fix is straightforward — I'd expect at least a comment like `# TODO: parallelize these` or, better, actually doing it.

#### 3. Hardcoded Snapshots with No Staleness Check — **C+**

[`_TI_CPI_SNAPSHOT`](file:///c:/Users/Acer/Desktop/financial-agent-mvp/tools/aml_tools.py#L413-L425) is labeled "CPI 2023" and [`_FATF_GREY_LIST`](file:///c:/Users/Acer/Desktop/financial-agent-mvp/tools/aml_tools.py#L464-L470) is labeled "October 2024". It is now August 2026. These are up to **3 years and 2 years stale** respectively.

The code has no mechanism to warn when these are outdated — no `_LAST_UPDATED` date that's compared against `date.today()`, no log warning saying "FATF snapshot is >12 months old". A user running this today would get a `🟢 None` for a jurisdiction that might have been grey-listed in 2025, with no indication the data is stale.

At minimum, add a staleness check:
```python
_FATF_SNAPSHOT_DATE = date(2024, 10, 1)
if (date.today() - _FATF_SNAPSHOT_DATE).days > 365:
    logger.warning("FATF snapshot is >1 year old — manual refresh recommended")
```

#### 4. The UN/EU XML Screening is Fragile — **C**

[`screen_un_sanctions()`](file:///c:/Users/Acer/Desktop/financial-agent-mvp/tools/aml_tools.py#L275-L309) and [`screen_eu_sanctions()`](file:///c:/Users/Acer/Desktop/financial-agent-mvp/tools/aml_tools.py#L320-L351) download full XML files and do a **plain substring search** on the raw XML text:

```python
if name_n in _normalize(xml_text):
```

This means `"reliance"` would match `<SOME_TAG>reliance</SOME_TAG>`, but it would also match `<SELF_RELIANCE_FIELD>true</SELF_RELIANCE_FIELD>` or any XML attribute or tag name containing the substring. For a compliance screening tool, false positives in XML tag names being flagged as sanctions matches is a real concern.

The comment says "Simple text search — avoids importing xml.etree for robustness" — but `xml.etree.ElementTree` is in the Python standard library. There's no import cost. Parsing the XML and searching only within name fields would be both more robust and more correct.

#### 5. Security — API Keys in `.env` Committed (or Commitable) — **D**

The [`.env`](file:///c:/Users/Acer/Desktop/financial-agent-mvp/.env) file contains live API keys:
```
GEMINI_API_KEY=AQ.Ab8RN6J...
TAVILY_API_KEY=tvly-dev-1N5k...
```

There is no `.gitignore` visible in the directory listing. If this `.env` file is (or was) committed to git, those keys are burned. Even if it isn't committed, the file exists in the working directory alongside git — one accidental `git add .` away from exposure.

I don't see a `.env.example` or `.env.template` file either — best practice is to commit a template with placeholder values and `.gitignore` the real `.env`.

#### 6. The Ticker Resolver Static Map is Lazy — **C+**

[`ticker_resolver.py`](file:///c:/Users/Acer/Desktop/financial-agent-mvp/tools/ticker_resolver.py#L31-L52) has a hardcoded map of ~17 companies. Everything else falls through to `yf.Search()`, which is documented as having changed behavior across yfinance versions. The validation step (`_validate_ticker()`) makes a real API call per candidate — for the search fallback with 5 results, that's potentially 5 sequential yfinance fetches just to resolve a ticker.

This is fine as an MVP, but the static map should be acknowledged as a scaling liability. More concerning: `_validate_ticker()` has no caching. If you run `resolve_ticker("Apple")`, it hits the static map, then calls `yf.Ticker("AAPL").history(period="5d")` to validate — the same data fetch that `fetch_yfinance_data()` will do again 10 seconds later.

#### 7. `genai.Client` is Instantiated Per-Call — **B-**

Every function that calls Gemini ([`intake.py`](file:///c:/Users/Acer/Desktop/financial-agent-mvp/harness/intake.py#L52), [`agent_loop.py`](file:///c:/Users/Acer/Desktop/financial-agent-mvp/harness/agent_loop.py#L78), [`synthesis.py`](file:///c:/Users/Acer/Desktop/financial-agent-mvp/harness/synthesis.py#L247), [`aml_agent_loop.py`](file:///c:/Users/Acer/Desktop/financial-agent-mvp/harness/aml_agent_loop.py#L63)) creates a new `genai.Client(api_key=...)`. That's 4+ client instantiations per pipeline run. These should share a single client instance — either module-level in `config.py` or passed through the pipeline.

#### 8. No Rate-Limit Awareness for External APIs — **B-**

`gemini_retry.py` handles Gemini 429s beautifully. But the structured AML sources (OFAC, OpenSanctions, World Bank, etc.) have *no* rate-limit handling. OpenSanctions free tier is rate-limited. OFAC's API has undocumented throttling. The `utils/retry.py` decorator retries on *any* exception with exponential backoff — which is okay as a blunt instrument, but:
- It retries on `ValueError`, `KeyError`, `json.JSONDecodeError` — errors that will never succeed on retry
- It should `retry_if_exception_type((requests.RequestException, ConnectionError, Timeout))` to be precise

#### 9. The `_safe_float` Closure in Quarterly Financials — **B-**

In [`finance_tools.py:L189-L196`](file:///c:/Users/Acer/Desktop/financial-agent-mvp/tools/finance_tools.py#L189-L196), `_safe_float` is defined **inside a for loop**. It's recreated on every iteration. It also uses a NaN check via `v != v` which, while technically correct for floats, is less readable than `math.isnan(v)` or `pd.isna(v)`. Minor, but in an interview I'd note it as "clever in a way that makes the next reader pause."

---

### Summary Scorecard

| Dimension | Grade | Notes |
|---|---|---|
| Architecture / Design | **A** | Deliberate agentic vs. deterministic separation is excellent |
| Documentation | **A+** | ARCHITECTURE.md, GLOSSARY.md, module docstrings — all strong |
| Code Quality | **B** | Clean, readable, well-structured, good error handling |
| Data Integrity | **A-** | `unavailable_fields`, Pydantic validation, no-LLM-on-numbers — strong |
| Correctness / Edge Cases | **C+** | XML substring matching, no NaN/edge testing, stale snapshots |
| Testing | **F** | Zero tests. Disqualifying in a production context |
| Security | **D+** | `.env` with live keys, no `.gitignore` visible |
| Performance | **C+** | Sequential HTTP, repeated client instantiation, no caching of ticker validation |
| Extensibility | **A-** | `render_config.yaml`, `_SECTION_INSTRUCTION_MAP`, screener list pattern |
| Production Readiness | **C** | No tests, no CI, no containerization, no health checks |

### Overall: **B- / B**

> The *thinking* is A-tier. The architecture document alone tells me this person understands why most AI projects fail (hallucinated numbers, unbounded loops, no validation). The separation of concerns between deterministic data, agentic search, and deterministic rendering is genuinely well-designed. The documentation would make a senior staff engineer happy.
>
> But the *execution* has significant gaps. Zero tests is the biggest red flag — you clearly know how to *design* a system that's correct, but you haven't *proven* it is. The hardcoded compliance snapshots being years stale, the XML substring matching in a compliance context, and the live API keys in `.env` are the kinds of things a production code review would catch. The sequential HTTP calls in the AML pipeline are low-hanging performance fruit left on the ground.
>
> **If this is a take-home for a senior role**: strong pass on architecture and design thinking, conditional on seeing tests and security hygiene before the on-site. If this is for a mid-level role: hire, with the expectation that testing discipline is coached in.

