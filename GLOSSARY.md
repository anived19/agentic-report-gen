# Glossary

Plain-English definitions of every agentic and technical concept used in this codebase.
Written for someone who knows Python but has not built with LLM-based agent frameworks before.

---

## A

### Agent
A program where an LLM decides what to do next at runtime, not just at design time. In this codebase, there are two agents: the **Research Agent** (Layer 1) and the **AML Adverse Media Agent** (Layer 2). Both follow the ReAct pattern: reason, then act (call a tool), then reason again based on the result. Everything else in this pipeline is deterministic Python — it is not "agentic" in any meaningful sense.

### Agent Loop
The `for` loop in `harness/agent_loop.py` (and `harness/aml_agent_loop.py`) that runs the Gemini model repeatedly until it stops calling tools or hits the turn budget. Each iteration: send the current conversation history to the model → get a response → if the model wants to call a tool, run it and append the result → repeat.

### Agent Prompt / System Instruction
The text in `agents/*.md` that is passed as `system_instruction` to every Gemini call in a given agent loop. Think of it as the agent's standing orders — it doesn't change during a run. For the Research Agent, it says: "only cite URLs you actually retrieved; use your turn budget deliberately." For the Chief Editor, it says: "only write numbers from the JSON I give you; never invent."

---

## B

### Bounded Loop
A loop with a hard ceiling on the number of iterations — in this project, `config.research_agent_max_turns` (default 4) for Layer 1 and 3 for the AML adverse media phase. "Bounded" is what separates a responsible agentic design from one that can spiral into infinite API calls. The model is told about its budget in the system prompt; the code enforces it unconditionally regardless of what the model says.

---

## C

### Chief Editor
The single-shot LLM call in `harness/synthesis.py` that takes validated market data + sentiment findings and produces the final Markdown report. It is deliberately **not** agentic: no tools, no loop, no decision about what to query next. By the time it runs, all the facts are already fetched and validated. Its job is formatting and synthesis only.

### CitedClaim
The Pydantic model in `schemas.py` that enforces the citation discipline: every claim must have a `source_url` that is a real `HttpUrl`. The Research Agent is forced to produce `key_catalysts` and `key_risks` as `CitedClaim` objects — if it tries to return an empty URL or a malformed one, Pydantic validation fails loudly before the claim reaches the PDF.

### Contents List
The `list[types.Content]` variable that holds the conversation history for a Gemini model session. Each call to `generate_content` takes this list; the response is appended to it before the next call. This is how multi-turn "memory" works in the Gemini SDK — the model re-reads the entire history on each turn.

---

## D

### Deterministic
A stage where the code always produces the same output for the same input, with no LLM involved. `tools/finance_tools.py` (yfinance fetch), `tools/pdf_tools.py` (HTML → PDF), and `tools/aml_tools.py` (structured source queries) are all deterministic. This is a design choice: hard numbers should never pass through an LLM call, because the model might paraphrase them incorrectly.

---

## F

### FinalReport
The Pydantic model that carries everything the PDF renderer needs: the ticker, company name, the Chief Editor's Markdown body, the validated `MarketMetrics`, the `SentimentFindings`, and optionally an `AMLScreeningResult`. It's a schema, not a class with methods — once assembled, it is passed to `compile_pdf()` and never modified.

### Frontmatter
The YAML block between `---` delimiters at the top of each `.md` file in `skills/` and `agents/`. For skill files, it contains the machine-read function schema (name, description, parameters). For agent files, it is documentation-only — only the prose body below it is loaded at runtime. `harness/md_loader.py:_split_frontmatter()` handles the parsing.

### FunctionDeclaration
The Gemini SDK's way of describing a tool the model can call — equivalent to an OpenAI "function definition." `harness/md_loader.py` builds one from each skill's YAML frontmatter and wraps it in a `types.Tool` before passing it to `generate_content`.

---

## H

