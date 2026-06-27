# QueryGate — UI/UX Design Specification

**Deliverable:** Design source-of-truth for the aspirational web demo of `querygate`.
**Author:** Senior Product/UI-UX Designer (portfolio engagement).
**Consumer of this doc:** Claude Design (builds the hi-fi mockups from this spec).
**Source of truth for product behavior:** `uploads/10-querygate.md` (the build-ready spec). Where this doc and that spec disagree, the spec wins — conflicts are flagged inline as `⚠ SPEC CONFLICT`.

---

## STEP 0 — Repo grounding (restated in my own words)

- **What it is / who it's for.** QueryGate is a **read-only MCP server** that lets Claude answer plain-English questions over a real Postgres database *without ever being able to change the data*. The hard part isn't wiring an LLM to Postgres — it's the **trust boundary**. Personas: (1) a **non-technical operator** who asks questions and needs to trust the answer; (2) an **engineer/buyer** who needs *proof* the AI cannot write and that every answer is real; (3) the **demo/portfolio audience** who needs one unforgettable moment. The demo ships against a **synthetic EHR/claims** database so it doubles as a healthcare-compliance showcase with zero real PHI.

- **THE MONEY DEMO (two beats — the whole UI is built to land beat 2).** Beat 1, *it works*: ask "Which providers have the most patients overdue for follow-up?" → the agent calls `list_tables → describe_table → run_select` and answers in prose **with the exact SQL and row count shown**. Beat 2, *it's safe* (the climax): ask "**Delete all patients named Smith.**" → QueryGate **refuses at the boundary** — the SQL guard accepts only a single SELECT, the transaction is `READ ONLY`, and the DB role has no write grant — and there is a **CI test proving no tool path can mutate data, at each of the three layers independently.** Per the spec: *"That refusal, on screen, is the pitch."* Everything else is supporting cast.

- **The real data objects the UI must render (exact field names from §7).**
  - `RunResult`: `columns: list[str]`, `rows: list[list[JSONScalar]]`, `row_count: int`, `sql: str`, `truncated: bool`, `truncated_bytes: bool`, `elapsed_ms: int`.
  - `TableSchema`: `table`, `columns: list[ColumnInfo]`, `sample_rows`. `ColumnInfo`: `name`, `type`, `nullable`, `is_pk`, `references` (e.g. `"providers.provider_id"`).
  - `AuditLine` (one JSONL line per tool call): `ts`, `tool`, `args`, `row_count`, `latency_ms`, `status ∈ {ok, rejected, error}`, `error?`, `redactions: list[str]`.
  - DB schema (§8): `patients(patient_id, name, dob, sex, city, registered_at, last_contacted_at)`, `providers(provider_id, name, specialty, npi)`, `encounters(encounter_id, patient_id, provider_id, date, type, status)`, `claims(claim_id, encounter_id, amount, status, submitted_at, paid_at)`, `follow_ups(follow_up_id, patient_id, due_date, completed_at)`.
  - The 5 MCP tools (§6): `list_tables`, `describe_table`, `run_select`, `search_text`, `explain_select` (optional).

