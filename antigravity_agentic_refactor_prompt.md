# Prompt for Antigravity: Convert the Financial Report Generator from a Fixed Pipeline into a Genuine Agentic Loop

Paste everything below this line into Antigravity as one instruction. It is written as a spec, not a vibe — follow it in order, and read the files it tells you to read before writing any code.

---

## 0. Read these files first, in this order, before changing anything

`ARCHITECTURE.md`, `main.py`, `harness/agent_loop.py`, `harness/intake.py`, `harness/synthesis.py`, `harness/aml_agent.py`, `harness/aml_agent_loop.py`, `harness/md_loader.py`, `tools/finance_tools.py`, `tools/ticker_resolver.py`, `tools/aml_tools.py`, `skills/search_web_news.md`, `skills/screen_entity_aml.md`, `schemas.py`, `render_config.yaml`, `agents/research_analyst.md`, `agents/chief_editor.md`, `agents/aml_screener.md`.

You need the exact YAML frontmatter schema used in `skills/search_web_news.md` before writing any new skill file — copy its key names exactly (do not invent new frontmatter conventions). You need `harness/agent_loop.py`'s Phase A tool-calling loop before writing the new orchestrator — that loop is the correct pattern, generalized. Do not reinvent the Gemini function-calling wiring from scratch; extract and reuse it.

## 1. What's actually wrong, precisely

Today, `main.py` runs a **fixed 7-step waterfall**: intake(company) → intake(type) → resolve_ticker → fetch_yfinance_data (fetches every field unconditionally) → research agent (the only real bounded tool loop) → chief editor → optional AML. The only two places a model decides what to do next are `harness/agent_loop.py`'s Phase A and `harness/aml_agent_loop.py`. Everything else — including which financial fields to pull, whether the resolved entity is actually correct, whether to ask the user something, and what the final report should emphasize — is hardcoded sequence, not a decision.

Concretely: (a) `resolve_ticker()` will silently pick *a* ticker for an ambiguous conglomerate name instead of asking, because nothing in the pipeline has a branch for "ask the user"; (b) `fetch_yfinance_data()` always pulls the full field set regardless of report type; (c) there is no step where the system looks at what it has gathered and decides it's insufficient and goes back for more; (d) every report of a given type gets the exact same section list with the exact same framing, so a sentiment report leads with market cap the same way a valuation report does, even though a sentiment reader cares about price *movement*, not the absolute figure.

## 2. What must NOT change (do not regress these — they are the reason this system is trustworthy)

- **No LLM ever authors a hard number.** All numeric data (prices, ratios, financials, sanctions hits) must continue to come from deterministic Python function calls (`tools/finance_tools.py`, `tools/aml_tools.py`), never from model text generation. The orchestrator's job is to decide *which* tool to call and *when* and *how to frame what was found*, never to compute or transcribe a figure itself.
- **The Chief Editor stays a single-shot, no-tool synthesis call** that writes prose strictly from a validated data snapshot plus a structured formatting plan (see §8a). Keep this separation between "agentic gathering/planning" and "grounded writing" — it's the right design and is the reason the report can't hallucinate figures. Do not let the orchestrator write report prose mid-loop.
- **Bounded loops, hard caps.** "Genuinely agentic" does not mean "unbounded." See §10 for exact numbers — these are deliberately conservative, agreed on for a specific reason: current stack is Tavily's free/Basic tier and `gemini-flash-3.5-lite`.
- **`render_config.yaml`-driven section ordering** becomes the fallback default (see §8a), not dead code. The `skills/*.md` + `harness/md_loader.py` YAML-frontmatter-to-`FunctionDeclaration` pattern, the PDF pipeline (`tools/pdf_tools.py`, `templates/`, `static/report.css`), and the `.agents/skills/*/SKILL.md` IDE docs all stay structurally as-is — you're extending this pattern, not replacing it.
- **The AML/ABC section's table format is fixed and does not get the dynamic-formatting treatment described in §8a.** Entity → source → finding → severity → citation is a regulatory-style disclosure, not a narrative section. Only Layer 1 sections adapt their framing per report.

## 3. Target architecture

Replace steps 2–6 of `main.py` (ticker resolution → market data → research agent → chief editor prep → AML) with **one master agentic loop**: Reason → Act → Observe → Validate → (loop or finalize). Steps 1 (cheap intent classification) and 7 (PDF render) stay as fixed pre/post steps.