### Harness
The shared execution layer in the `harness/` directory. It handles: running Gemini calls with retry logic, managing conversation history, routing tool calls to real Python functions, and validating outputs. You modify the harness only if you need to change *how* the pipeline runs — not to change *what* the report contains (for that, edit agent prompts or `render_config.yaml`).

---

## M

### MarketMetrics
The Pydantic model that holds all fetched market data for one ticker — price, moving averages, RSI, MACD, volume trend, valuation multiples, earnings, analyst ratings, holdings, and quarterly financials. It is populated deterministically by `tools/finance_tools.py` and passed read-only to both the Chief Editor and the PDF renderer. The LLM reads it but never writes to it.

---

## P

### Phase A / Phase B
The two sub-phases of the Research Agent loop in `harness/agent_loop.py`:
- **Phase A**: The tool-calling loop. The model can call `search_web_news` repeatedly. It decides its own queries and when it has enough.
- **Phase B**: One final Gemini call, same conversation history, tools disabled, with `response_mime_type=application/json` and a JSON schema constraint. Forces a clean, Pydantic-validatable `SentimentFindings` object out of the accumulated conversation. Kept separate because you can't reliably get structured JSON output and tool calls in the same API call.

### Progressive Disclosure
The principle that an agent (or IDE) should only load the context it currently needs, not everything at once. The `.agents/skills/*.md` files follow this: they are discovered by the IDE but only loaded into context when the skill's trigger condition matches the current task. This keeps the model's context window from filling up with irrelevant information.

---

## R

### ReAct (Reason + Act)
The pattern the Research Agent uses: after each tool call result, the model reasons about what it found before deciding whether to call another tool or stop. It's not a formal algorithm in this codebase — it's the natural behavior of the Gemini model when given a system prompt that says "use your search tool deliberately, reason about whether you have enough."

### render_config.yaml
The single source of truth for which sections appear in each report type and in what order. Decouples section ordering from Python code — adding or reordering a section is a YAML edit, not a code change. `harness/synthesis.py` reads it at startup.

---

## S

### SentimentFindings
The Pydantic model output of the Research Agent: an `overall_sentiment` label (Bullish / Bearish / Neutral), a `sentiment_summary` string, and lists of `CitedClaim` objects for catalysts and risks. Validated by Pydantic before the Chief Editor ever sees it — if the model returns uncited claims or an invalid URL, the pipeline fails at validation, not silently downstream.

### Single-Shot Call
A Gemini `generate_content` call with no tools and no follow-up — one request, one response. Intake classification and the Chief Editor synthesis are both single-shot calls. They're used where the task is deterministic enough that you don't need the model to decide what to do next.

### SkillBundle
The Python dataclass in `harness/md_loader.py` that pairs a `FunctionDeclaration` (what the model sees) with the actual Python callable it maps to (what gets run). When the model asks to call `search_web_news`, the harness looks up the `SkillBundle` for that name and calls `bundle.function(**args)`.

### Structured Output
When a Gemini call is configured with `response_mime_type="application/json"` and a `response_json_schema`. This tells the model to emit only valid JSON matching the schema. Used in Phase B of both agent loops to force clean, Pydantic-validatable output instead of hoping the model spontaneously writes valid JSON.

---

## T

### Turn Budget
The maximum number of times the model can call a tool in one agent run. Set via `config.research_agent_max_turns` (default 4 for Layer 1, 3 for AML adverse media). When the budget is exhausted, the harness sends a "conclude now" message and proceeds to Phase B. The model is told about the budget in its system prompt, but the code enforces it unconditionally.

---

## U

### unavailable_fields
A list on `MarketMetrics` that records every field yfinance couldn't supply for a given ticker. When the Chief Editor receives this list, it is instructed to write "data unavailable" for those fields rather than guessing or omitting them. This is the mechanism that makes the "no fabricated data" constraint enforceable across all tickers, not just ones with perfect yfinance coverage.