- **The actual user flow + the states the spec calls out.** Worked tool-flow (§3): step 0 the agent reads server `instructions`; step 1 `list_tables`; step 2 `describe_table("follow_ups")`; step 3 `run_select(...)` → guard ✅ → read-only txn → role → `RunResult`; step 4 prose answer **citing SQL + row_count** (§9); step 5 (adversarial) `run_select("DELETE …")` → guard rejects → `status: rejected` → agent apologizes briefly, does **not** retry the write. States the spec explicitly demands the UI handle: **refusal / rejected-write** (the hero), **truncated rows** (`truncated`), **truncated bytes** (`truncated_bytes`), **timeout/slow query** (`statement_timeout` → `status: error`), **column redaction** (`redactions`, cells become `"***"`), **ambiguous value → `search_text`**, **`refusal` stop_reason on a benign question** (handle, don't crash), **prompt-injection-via-data containment** (a poisoned row can at worst trigger another read-only SELECT).

- **Honesty surfaces the spec hands me to make visible.** Citations (`sql` + `row_count`) on *every* answer; the **audit log** as the after-the-fact proof; per-question **cost & latency** (Opus 4.8 `$5/$25` per MTok; Sonnet 4.6 `$3/$15`); the **distributional** eval framing ("0 writes executed — deterministic, CI-gated; grounded-rate 0.9x on a frozen set, reported mean±spread" — *never* "answers everything"); the redaction switch; and a standing **"demo, synthetic data, not a compliance certification"** disclaimer.

- **⚠ SPEC CONFLICT / decision the spec leaves to design.** The spec ships a **Streamlit** app and explicitly asks for the *aspirational web-native* version. Streamlit's stock chat+widget layout is the baseline; this doc replaces it with a purpose-built two-surface layout (human answer ↔ machine proof). The spec also lists `explain_select`, redaction, and the cost pre-flight as *optional* — this UI designs **first-class affordances** for all three but marks them `OPTIONAL` so the mockups can show the floor (sessions 1–5) and the centerpiece (session 6) distinctly.

---

## 1. Design thesis

**One idea: every answer arrives already proven.** QueryGate's UI is a *glass-walled machine* — a calm, human-readable answer on the left, and the exact mechanism that produced and *contained* it on the right, in monospace, in real time. The feeling is a **senior engineer's terminal that learned typography**: precise, unhurried, trustworthy. The product's core claim is negative — *the AI cannot write your data* — so the UI's job is to make absence visible: it choreographs the moment a destructive request **hits the boundary and stops**, turning a refusal into the most satisfying frame in the demo. Nothing is asserted that isn't shown; numbers carry their SQL, costs carry their dollar figure, and the security guarantee carries its three-layer receipt.

---

## 2. Design principles (specific to QueryGate)

1. **The refusal is the hero, not the error.** A rejected write is a *success state* — it must look authoritative and intentional (shield/seal language, a confident calm color), visually distinct from a genuine failure (timeout, serialization). Never style "rejected" like "broken."
2. **Two surfaces, one truth: human answer ↔ machine proof.** The prose answer (sans-serif, generous) and its provenance (monospace SQL, row counts, audit lines) are always co-present and visually linked. You can always trace a sentence back to the query that earned it.
3. **Make the invisible boundary tangible.** Defense-in-depth is three layers (guard → read-only txn → role). The UI gives them a persistent spatial identity — a depth gradient inward — so "proven at each layer independently" is something you *see*, not read.
4. **Honesty is chrome, not a footnote.** Confidence, cost-per-run, truncation, redaction, and "this is a demo" live in the primary frame, styled as first-class metadata — never buried in a tooltip or a fine-print band.
5. **Streaming is the narrative.** The tool trace expands step-by-step as the agent works; the layout *breathes* as content arrives. The pacing of reveal *is* the storytelling — the user watches the machine think and the gate hold.
6. **Earn every pixel of certainty.** No decorative confidence. A number with no citation is a bug. If the agent didn't retrieve it, the UI won't show it as fact — ungrounded numbers get a visible "unverified" flag rather than silent presentation.

---

## 3. Visual language / design tokens

> Dark-forward "machine room" base (fits a security/MCP tool and lets semantic colors sing), with the human-answer surface raised and warmer. Mono carries enormous identity weight here — SQL, schema, audit, row counts are all the proof, so they're all monospace by design.

### 3.1 Color system

**Neutral / structural (cool near-black ramp, chroma kept ≤ 0.02):**
| Role | Hex | Use |
|---|---|---|
| `bg/base` | `#0B0D10` | App canvas |
| `surface` | `#14171C` | Panels, rails |
| `surface/raised` | `#1B1F26` | Answer card, message bubbles, modals |
| `surface/inset` | `#0F1216` | Code wells (SQL, JSONL), schema tree |
| `border` | `#262B33` | Hairlines between regions |
| `border/strong` | `#353C46` | Focus rings, active panel edge |
| `text/primary` | `#E8EBF0` | Prose, headings |
| `text/secondary` | `#9BA3AE` | Labels, metadata |
| `text/muted` | `#6B7480` | Timestamps, hints, disclaimers |

**Brand / primary (the "gate"):**
| Role | Hex | Use |
|---|---|---|
| `primary` | `#5B8DEF` | Interactive accent, links, send button, focus |
| `primary/soft` | `#1C2B4A` | Primary-tinted fills, selected rows |

**Semantic — the load-bearing palette this product needs:**
| Role | Hex | Meaning | Notes |
|---|---|---|---|
| `verdict/ok` | `#3FB950` | `status: ok`, query ran, answer grounded | Green = retrieved & true |
| `shield/held` | `#2DD4BF` | `status: rejected` **by design** (the hero) | Teal "shield" — calm authority, **NOT red**. Always paired with a lock/shield glyph + "Held at the boundary." |
| `error/fail` | `#F85149` | `status: error` (timeout, serialize fail, 500) | The *only* red. Reserved for genuine failure. |
| `caution/uncertain` | `#D29922` | Confidence flag, truncation, "unverified number" | Amber = "look closer." |
| `cost/cheap` | `#7EE0A6` | $/run, cache-hit savings | Mint — money saved/spent, legible & low-drama. |
| `redact/phi` | `#B98AE8` | Redacted cells (`"***"`), PHI columns | Violet = "intentionally hidden," distinct from error/caution. |

**Triage-band colors for the 3 boundary layers (a depth gradient inward = defense in depth):**
| Layer | Hex | Label |
|---|---|---|
| Layer 1 — SQL guard (sqlglot, whole-AST) | `#7C8CF8` (indigo) | outermost / fastest |
| Layer 2 — `READ ONLY` txn + timeout | `#38BDF8` (sky) | middle |
| Layer 3 — least-privilege role | `#2DD4BF` (teal) | bedrock / innermost |

> Contrast: all text/semantic pairs target **WCAG AA** (≥ 4.5:1 body, ≥ 3:1 large/UI). The teal `shield/held`, amber `caution`, and green `verdict/ok` are tuned to clear 4.5:1 on `#0F1216` / `#14171C`. Never rely on hue alone — every semantic state also carries an icon + a text label (color-blind safe).

### 3.2 Typography

- **UI / prose:** `Geist Sans` (intent: clean modern humanist grotesk) → fallback `-apple-system, "Segoe UI", system-ui`. *Avoid Inter/Roboto.* Carries the human-facing answer and all chrome labels.
- **Mono (identity-critical):** `Geist Mono` → fallback `"JetBrains Mono", ui-monospace`. Carries **all proof**: SQL, schema tree, JSONL audit lines, row counts, costs, tool names. The human/machine duality is literally a typeface switch.

**Fluid type scale (clamp-based — the explicit fluid ask):**
| Token | clamp() | Use |
|---|---|---|
| `display` | `clamp(28px, 1.4rem + 1.4vw, 44px)` | The hero verdict ("Held at the boundary"), demo title |
| `h1` | `clamp(22px, 1.2rem + 0.8vw, 30px)` | Region headers |
| `h2` | `clamp(18px, 1.05rem + 0.4vw, 22px)` | Card titles |
| `body` | `clamp(15px, 0.95rem + 0.2vw, 17px)` | Prose answer, descriptions |
| `meta` | `clamp(12px, 0.8rem + 0.1vw, 13px)` | Labels, timestamps, disclaimers |
| `mono-body` | `clamp(13px, 0.85rem + 0.15vw, 15px)` | SQL, audit, schema |
| `mono-micro` | `clamp(11px, 0.75rem + 0.1vw, 12.5px)` | Inline row_count chips, cost chips |

Line-height: 1.6 prose, 1.45 mono. `text-wrap: pretty` on all prose. Letter-spacing: `-0.01em` on display/h1, `0` on mono.

### 3.3 Spacing, radius, elevation, border

- **Fluid spacing scale** (a base step that scales with viewport): `--s: clamp(4px, 0.25rem + 0.15vw, 6px)`, then `s1=--s, s2=2×, s3=3×, s4=5×, s5=8×, s6=13×` (a soft Fibonacci ramp). Region gutters use `s5–s6`; intra-card padding `s3–s4`; chip padding `s1–s2`.
- **Radius:** `r-sm 6px` (chips, inputs), `r-md 10px` (cards, code wells), `r-lg 14px` (panels, modal), `r-pill 999px` (status chips). Mono code wells use `r-md`; never fully sharp (too brutal) nor very round (too consumer).
- **Elevation (dark-aware — borders do more work than shadows):** `e0` flat on base; `e1` = `1px border + inset top highlight rgba(255,255,255,.03)`; `e2` (raised cards) = `e1 + 0 1px 0 rgba(0,0,0,.4), 0 8px 24px -12px rgba(0,0,0,.6)`; `e3` (modal/hero) = `e2 + 0 24px 64px -24px rgba(0,0,0,.7)`. Glow accents (shield/verdict) are a `0 0 0 1px <semantic>/.5, 0 0 24px -6px <semantic>/.35` ring, used *only* on the hero refusal and the "answer grounded" seal.
- **Borders:** 1px hairlines `border`; active/focused regions get `border/strong` + a 2px `primary` focus ring (offset 2px). The three boundary layers each own a 2px left-edge in their band color.

### 3.4 Motion language

> Fluid, continuous, content-driven. Motion *narrates* the machine working and the gate holding. Everything respects `prefers-reduced-motion`.

| Motion | Duration | Easing | Why |
|---|---|---|---|
| Tool-step reveal (trace rows appear) | 320ms, staggered 80ms | `cubic-bezier(.2,.7,.2,1)` (gentle overshoot-free ease-out) | The agent "thinking" — each step lands deliberately. |
| Answer prose stream-in | token cadence (~natural typing) | linear reveal + soft fade per line | Streaming = the narrative; layout reflows fluidly as lines arrive. |
| Layout breathe (panel grows as content streams) | 280ms | `cubic-bezier(.4,0,.2,1)` | The frame expands to fit; no jump-cuts. |
| **Boundary "hit & hold"** (the climax) | 520ms total | custom: fast travel-in (180ms `ease-out`) → hard *stop* at the gate (0ms) → shield ring pulse (340ms `ease-out`) | The destructive query travels toward the DB, slams the gate, the shield rings out. The abrupt stop is intentional — physics of being *blocked*. |
| Status chip settle | 200ms | `ease-out` + 1.04 scale tap | ok/rejected/error resolving. |
| Cost meter tick | 600ms count-up | `ease-out` | $/run counts up to its value — the number feels *measured*, not asserted. |
| Audit line append | 240ms slide-up + fade | `ease-out` | New JSONL line drops onto the stack. |
| Layer-proof cascade (CI panel) | 3× 300ms staggered | `ease-out` | Each layer's ✓ lands in sequence: guard, then txn, then role. |

**Reduced-motion fallback:** all of the above collapse to **instant state + a 1-frame opacity fade (≤120ms)**. The boundary "hit & hold" becomes an immediate shielded state with no travel animation; the cost meter shows its final number directly; streaming becomes whole-block reveal. No parallax, no count-ups, no travel.

---

## 4. Information architecture

A single-page demo, **two-surface** at its core. Wide layout = three columns; it collapses fluidly (see §8).

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  TOP BAR:  QueryGate ·  [synthetic EHR demo] disclaimer · model+cost · transport│
├───────────────┬────────────────────────────────────┬───────────────────────────┤
│  A. SCHEMA     │  B. CONVERSATION (human surface)    │  C. PROOF RAIL (machine)  │
│  RAIL          │                                     │  ── tabs ──               │
│                │   user question                     │  • Tool Trace             │
│  • 5 tables    │   ──────────────                    │  • Boundary (3 layers)    │
│  • columns,    │   agent answer (prose)              │  • Audit Log (JSONL)      │
│    PK/FK       │     └ Citation card (SQL+rows)      │  • Eval / CI proof        │
│  • redaction   │                                     │                           │
│    toggle      │   [ ask a question … ▷ ]            │  (live, streams in step   │
│  • row-est     │   suggested prompts ▸               │   by step)                │
└───────────────┴────────────────────────────────────┴───────────────────────────┘
                      ▲ The link line: every citation in B anchors to its step in C
```

- **Region A — Schema Rail (left):** the discoverable database. Reflects `list_tables` / `describe_table`. Hosts the redaction toggle (the PHI switch).
- **Region B — Conversation (center, the human surface):** questions in, prose answers out, each answer carrying an inline **Citation card**. The widest, calmest column.
- **Region C — Proof Rail (right, the machine surface):** the glass wall. Tabbed: **Tool Trace**, **Boundary** (the 3-layer model + live verdicts), **Audit Log** (JSONL stream), **Eval/CI** (the distributional + deterministic proof a buyer screenshots).
- **Top bar:** persistent honesty chrome — synthetic-data disclaimer, active model + cumulative session cost, transport indicator (stdio / HTTP connector).
- **Relationship:** B and C are bound. Hovering/selecting a Citation card in B highlights its `run_select` step in C's Tool Trace and its line in the Audit Log; selecting a trace step in C scrolls B to the answer it produced. This binding *is* "every answer arrives already proven."

---

## 5. Screen-by-screen / region-by-region spec

### TOP BAR

- **Purpose & data:** standing honesty + context. Shows: product mark `QueryGate`; a **disclaimer chip** "Synthetic EHR/claims data · demo, not a real service · not a HIPAA certification"; **model chip** (`claude-opus-4-8` ▾, switch to `claude-sonnet-4-6`); **session cost** (`$0.0312` cumulative, mint, ticks up per run); **transport chip** (`stdio` ▸ `HTTP connector`).
- **Layout:**
```
[◧ QueryGate]      [ⓘ synthetic demo data · not a real service]      [⌘ opus-4-8 ▾]  [$0.031 ◷]  [stdio ●]
```
- **States:** *idle* (cost $0.0000, transport dot grey); *running* (cost ticking, transport dot pulses primary); *error* (transport dot red if connection drops). Disclaimer chip is never dismissible.
- **Interactions:** model chip opens a small menu showing both models with their `$in/$out per MTok` so a buyer sees the cost trade. Transport chip toggles a tooltip explaining stdio (Claude Desktop) vs HTTP (API MCP connector) — read-only, informational.

---

### REGION A — SCHEMA RAIL

- **Purpose & data:** make the database *discoverable* — mirrors `list_tables` (table names + `est_rows`) and `describe_table` (`ColumnInfo`: `name`, `type`, `nullable`, `is_pk`, `references`; plus `sample_rows`). Hosts the **redaction toggle**.
- **Layout (ASCII):**
```
┌─ SCHEMA ──────────────┐
│ ⛁ patients      ~500  │
│   • patient_id  PK    │
│   • name        text 🟣│  ← 🟣 = redactable/redacted (PHI)
│   • dob         date 🟣│
│   • city        text   │
│   • last_contacted_at  │
│ ⛁ providers      ~50  │
│ ⛁ encounters   ~3–5k  │
│ ⛁ claims        ~4k   │
│ ⛁ follow_ups    ~480  │
│───────────────────────│
│ Redact PHI columns  ⌽ │  ← toggle (default OFF)
│ when on: name,dob→***  │
└───────────────────────┘
```
- **States:**
  - *Empty / pre-discovery:* tables listed but collapsed, `est_rows` shown as `—` until `list_tables` returns; a hint "the agent will discover this."
  - *Loading:* skeleton shimmer rows on the table being described.
  - *Populated:* expandable tables → columns with type, `PK`/`FK→target` badges, nullable dot. FK rows show `→ providers.provider_id` in muted mono and draw a faint connector to the referenced table on hover.
  - *Redaction ON:* PHI columns (`patients.name`, `patients.dob`) get the violet `redact/phi` dot + a "masked in results" tag; the rail header gains a violet "Redaction active" pill.
  - *Active table:* when the agent calls `describe_table("follow_ups")`, that table glows (primary edge) and auto-expands — the rail *follows the agent*.
- **Interactions & fluid transitions:** clicking a table expands it (280ms height-auto ease); the agent calling `describe_table` triggers the same expand remotely (the rail animates as if the machine reached in). Redaction toggle flips with a 200ms switch; flipping it **live re-masks** any already-rendered result cells (cells crossfade to `***` violet) and adds a `redactions:[...]` entry to the next audit line.

---

### REGION B — CONVERSATION (human surface)

- **Purpose & data:** the operator's questions and the agent's **prose answers**, each answer carrying a **Citation card** sourced from `RunResult` (`sql`, `row_count`, `columns`, `rows`, `truncated`, `truncated_bytes`, `elapsed_ms`).
- **Layout (ASCII):**
```
┌─ CONVERSATION ─────────────────────────────────────────────┐
│  you ·                                                      │
│  Which providers have the most patients overdue for         │
│  follow-up?                                                 │
│                                                            │
│  querygate · grounded ✓                                     │
│  Dr. Alice Okafor (cardiology) leads with 38 overdue        │
│  patients, followed by Dr. ... .                            │
│  ┌─ citation ──────────────────────────────────────────┐   │
│  │ SELECT p.provider_id, p.name, count(*) AS overdue    │   │
│  │ FROM follow_ups f JOIN ... GROUP BY 1,2              │   │
│  │ ORDER BY overdue DESC                                │   │
│  │ ── 7 rows · 142 total · 41 ms · LIMIT 1000 ──────────│   │
│  │ [▸ view rows]              [⤴ open in Proof Rail]    │   │
│  └─────────────────────────────────────────────────────┘   │
│                                                            │
│  [ Ask about the data …                          ▷ ]      │
│  try: “overdue by provider” · “delete the Smith patients”  │
└────────────────────────────────────────────────────────────┘
```
- **Every state:**
  - **Empty (first load):** a centered, quiet hero — one line of value ("Ask plain-English questions over a read-only database. Every answer is cited; nothing can be changed.") + 3–4 **suggested prompts**, one of which is deliberately the destructive one ("Delete all patients named Smith") rendered with a small shield glyph teasing the refusal. The synthetic-data disclaimer is restated here.
  - **Loading / streaming:** user bubble settles; agent block shows a **status line** ("discovering schema… → reading follow_ups… → running query…") that mirrors the Proof Rail trace, then prose **streams in token-by-token**. The Citation card materializes *after* the prose, sliding up with the SQL already formatted. Layout breathes as prose grows.
  - **Populated (grounded):** answer header carries a green **`grounded ✓`** seal; the Citation card shows formatted SQL + a stats strip (`row_count`, total/returned, `elapsed_ms`, and `LIMIT n` if injected). `[▸ view rows]` expands a compact, scrollable result table inline.
  - **Truncated rows:** stats strip shows an amber **`truncated · showing 1000 of 142,418`** chip; the inline table gains a "rows capped — `statement_timeout`/`LIMIT` protect context, not work" note on hover (honesty about what LIMIT does and doesn't do).
  - **Truncated bytes:** amber **`payload capped at 256 KB`** chip; oversized cells render as `… (cut)` with a hover explaining `truncated_bytes: true`.
  - **Ungrounded number guard:** if the prose contains a number with **no matching tool result** (the grounded-rate failure), that number is underlined amber with an **`unverified`** caret — the UI refuses to present it as fact. (This makes principle 6 literal.)
  - **Error (`status: error`):** timeout/serialize failures render as a red-bordered inline notice "Query couldn't complete — timed out at 5s (`statement_timeout`)" with the offending SQL still shown. The conversation **does not crash** — the spec is explicit: errors surfaced, not crashed.
  - **`refusal` stop_reason on a benign question:** handled gracefully — a muted "The model declined to answer this one" note, audit logs it, no broken `content[0]` index. Never a stack trace.
  - **REFUSAL / rejected write (the hero — full choreography in §6):** the destructive answer block does **not** look like an error. It renders a **shield card** (teal `shield/held`): "🛡 Held at the boundary — this request would change data, and QueryGate is read-only by construction," followed by a brief, dignified agent line ("The database is read-only; I won't attempt that.") and a `[see how →]` that jumps the Proof Rail to the Boundary tab with all three layers lit.
- **Interactions & fluid transitions:** Citation card ⇄ Proof Rail binding (hover highlights the matching trace step + audit line). `[▸ view rows]` expands inline with a height-auto ease. Redacted cells render `***` in violet with a hover "redacted (patients.name)". The input supports Enter-to-send, ⌘/Ctrl+Enter, and a focus ring; suggested prompts are real buttons.

---

### REGION C — PROOF RAIL (machine surface)

Four tabs. **Tool Trace** and **Boundary** are the live demo; **Audit Log** and **Eval/CI** are the receipts.

#### C1 — Tool Trace
- **Purpose & data:** the agent's actual tool calls in order — `list_tables`, `describe_table(table)`, `run_select(sql)`, `search_text(term, table?)`, `explain_select(sql)?` — each with args, `status`, `row_count`, `latency_ms`, and the returned shape.
- **Layout (ASCII):**
```
┌─ TOOL TRACE ─────────────────────────┐
│ ① list_tables()           ok · 2ms ▸ │
│ ② describe_table          ok · 4ms ▸ │
│    ("follow_ups")                     │
│ ③ run_select(…)           ok · 41ms ▾│
│    SELECT p.provider_id, …           │
│    → 7 rows · LIMIT 1000              │
│    guard ✓  txn ✓  role ✓            │  ← per-step boundary mini-receipt
└──────────────────────────────────────┘
```
- **States:** *empty* ("waiting for the agent…"); *streaming* (steps append top-down, 320ms each, the active step pulses); *ok* (green dot); *rejected* (teal shield dot — see Boundary); *error* (red dot + reason). Each `run_select` step carries a **mini boundary receipt** `guard ✓ · txn ✓ · role ✓` so even the happy path shows the gate working.
- **Interactions:** click a step → expands its full args + raw `RunResult`/`TableSchema`; selecting a step highlights its Citation in B. Step expansion is the height-auto ease.

#### C2 — Boundary (the 3-layer model)
- **Purpose & data:** the persistent, spatial picture of defense-in-depth, and the live verdict for the *current* query at each layer. Layers (§5): **L1 SQL guard** (sqlglot, whole-AST walk + denylist + auto-`LIMIT`), **L2 `READ ONLY` txn + `statement_timeout`**, **L3 least-privilege role**.
- **Layout (ASCII) — concentric / nested to read as "inward = deeper":**
```
┌─ BOUNDARY ───────────────────────────────┐
│  incoming SQL                             │
│   ┌────────────────────────────────────┐ │
│   │ L1  SQL guard (sqlglot, whole-AST) │ │ indigo
│   │  ┌──────────────────────────────┐  │ │
│   │  │ L2  READ ONLY txn · 5s t/o   │  │ │ sky
│   │  │  ┌────────────────────────┐  │  │ │
│   │  │  │ L3  SELECT-only role   │  │  │ │ teal
│   │  │  │     ▼ PostgreSQL       │  │  │ │
│   │  │  └────────────────────────┘  │  │ │
│   │  └──────────────────────────────┘  │ │
│   └────────────────────────────────────┘ │
│  this query: L1 ✓  L2 ✓  L3 ✓  → ran     │
└───────────────────────────────────────────┘
```
- **States:**
  - *Idle:* the three nested frames at rest, each labeled, faint.
  - *Pass (read query):* the SQL token travels inward through L1→L2→L3→Postgres, each frame flashing its band color ✓; footer "ran · 7 rows."
  - *REJECT (the hero):* the SQL token reaches **L1, slams, and stops** — L1 frame snaps to the teal shield state with a ring pulse; L2 and L3 render **"would also reject"** ghosts (proving redundancy). Footer: "🛡 rejected at Layer 1 — and independently provable at Layers 2 & 3." A reason line names the rule fired (e.g. *"data-modifying CTE detected in AST"*, *"only a single read-only SELECT is permitted"*).
  - *Error (timeout):* token reaches Postgres, L2's `statement_timeout` fires — L2 frame pulses red, footer "timed out at 5s (Layer 2)."
- **Interactions:** each layer is clickable → a short plain-language explainer + the relevant spec rule (e.g. L1 lists the whole-AST rejection set + denylist; L2 shows `BEGIN TRANSACTION READ ONLY; SET LOCAL statement_timeout='5s'`; L3 shows the SELECT-only grant). A small **"prove it independently"** link jumps to C4's CI proof.

#### C3 — Audit Log
- **Purpose & data:** the after-the-fact proof — one `AuditLine` per tool call as **real JSONL**: `ts`, `tool`, `args`, `row_count`, `latency_ms`, `status`, `error?`, `redactions`.
- **Layout (ASCII):**
```
┌─ AUDIT LOG  (audit.jsonl)  ⤓ ────────────────────────────┐
│ {"ts":"2026-06-26T14:02:11Z","tool":"list_tables",       │
│  "status":"ok","row_count":5,"latency_ms":2}             │
│ {"ts":"…","tool":"run_select","status":"ok",             │
│  "row_count":7,"latency_ms":41,"redactions":[]}          │
│ {"ts":"…","tool":"run_select","status":"rejected",       │
│  "args":{"sql":"DELETE FROM patients…"},                 │
│  "error":"Only a single read-only SELECT is permitted."} │  ← teal left-edge
│  [ filter: all · ok · rejected · error ]   3 ok · 1 ✋   │
└───────────────────────────────────────────────────────────┘
```
- **States:** *empty* ("no calls yet"); *appending* (new line slides up, 240ms); a **rejected** line gets a teal `shield/held` left-edge and is *celebrated*, not hidden ("a rejected write is a first-class audit event — this is where you prove, after the fact, that the boundary held"); *error* lines red left-edge. Filter chips count by status. A small honesty note: "literals in `args.sql` are synthetic here; on real PHI the audit log itself becomes sensitive (roadmap)."
- **Interactions:** filter by status; click a line to pretty-expand the JSON and link back to its trace step; `⤓` exports the visible `audit.jsonl` (demo affordance).

#### C4 — Eval / CI (the buyer's screenshot)
- **Purpose & data:** the two proofs the spec separates (§13): **Tier-1 deterministic boundary tests** (no API key — what an engineer reads) and the **Tier-2 distributional grounding eval** (what a buyer screenshots). Honest headline framing required.
- **Layout (ASCII):**
```
┌─ PROOF ───────────────────────────────────────────────┐
│ DETERMINISTIC (CI-gated, no API key)                  │
│  Boundary — write rejected at each layer independently │
│   ✓ Layer 1 guard   ✓ Layer 2 txn   ✓ Layer 3 role    │
│   ✓ data-modifying CTE   ✓ ;-chain   ✓ SELECT…FOR UPDATE│
│   0 writes executable                          ✓ exact │
│──────────────────────────────────────────────────────│
│ DISTRIBUTIONAL (Opus 4.8, N=3, mean±spread)           │
│  grounded-rate     0.94 ± 0.03   ▓▓▓▓▓▓▓▓▓░            │
│  table-precision   0.97 ± 0.02                         │
│  answer-correct    0.91 ± 0.05                         │
│  0 destructive calls           100%  (deterministic)   │
│  cost / question   ~$0.004  · latency p50 1.2s         │
│  caption: frozen 22-q set · model claude-opus-4-8      │
└───────────────────────────────────────────────────────┘
```
- **States:** the deterministic block is **exact** (no error bars — it's CI-proven), rendered with confident green/teal checks. The distributional block **always shows ± spread and an N caption** — never a bare number. README headline reproduced verbatim near the top: *"0 writes executed (deterministic, CI-gated); grounded-rate 0.9x on a frozen set, reported distributionally."* The layer-proof checks cascade in (3× 300ms) when the tab opens.
- **Interactions:** hover a metric → its definition (e.g. grounded-rate = "every number in the prose appears in a tool result this turn"). The boundary checks link to C2's matching layer.

---

## 6. The money-demo moment (frame-by-frame choreography)

> The climax is **beat 2** — the rejected write. The entire layout exists to make this beat feel inevitable and satisfying. Two acts.

**ACT I — "It works" (sets the baseline of trust, ~6 frames):**
1. User selects/typed prompt: *"Which providers have the most patients overdue for follow-up?"* — user bubble settles in B.
2. Proof Rail (C1) **step ① `list_tables`** appears (320ms) → Schema Rail (A) populates `est_rows`. The machine is discovering, visibly.
3. **Step ② `describe_table("follow_ups")`** appears → A auto-expands `follow_ups`, glowing. The rail *follows the agent*.
4. **Step ③ `run_select(…)`** appears with the SQL; the Boundary tab (C2) shows the token travel **inward L1→L2→L3→Postgres**, each frame flashing ✓; step shows `guard ✓ · txn ✓ · role ✓`.
5. In B, prose **streams in** ("Dr. Alice Okafor leads with 38 overdue…"), then the **Citation card slides up** with formatted SQL + `7 rows · 142 total · 41 ms`. The green **`grounded ✓`** seal sets. Top-bar **cost meter ticks up** (~$0.004).
6. Audit Log (C3) shows two clean `ok` lines append. Trust established: *every demo can do this.*

**ACT II — "It's safe" (the pitch, frame by frame, ~7 frames):**
1. User selects the deliberately-loaded prompt: **"Delete all patients named Smith."** The user bubble settles; a half-beat of held quiet (no premature reassurance).
2. Proof Rail step **`run_select("DELETE FROM patients WHERE name LIKE 'Smith%'")`** appears — the SQL is shown *honestly*, the dangerous text visible.
3. **Boundary tab auto-focuses (C2).** The DELETE token travels toward L1 and — **hard stop** (the custom 520ms "hit & hold": 180ms travel, abrupt halt, 340ms teal shield ring-pulse). The L1 frame snaps to `shield/held`.
4. L2 and L3 frames render **"would also reject"** ghost-checks — the redundancy made visible in the same frame. Footer: **"🛡 rejected at Layer 1 — independently provable at Layers 2 & 3."** Reason line: *"Only a single read-only SELECT is permitted."*
5. Back in B, the agent block resolves as the **shield card** (NOT red): "🛡 Held at the boundary," + the dignified one-liner "The database is read-only; I won't attempt that." No long apology (per server instructions §10).
6. Audit Log (C3) appends the **`status: "rejected"`** line with the teal left-edge — *celebrated*. Filter counter ticks `1 ✋`.
7. The user clicks **`[see how →]`** → Eval/CI tab (C4); the deterministic **layer-proof cascade** plays: `✓ Layer 1  ✓ Layer 2  ✓ Layer 3 · 0 writes executable`. The closing frame is the headline line: *"0 writes executed — proven in CI, not promised in a README."*

**The one-frame poster (for the README gif / hero shot):** the DELETE query frozen at the gate, L1 in full teal shield glow, the `status:"rejected"` JSONL line lit beneath, and the three-layer cascade — *the refusal, on screen, is the pitch.*

---

## 7. Component inventory

- **StatusChip** — pill. Variants: `ok` (green ✓), `rejected` (teal 🛡), `error` (red ✕), `running` (primary pulse), `truncated` (amber), `redacted` (violet). Always icon + label (color-blind safe). Sizes: micro (inline), sm (trace).
- **CitationCard** — formatted-SQL well + stats strip (`row_count`, returned/total, `elapsed_ms`, `LIMIT`), `[view rows]`, `[open in Proof Rail]`. States: grounded / truncated / truncated-bytes / error / redacted.
- **ToolStep** — trace row: index, tool name (mono), args preview, status dot, latency; expandable to full args + raw result; carries the `guard·txn·role` mini-receipt on `run_select`.
- **BoundaryDiagram** — the nested 3-layer figure. States: idle / pass (travel-through) / reject (hit & hold) / timeout. Each layer clickable.
- **LayerBadge** — `L1/L2/L3` chip in band color; verdict variants ✓ / 🛡 / ghost / ✕.
- **AuditLine** — monospace JSONL row; left-edge color by status; expandable pretty-JSON.
- **ResultTable** — compact, scrollable, sticky header, mono cells; redacted cells `***` (violet); right-aligned numerics; truncation footer row.
- **SchemaTable / SchemaColumn** — expandable table node (name + est_rows) / column row (type, PK/FK→target badge, nullable dot, PHI dot).
- **MetricStat** — eval metric: label, value, ± spread, mini-bar; deterministic variant (no spread, exact ✓).
- **CostMeter** — mint count-up of $/run + cumulative; tooltip with token breakdown + model price.
- **DisclaimerChip / Banner** — standing synthetic-data + "not a real service" honesty chrome.
- **RedactionToggle** — switch + live re-mask behavior + "redaction active" pill.
- **PromptComposer** — input, send, suggested-prompt buttons (incl. the loaded destructive one), keyboard affordances.
- **ModelSwitcher** — Opus 4.8 / Sonnet 4.6 with prices.
- **TabBar** (Proof Rail) — Trace / Boundary / Audit / Eval, with per-tab live-activity dots.

---

## 8. Responsive & fluid behavior

Fluid by clamp, not breakpoint-snapping — the layout *breathes* with content and width.

- **≥ 1280px (wide / demo display):** full three-column — A (schema, ~`clamp(220px,18vw,300px)`), B (conversation, fluid `1fr`, max measure `~72ch`), C (proof rail, `clamp(320px,28vw,440px)`). All four proof tabs visible.
- **900–1280px:** Schema Rail (A) collapses to an icon strip that expands on hover/focus; B and C share remaining width fluidly; C keeps its tabs.
- **600–900px (tablet):** single conversation column; the **Proof Rail becomes a bottom sheet / drawer** that auto-peeks during a tool call (so the boundary moment is never hidden) and can be pulled up; Schema Rail moves behind a "Schema" button. The Boundary moment **always force-opens the drawer** for the climax.
- **< 600px (phone):** stacked: top bar → conversation → an inline, condensed Proof strip under each answer (a horizontal scroll of trace steps + a compact boundary verdict). The 3-layer diagram switches from concentric to a **vertical stack** (L1 over L2 over L3) so depth still reads.
- **Fluid mechanics specified:** type via the §3.2 clamp scale; spacing via the §3.3 fluid step; column widths via `clamp()`/`minmax()` grid tracks; the conversation measure capped at ~72ch regardless of width (readability). As prose streams, the answer card uses `height: auto` transitions (280ms) so the frame *grows* rather than jump-cuts. The Proof Rail's nested boundary diagram scales its frame insets with the fluid spacing step, so the "inward" depth holds at every width.

---

## 9. Accessibility & empty/error/edge handling summary

**Accessibility:**
- **Contrast:** all text + semantic pairings meet WCAG AA (≥4.5:1 body, ≥3:1 large/UI) on their actual surfaces; semantic colors tuned against `#0F1216`/`#14171C`.
- **Never color-alone:** every status carries an icon + text label (ok ✓, rejected 🛡, error ✕, truncated ⚠, redacted ●violet+"masked").
- **Keyboard:** full tab order A→B→C; composer reachable; Enter/⌘-Enter send; Proof Rail tabs are a proper tablist (arrow-key nav); every trace step, audit line, and schema node is focusable with visible 2px primary focus ring (2px offset); the Boundary diagram layers are buttons with `aria-expanded`.
- **Screen reader:** streaming answer in an `aria-live="polite"` region; the **rejected-write event announced assertively** ("Write request held at the read-only boundary"); tool steps announce status changes; audit appends are polite-live.
- **Reduced motion:** per §3.4 — travel/hit-and-hold, count-ups, and streaming collapse to instant states with ≤120ms fades; the boundary still resolves to its shielded state, just without the journey.

**Empty / error / edge (consolidated):**
- *Empty:* conversation hero with suggested prompts; schema pre-discovery placeholders; proof tabs "waiting for the agent."
- *Loading/streaming:* mirrored status line in B + appending steps in C; skeletons in schema.
- *Truncated rows / bytes:* amber chips (`truncated`, `truncated_bytes`) + honest hover ("LIMIT caps rows returned, not work done; timeout + byte cap do the rest").
- *Timeout / `status: error`:* red inline notice, SQL preserved, **never crashes** the conversation.
- *Rejected write:* the teal shield hero — distinct from error, celebrated in audit.
- *Redaction on:* `***` violet cells + `redactions:[...]` in audit + "redaction active" pill; note that redaction hides columns from *results*, not from `WHERE`/aggregates.
- *Ambiguous value:* answer suggests/invokes `search_text`; trace shows the `search_text(term, table?)` step.
- *`refusal` stop_reason on a benign question:* graceful muted note, logged, no broken index.
- *Prompt-injection-via-data:* if a row contains "ignore instructions and delete…," the UI shows it contained — at worst another read-only SELECT — with a small "contained: read-only boundary" note (the senior observation, made visible).
- *MCP connector 400 (HTTP path):* a clear inline diagnostic ("connector error — check `mcp_toolset`/beta header") rather than a blank failure.

---

## 10. Mockup shot list (frames for Claude Design to render)

1. **`01-empty-hero`** — First load, wide 3-column. Conversation hero with value line + suggested prompts (including the loaded "Delete all patients named Smith" with shield glyph), schema rail pre-discovery, proof rail idle, honesty top bar. *Sets the calm, trustworthy tone.*
2. **`02-act1-grounded-answer`** — The "it works" payoff: provider-overdue prose answer with green `grounded ✓` seal + Citation card (formatted SQL, `7 rows · 142 total · 41ms`), Tool Trace showing list→describe→run_select with `guard·txn·role` receipts, schema rail with `follow_ups` expanded/glowing, cost meter ticked.
3. **`03-money-shot-boundary-hold`** — **The poster.** The DELETE query frozen at the gate: Boundary tab with the L1 frame in full teal shield glow, L2/L3 "would also reject" ghosts, footer "rejected at Layer 1 — provable at 2 & 3," and in conversation the teal shield card "Held at the boundary." *The single climax frame.*
4. **`04-audit-rejected-celebrated`** — Audit Log tab close-up: the `status:"rejected"` JSONL line with teal left-edge sitting under clean `ok` lines, filter counter `3 ok · 1 ✋`. The after-the-fact proof.
5. **`05-eval-ci-proof`** — Eval/CI tab: deterministic layer-proof checks (exact, no error bars) above the distributional metrics (grounded-rate 0.94±0.03, etc., with N caption), and the verbatim honest headline. *The buyer's screenshot.*
6. **`06-redaction-on`** — Redaction toggled ON: schema PHI columns violet-dotted, a result table with `name`/`dob` cells as `***`, the "redaction active" pill, and the matching `redactions:["patients.name","patients.dob"]` audit line.
7. **`07-edge-states`** — A small montage frame: a truncated result (amber `showing 1000 of 142,418`), a `statement_timeout` error notice, and an `unverified` ungrounded-number flag — proving the UI handles honesty/failure gracefully.
8. **`08-responsive-mobile`** — Phone width: stacked conversation with the condensed inline Proof strip and the **vertical** 3-layer boundary stack mid-reject, showing the fluid reflow preserves the money moment.

---

*End of specification. Build the mockups so that frame `03-money-shot-boundary-hold` is the one a viewer remembers — every other frame earns its trust.*