```
User query (plain English)
        |
        v
[Seed] extract_company_reference() + detect_report_type()   <- keep as cheap priors, NOT hard gates
        |
        v
+-----------------------------------------------------------+
|         MASTER ORCHESTRATOR LOOP (harness/orchestrator.py)|
|                                                             |
|   REASON  -> model looks at AgentState + full tool menu,   |
|              decides next action                           |
|   ACT     -> orchestrator executes exactly one tool call   |
|   OBSERVE -> tool result is validated against its Pydantic |
|              schema and merged into AgentState             |
|   VALIDATE-> deterministic completeness/consistency check  |
|              against the report_type's requirement profile |
|              -> if incomplete/contradictory: loop back     |
|              -> if >1 ticker candidate: ask_user (ONLY      |
|                 human-interaction point in the system)     |
|              -> if satisfied or budget exhausted: plan      |
|                 report format (§8a), then finalize          |
+-----------------------------------------------------------+
        |
        v
run_chief_editor() [unchanged, single-shot, no tools, now also
                     consumes the orchestrator's ReportSpec]
        |
        v
[optional] AML section already merged into AgentState by the loop
        |
        v
compile_pdf() [unchanged]
```

`harness/agent_loop.py` and `harness/aml_agent_loop.py` fold into this loop as tool-selectable behaviors rather than separate hardcoded stages — see §6 for what to keep vs. merge.

## 4. New data model — add to `schemas.py`

```python
class AgentStatus(str, Enum):
    RUNNING = "running"
    AWAITING_USER = "awaiting_user"
    DONE = "done"
    FAILED = "failed"

class ToolCallRecord(BaseModel):
    turn: int
    tool_name: str
    arguments: dict
    result_summary: str          # short, for the trace log — not the raw payload
    ok: bool
    error: Optional[str] = None

class ClarificationRequest(BaseModel):
    question: str
    options: list[str]           # e.g. ["Tata Motors (TATAMOTORS.NS)", "TCS (TCS.NS)", ...]

class ValidationResult(BaseModel):
    satisfied: bool
    missing: list[str]           # data categories still needed for this report_type
    contradictions: list[str]    # e.g. "AML found a sanctions hit but sentiment is unambiguously bullish"
    notes: str

class SectionSpec(BaseModel):
    key: str                     # e.g. "financial_highlights"
    include: bool
    emphasis: str                # short directive to the Chief Editor — what leads, what's a footnote
    order: int

class ReportSpec(BaseModel):
    sections: list[SectionSpec]
    rationale: str               # why this shape — goes in the trace log, not the report itself

class RunTelemetry(BaseModel):
    gemini_calls: int = 0
    tavily_calls: int = 0
    tavily_calls_budget: int = 5
    wall_clock_seconds: float = 0.0

class AgentState(BaseModel):
    user_query: str
    status: AgentStatus = AgentStatus.RUNNING
    report_type: ReportType
    run_aml: bool
    company_reference: Optional[str] = None
    candidate_entities: list[dict] = []     # from resolve_entity, before disambiguation
    ticker: Optional[str] = None
    company_name: Optional[str] = None
    market_data: dict = {}                  # incrementally filled by granular fetch tools
    sentiment_findings: Optional[SentimentFindings] = None
    aml_result: Optional[AMLScreeningResult] = None
    report_spec: Optional[ReportSpec] = None
    pending_clarification: Optional[ClarificationRequest] = None
    tool_log: list[ToolCallRecord] = []
    telemetry: RunTelemetry = RunTelemetry()
    turn: int = 0
    max_turns: int = 20
```

Every field the orchestrator writes into `market_data`, `sentiment_findings`, `aml_result` must go through the *existing* Pydantic models in `schemas.py` (`MarketMetrics` etc.) before it's accepted — a tool result that fails schema validation is a failed observation, not silently-accepted partial data.

## 5. Tool registry

`fetch_yfinance_data()` currently pulls everything in one deterministic call. Split it into independently callable functions in `tools/finance_tools.py`, each wrapped as a skill under `skills/` following the exact frontmatter pattern in `skills/search_web_news.md`:

| New tool (skill) | Replaces part of | Returns | Counts against Tavily budget? |
|---|---|---|---|
| `get_price_snapshot(ticker)` | `fetch_yfinance_data` | price, market cap, 50d/200d MA, period high/low | no |
| `get_valuation_multiples(ticker)` | `fetch_yfinance_data` | P/E, forward P/E, P/B, P/S, EV/EBITDA, margins | no |
| `get_fundamentals(ticker)` | `fetch_yfinance_data` | EPS, D/E, ROE, ROCE, analyst consensus | no |
| `get_quarterly_financials(ticker)` | `fetch_yfinance_data` | quarterly financials table | no |
| `get_technicals(ticker)` | `fetch_yfinance_data` | RSI-14, MACD, volume trend, support/resistance | no |
| `get_ownership(ticker)` | `fetch_yfinance_data` | insider %, institutional % | no |
| `resolve_entity(query)` | `tools/ticker_resolver.py` | list of `{ticker, name, exchange, sector, confidence}` — dedupe and filter to genuinely valid candidates, don't return noise | no |
| `search_web_news(query, ticker, depth)` | unchanged (`agent_loop.py`'s tool) | Tavily results; `depth` is `"basic"` (1 credit) or `"advanced"` (2 credits), model's choice | **yes** |
| `run_structured_aml_sweep(entity_name, ticker)` | all of `tools/aml_tools.py`'s per-source functions, bundled | one call sweeps OFAC/OpenSanctions/World Bank/UN/EU/SEC EDGAR/TI CPI/FATF in one deterministic pass — see §9 for why this is bundled, not per-source | no |
| `search_adverse_media(entity_name, focus, depth)` | `harness/aml_agent_loop.py` | Tavily adverse-media results; `focus` narrows a follow-up (e.g. "why flagged on OFAC SDN") | **yes** |
| `ask_user(question, options)` | *new — does not exist today* | the user's typed choice; see §7. **The only tool that pauses the loop.** | no |
| `validate_data(state_snapshot)` | *new* | `ValidationResult` — deterministic completeness/consistency check | no |
| `plan_report_format(state_snapshot)` | *new* | `ReportSpec` — see §8a. Not a side-effecting call, just a structured planning output. | no |
| `finalize_report()` | *new* | signals the loop to exit RUNNING and hand off to `run_chief_editor` | no |

Every yfinance-backed and AML-source-backed tool stays deterministic Python underneath — you're only changing the granularity and dispatch of what's exposed to the model, not adding LLM involvement to the numbers themselves. `_INFO_FIELDS` in `finance_tools.py` should be partitioned across these functions so each only does the `.info` lookups / history computation it needs.

## 6. What happens to the existing loops

- `harness/agent_loop.py`'s Phase A (tool-calling) becomes the *pattern* the new master loop is built on — lift its Gemini wiring (contents management, function-call parsing, tool dispatch, retry via `harness/gemini_retry.py`) into `harness/orchestrator.py`. Its Phase B (JSON extraction into `SentimentFindings`) stays as-is and is invoked by the orchestrator once `search_web_news` has produced enough material.
- `harness/aml_agent.py`'s Phase 1 (structured sources) and Phase 3 (TI CPI/FATF) collapse into the single `run_structured_aml_sweep` tool (see §5, §9) — there's no genuine decision in "should I check OFAC," it's an unconditional sweep, so don't spend orchestrator turns asking the model to call each source individually. Phase 2 (adverse-media search) becomes `search_adverse_media`, and this is where the real AML reasoning happens: the orchestrator decides how many follow-up searches to run and with what focus, based on what the structured sweep found.
- `harness/intake.py`'s two single-shot calls stay, but downgrade their role: they seed `AgentState.company_reference` and `AgentState.report_type` as **priors**, not gates. The orchestrator may revise `report_type` mid-loop if evidence contradicts the initial classification, at most once, logged when it happens.

## 7. Human-in-the-loop disambiguation — the ONLY interaction point

Keep this rule as simple, cheap, and unambiguous as possible — no confidence thresholds, no "try to guess the most likely one first," no keyword-based tie-breaking logic in the orchestrator's reasoning. **If `resolve_entity` returns more than one candidate, call `ask_user`. Full stop. That is the entire rule, and it is the only tool in the entire registry that pauses the loop.**

The burden of not over-triggering this belongs to `resolve_entity` itself, not to the orchestrator's judgment: `resolve_entity` must dedupe by ticker and filter out low-confidence noise before returning, so that "more than one candidate" reliably means "genuinely more than one plausible target," not "the search API returned some garbage alongside the right answer." Get that filtering right in the tool, and the orchestrator's rule can stay a dumb, cheap, zero-judgment check — which is exactly what you want for the one place a wrong autonomous guess poisons every downstream number in the report.

**Detection.** Don't rely on yfinance's fuzzy search alone for group-name collisions — Indian conglomerates (Tata, Reliance, Aditya Birla, Mahindra, Adani, Bajaj) won't reliably disambiguate from a bare group name through search matching. Add a small curated `tools/conglomerate_map.yaml` (group name → list of `{name, ticker, exchange}` for its listed entities) that `resolve_entity` checks first; fall back to yfinance search only when the query isn't a known group name. Merge both result sets, dedupe, and return the filtered candidate list. Keep this file small and hand-maintained — same spirit as the existing `_STATIC_MAP` in `tools/ticker_resolver.py`.

**Every other ambiguity in the system is resolved autonomously, not asked about.** State this explicitly in the orchestrator prompt (§11) as a list of non-triggers, so the model doesn't invent reasons to pause:
- Report type unclear from the query → infer the best fit, proceed, state the interpretation in the report's opening line rather than asking.
- A data field is unavailable → use the existing "data unavailable" pattern, don't stop.
- A news search returns thin/irrelevant results → reformulate and retry within the search budget, don't ask the user to narrow it down.
- Dual-listed entity (NSE vs BSE) → default to the primary listing per existing convention in `ticker_resolver.py`, no need to ask.
- How much AML depth is warranted → decided by the loop's own validation logic (§9), never punted to the user.
- What the report should emphasize/how it should be structured → decided by `plan_report_format` (§8a), never asked.

**Mechanics of `ask_user`.** This tool is different from every other tool in the registry: it is a genuine pause, not an automated function call.

```python
def ask_user(question: str, options: list[str]) -> str:
    print(f"\n{question}")
    for i, opt in enumerate(options, 1):
        print(f"  {i}. {opt}")
    while True:
        raw = input("> ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1]
        match = [o for o in options if raw.lower() in o.lower()]
        if len(match) == 1:
            return match[0]
        print("Didn't catch that — type the number or the name.")
```

The orchestrator sets `AgentState.status = AWAITING_USER` and `pending_clarification` before calling this, appends the returned choice as a function response to the Gemini `contents` list exactly like any other tool result, then resumes with `status = RUNNING`. If you later expose this outside the CLI (an API), `ask_user` needs to become an async pause that serializes `AgentState` and returns a `202` with the clarification payload, resuming from a POSTed answer — flag this with a `# TODO: swap for async pause when moving off CLI` comment, but implement the CLI version now.

**Example trace:**

```
> python main.py "valuation report of Tata"

[reason] Checking for candidates.
[act] resolve_entity("Tata")
[observe] 6 candidates found via conglomerate_map: TATAMOTORS.NS, TCS.NS, TATASTEEL.NS,
          TATAPOWER.NS, TATACONSUM.NS, TITAN.NS
[reason] More than one candidate -> must ask. No further reasoning about which is "likely."
[act] ask_user(...)

Which Tata Group company did you mean?
  1. Tata Motors (TATAMOTORS.NS)
  2. Tata Consultancy Services (TCS.NS)
  3. Tata Steel (TATASTEEL.NS)
  4. Tata Power (TATAPOWER.NS)
  5. Tata Consumer Products (TATACONSUM.NS)
  6. Titan Company (TITAN.NS)
> TCS

[observe] User selected TCS.NS
[reason] report_type=VALUATION. Required categories: price_snapshot, valuation_multiples, fundamentals.
[act] get_price_snapshot(TCS.NS)
[act] get_valuation_multiples(TCS.NS)
[act] get_fundamentals(TCS.NS)
[act] validate_data(...)
[observe] satisfied=True, missing=[]
[act] search_web_news("TCS Q3 results outlook", ticker="TCS.NS", depth="basic")
[act] plan_report_format(...)
[observe] ReportSpec: valuation_multiples leads, fundamentals second, technicals excluded
           (not required for a pure valuation ask), rationale logged.
[act] finalize_report()
```

## 8. Report-type requirement profiles — the guardrail that keeps autonomy from being reckless

Full autonomy over which data to fetch is the goal, but an ungoverned model deciding "I have enough" for a finance report is a real risk. Resolve this with a **soft checklist, not a hard branch** — a new `orchestrator_config.yaml` mapping report type to required and optional data categories:

```yaml
VALUATION:
  required: [price_snapshot, valuation_multiples, fundamentals]
  optional: [technicals, ownership, quarterly_financials]
  min_news_searches: 1
TECHNICAL:
  required: [price_snapshot, technicals]
  optional: [valuation_multiples]
  min_news_searches: 0
FULL_EQUITY:
  required: [price_snapshot, valuation_multiples, fundamentals, technicals, ownership]
  optional: [quarterly_financials]
  min_news_searches: 2
SENTIMENT:
  required: [price_snapshot]
  optional: [valuation_multiples]
  min_news_searches: 3
GENERAL:
  required: [price_snapshot]
  optional: [valuation_multiples, fundamentals, technicals]
  min_news_searches: 1
```

`validate_data` is a **deterministic** function (not another LLM call) that checks `AgentState.market_data` keys and `len(sentiment_findings.queries_used)` against this profile and returns a `ValidationResult`. The model has full discretion over *order*, *which optional categories matter for this specific query*, and *when to stop searching news beyond the minimum* — but it cannot call `finalize_report` while `satisfied=False` on `required` categories. This is the actual "learn" step in reason→act→learn→perceive→reason: the loop's own validator tells the model what's still missing, and the model incorporates that into its next Reason turn.

### 8a. Agentic report format — `plan_report_format` and `ReportSpec`

This is the mechanism for your core requirement: **the same report_type should not produce the same report shape every time, and a sentiment report should not treat market cap the way a valuation report does.**

Once `validate_data` returns `satisfied=True`, the orchestrator's next action (before `finalize_report`) is `plan_report_format`, which produces a `ReportSpec` (§4): for every candidate section, `include`, `order`, and an `emphasis` string — a short directive telling the Chief Editor what leads and what's a footnote, given what was actually found and why the user asked. Concretely: for a SENTIMENT report, the Financial Highlights section's `emphasis` should say something like *"lead with % price movement, MA crossovers, and volume trend; market cap is a supporting data point, not a headline"* — for a VALUATION report on the same company, the emphasis for that same section flips.

`_SECTION_INSTRUCTION_MAP` in `harness/synthesis.py` takes each `SectionSpec.emphasis` as an override layered on top of its existing base instruction, rather than using a fixed instruction regardless of report type. `render_config.yaml`'s per-type list becomes the **fallback default** — used if `plan_report_format` fails validation or is skipped — not the only possible shape. This keeps the safety net without treating it as the ceiling.

Two things are explicitly exempt from this dynamic treatment, per §2:
- **The chart stays hardcoded at the top** of `templates/report_template.html` — not a `SectionSpec` decision.
- **The AML/ABC section keeps its fixed tabular format.** Only Layer 1 narrative sections adapt.

Log `ReportSpec.rationale` to the trace file — it's the model's own stated reasoning for the structural choice, useful for debugging and for explaining to anyone reviewing the output why two reports on the same company look different.

## 9. AML autonomy (flag-gated, agentic inside)

Keep `--aml` (and, if you want, natural-language detection of intent — "with AML screening", "sanctions check", "ABC risk" in the query text) as the gate for whether the AML branch runs at all. Inside that branch:

- `run_structured_aml_sweep` always runs in full — every one of OFAC/OpenSanctions/World Bank/UN/EU/SEC EDGAR/TI CPI/FATF gets checked, unconditionally, in one bundled deterministic call. There's no decision here, so don't spend orchestrator turns pretending there is.
- After the sweep returns, the orchestrator reasons about `search_adverse_media` depth: if every structured source came back clean, run it once at `depth="basic"` for baseline coverage and stop. If **any** structured source returns a hit, run a **targeted follow-up** with `focus` built from the specific finding (e.g. `focus="reason for OFAC SDN listing"`, `depth="advanced"` if the basic pass was too shallow to be useful). This is where genuine AML reasoning shows up — clean-across-the-board gets minimal digging, a real hit gets targeted digging.
- Both `search_web_news` and `search_adverse_media` draw from the **same shared Tavily budget** (§10) — the orchestrator allocates it across research vs. AML itself, it isn't pre-split by stage.
- `render_aml_markdown()` stays a deterministic formatter reading from `AMLScreeningResult` — the loop's autonomy is in *what gets searched*, never in how findings are phrased or structured.

## 10. Guardrails — finalized numbers

These are deliberately conservative given the current stack: Tavily's free/Basic tier and `gemini-flash-3.5-lite`. Re-verify against `ai.google.dev`'s rate-limit page and `docs.tavily.com/documentation/rate-limits` before shipping — these figures drift and vary by exact pinned model version.

- **`AgentState.max_turns = 20`** for the master orchestrator loop. If exceeded, force-transition to `FAILED` with a clear error stating exactly which `required` categories were still unsatisfied at cutoff — don't loop silently forever. The `run_structured_aml_sweep` bundling (§6, §9) is what keeps 20 turns sufficient even for a `FULL_EQUITY` + `--aml` run: granular finance fetches (~6 max) + resolve_entity (1) + validate_data calls (2-3) + Tavily-backed calls (up to 5, shared budget) + one AML sweep (1) + plan_report_format (1) + finalize_report (1) comfortably fits inside 20.
- **Gemini RPM target: 12**, not the ~15 RPM ceiling reported for Flash-Lite tiers — deliberate headroom, not the max. This is a soft pacing target, not a hard sleep; `harness/gemini_retry.py`'s existing 429-aware backoff remains the actual defense against bursts. In practice each orchestrator turn already waits on a real tool round-trip (yfinance or Tavily latency), so RPM is unlikely to be the binding constraint in normal operation.
- **Tavily budget: 5 calls per run, total, shared** across `search_web_news` and `search_adverse_media` combined — tracked in `AgentState.telemetry.tavily_calls` against `tavily_calls_budget = 5`. The orchestrator allocates this budget itself between research and AML depth; don't pre-split it into separate per-phase caps. If a run needs all 5 and the system doesn't fail, that run earned it — this is not a tight cap to optimize against, it's a ceiling to prevent runaway search loops.
- **Diminishing-returns early stop**: if two consecutive Tavily calls (either tool) return results with no new named entities/catalysts/findings versus what's already in `AgentState`, stop searching for that phase rather than spending the rest of the budget waiting. Compare on result URLs/titles already ingested — cheap, no extra LLM call needed.
- **Idempotency check** before executing any tool call: if the exact `(tool_name, arguments)` pair already exists in `tool_log` with `ok=True`, skip re-execution and feed the cached result back.
- **Zero-candidate resolution stays fail-closed**, exactly as `main.py` does today: one retry with a broadened query, then abort with "Could not resolve a ticker... no report is generated on unresolved/guessed data." Not up for autonomy.
- **Pre-finalize consistency check**, separate from `validate_data`'s completeness check: a deterministic pass flagging things like "AML found a hit but the narrative sentiment section is unambiguously bullish with no mention of it" or "P/E multiple present but EPS is null." These don't block the run, but log them to the trace as warnings.
- **Per-run telemetry**, populated into `AgentState.telemetry` and dumped alongside the trace: total Gemini calls, total Tavily calls used against budget, wall-clock seconds. This is how you catch which report types are getting expensive before it becomes a rate-limit problem mid-semester, and it's free to compute since you're already logging every tool call.
- Every tool call and its outcome goes into `tool_log`; at the end of the run, dump `AgentState.tool_log`, `telemetry`, and `report_spec.rationale` to `outputs/TICKER_DATE_trace.json` alongside the PDF. Not optional — this is what makes "genuinely agentic" demonstrable and is your own debugging tool.

## 11. Orchestrator system prompt — draft for `agents/orchestrator.md`

Write this file following the same frontmatter/prose convention as `agents/research_analyst.md`. Content to include:

- Role: you are the planning agent for a financial report pipeline. You decide which tools to call, when, and how the final report should be framed; you never write report prose or invent numbers.
- The full tool menu with one-line descriptions (pull from the skill files, don't duplicate schemas in prose).
- **The one hard interaction rule, stated exactly**: *"If `resolve_entity` returns more than one candidate, call `ask_user` immediately. This is the only situation in which you pause. Do not attempt to guess which candidate is most likely, do not apply confidence thresholds, do not look for disambiguating keywords in the query — more than one candidate is sufficient and necessary to ask, nothing else in this system asks."*
- **The autonomy default, stated exactly**: *"In every other situation — ambiguous report type, missing or unavailable data, thin search results, unclear AML depth, how the report should be structured — you decide and proceed. Do the best version of the report the available data supports. Note limitations inline rather than stopping."* Include the non-trigger list from §7 verbatim.
- You must not call `finalize_report` while `validate_data` reports unsatisfied `required` categories for the current `report_type`.
- You must call `plan_report_format` after data gathering is validated complete and before `finalize_report` — every numeric claim in the eventual report must trace to a tool result already in state, but *how that result is framed* is your call to make per-run.
- Respect the shared Tavily budget and `max_turns`; if close to budget with only optional categories missing, finalize with what you have rather than risk `FAILED`.
- Permit revising `report_type` once if evidence contradicts the initial classification, log why.

## 12. Implementation order

1. `schemas.py` additions (§4).
2. Split `tools/finance_tools.py` into the granular functions (§5), partitioning `_INFO_FIELDS` per function.
3. `tools/conglomerate_map.yaml` + updated `resolve_entity` in `tools/ticker_resolver.py` returning deduped, filtered multi-candidate results.
4. Bundle `tools/aml_tools.py`'s structured screeners into `run_structured_aml_sweep`.
5. New skill files under `skills/` for every tool in §5's table, mirroring `skills/search_web_news.md`'s frontmatter exactly.
6. `orchestrator_config.yaml` (§8).
7. `agents/orchestrator.md` (§11).
8. `harness/orchestrator.py` — the master loop, generalizing `harness/agent_loop.py`'s Phase A wiring, calling Phase B and the AML sweep as internal helpers.
9. Wire `ask_user` (§7) with the pause/resume semantics described, and `plan_report_format`/`ReportSpec` (§8a), and thread `ReportSpec` into `harness/synthesis.py`'s `_SECTION_INSTRUCTION_MAP`.
10. Implement diminishing-returns early stop, idempotency check, pre-finalize consistency check, and `RunTelemetry` logging (§10).
11. Update `main.py`: replace steps 2–6 with a single call into `harness/orchestrator.py`; keep step 1 (intake priors) and step 7 (PDF render) as-is; keep `--aml` flag behavior, add optional natural-language AML trigger if you choose to.
12. Update `ARCHITECTURE.md`'s pipeline diagram and file-by-file map to reflect the orchestrator-centric flow.
13. Tests under `tests/`: the Tata disambiguation trace, a clean single-ticker valuation run, a same-company sentiment-vs-valuation run showing materially different `ReportSpec` emphasis, a `--aml` run with a clean sweep, a `--aml` run where a structured hit triggers a targeted adverse-media follow-up, and a diminishing-returns early-stop case (mock two repeated search results).

## 13. Acceptance criteria

- `python main.py "valuation report of Tata"` pauses on a numbered disambiguation prompt and fetches no market data before the user answers.
- `python main.py "valuation analysis of TCS"` runs with **no** technicals or ownership fetch calls in the trace, and does not pause.
- Running both `python main.py "valuation analysis of TCS"` and `python main.py "news sentiment report of TCS"` back to back produces two PDFs whose Financial Highlights framing is *visibly* different — the sentiment run's `ReportSpec.rationale` in the trace should explicitly justify leading with movement/momentum over absolute market cap.
- `python main.py "full equity report on TCS" --aml` produces a trace showing one `run_structured_aml_sweep` call plus at least one `search_adverse_media` call, all Tavily calls counted against a single shared budget of 5, and the PDF's AML section is unchanged in format from today's output regardless of what `ReportSpec` did to Layer 1.
- Forcing a fake ambiguous structured-source hit (mock one screener to return a match) results in a *second, targeted* `search_adverse_media` call with a `focus` argument derived from that finding.
- `outputs/TICKER_DATE_trace.json` exists after every run and shows a legible reason/act/observe/validate sequence, `RunTelemetry`, and `ReportSpec.rationale`.
- A mocked repeated-search-results test demonstrates the diminishing-returns stop actually cuts a search phase short before the shared Tavily budget is exhausted.
- No test or manual run ever shows a numeric value in the final PDF that doesn't trace back to a tool result in `tool_log`.
