# Project 15 — Agent Orchestration System with Tool Use, Memory, and Human-in-the-Loop
## Implementation Plan

> **Source:** This plan implements **Project 15** from the *BASWE 15 AI Engineering Projects* guide (`BASWE__15_AI_Engineering_Projects_Guide.docx.md`, "Project 15" section). It follows the guide's tech stack, its six build phases, and its day-by-day scope. Anything not in the original guide is explicitly tagged **[ADDITION]** (new but necessary), **[CHOICE]** (the guide offered options and one is selected), or **[SCOPE NOTE]** (guide feature staged or bounded to stay realistic). A consolidated deviations table is in §10.

---

## 1. Project Goal and Expected Outcome

**Goal (from the guide):** Build a multi-agent orchestration platform where a **Supervisor Agent** decomposes complex tasks and delegates subtasks to specialized tool-using **Specialist Agents**, a **Reviewer Agent** validates outputs, the system maintains **persistent memory** across interactions (short-term working memory + long-term semantic memory), and it **escalates to a human operator** when confidence is low or the task requires approval — with **full observability** into every agent decision.

**Expected outcome at the end (Day 14):**

1. `docker compose up` starts the full system: orchestration API, Celery workers, Redis, PostgreSQL, ChromaDB, human review UI, and trace explorer UI.
2. A demo script runs a showcase research task end-to-end, visibly exercising: task decomposition, parallel specialist execution with real tool calls, a reviewer rejection → rework loop, a memory-informed planning decision, and a human approval of a sensitive step.
3. Every execution produces a browsable trace tree with per-agent decisions, tool calls, latency, token usage, and dollar cost.
4. Any past execution can be replayed step-by-step and forked with modified inputs.
5. A README with an architecture diagram and an under-5-minute demo recording, framed with the guide's narrative: *"production infrastructure for autonomous AI workflows, not an AI demo."*

---

## 2. Scope: MVP and Full V1

### 2.1 MVP (target: end of Phase 2, ~Day 7)

A vertical slice proving the core loop works before HITL and observability are layered on:

- **Agents:** Supervisor + 2 specialists (**Research**, **Writing**) + Reviewer.
- **Planning:** task decomposition into a dependency-ordered subtask list via structured output (Pydantic-validated), with a confidence score.
- **Tools:** registry with 3 tools — web search, file read/write (workspace-scoped), sandboxed code execution. Permissions enforced per specialist; every invocation logged.
- **Graph:** LangGraph state machine `intake → plan → execute (parallel/sequential) → review → synthesize → deliver`, with conditional edges for specialist retry and reviewer rejection (max 2 rework cycles).
- **Memory:** Redis working memory scoped to a task and cleared on completion; ChromaDB long-term memory with basic save-after-completion and retrieve-before-planning.
- **HITL (minimal):** one escalation path — low plan confidence pauses the run; approve/reject via API endpoint (UI comes in Phase 3).
- **Persistence:** tasks, plans, subtasks, and tool invocations in PostgreSQL.
- **Execution:** inline (in-process) run mode; docker-compose for infra services (Postgres, Redis, ChromaDB) only.
- **Models:** OpenAI for supervisor/specialists, Anthropic for the reviewer (multi-model routing from day one).

### 2.2 Full V1 (target: Day 14)

Everything in the guide's Project 15 spec:

- **Agents:** 4 specialists (add **Data Analysis** and **Code Execution**), each with domain-specific tools.
- **Tools:** all 5 from the guide (web search, file read/write, sandboxed code execution, database query, API calls) + rate limits per tool. MCP adapter as stretch (see §10).
- **Memory:** importance scoring, consolidation of similar memories, expiration/decay, a memory dashboard, and a user-data delete endpoint.
- **HITL:** all 5 escalation triggers, 4 granular approval levels (Notify / Approve action / Approve plan / Take over), approval queue with notifications, and a review UI with context, reasoning, relevant memories, action buttons, and a clarifying-question chat panel.
- **Observability:** OpenTelemetry-based execution tracing, trace explorer UI (tree view, color-coded, expandable prompts/responses), cost & performance tracking with aggregates, and the replay system.
- **Async execution:** Celery workers consuming from Redis.
- **Integration:** full docker-compose (7 services), scripted demo scenario, end-to-end test suite.
- **Polish:** README, architecture diagram, <5-min demo recording.

### 2.3 Explicitly out of scope

Authentication/multi-tenancy, horizontal scaling, cloud deployment, model fine-tuning, streaming token-by-token UI, and any features from other projects in the guide.

---

## 3. System Architecture and Data Flow

### 3.1 Component diagram

```
                        ┌────────────────────────────────────────────────┐
                        │                 Human Operator                 │
                        │   Review UI (Streamlit)   Trace Explorer (UI)  │
                        └──────────┬─────────────────────────┬───────────┘
                                   │ approve/modify/reject/   │ browse traces,
                                   │ take-over + chat         │ costs, replay
┌──────────┐   POST /tasks   ┌─────┴──────────────────────────┴───────────┐
│  Client  ├────────────────►│           Orchestration API (FastAPI)      │
└──────────┘                 └─────┬───────────────────────────────▲──────┘
                                   │ enqueue                       │ status/results
                             ┌─────▼──────────────────────────────┴──────┐
                             │        Celery Worker (LangGraph run)      │
                             │  ┌──────────────────────────────────────┐ │
                             │  │            LangGraph Graph           │ │
                             │  │  intake → plan → execute → review    │ │
                             │  │      ↑        │        │      │      │ │
                             │  │      └─rework─┘   escalate  synthesize│ │
                             │  │                       │        │     │ │
                             │  │  Supervisor  Specialists(4)  Reviewer│ │
                             │  └───────┬───────────┬──────────────────┘ │
                             └──────────┼───────────┼────────────────────┘
                                        │           │ tool calls
                              LLM Router│     ┌─────▼─────────┐
                          (OpenAI +     │     │ Tool Registry │
                           Anthropic)   │     │ search·files· │
                                        │     │ code·db·api   │
                                        │     └───────────────┘
      ┌───────────┐   ┌────────────┐   ┌──────────────┐
      │   Redis   │   │ PostgreSQL │   │   ChromaDB   │
      │ working   │   │ tasks·plans│   │  long-term   │
      │ memory +  │   │ approvals· │   │  semantic    │
      │ celery    │   │ traces·    │   │  memory      │
      │ broker    │   │ checkpoints│   │  (vectors)   │
      └───────────┘   └────────────┘   └──────────────┘
```

### 3.2 Agent hierarchy (guide Phase 1.1)

| Layer | Agent | Responsibility | Default model |
| --- | --- | --- | --- |
| 1 | **Supervisor** | Receives task, retrieves memories, creates execution plan, delegates, synthesizes final output | Strong model (e.g. `gpt-4o` or `claude-sonnet`) |
| 2 | **Research Specialist** | Web research, source gathering | Cheaper model (e.g. `gpt-4o-mini`) |
| 2 | **Data Analysis Specialist** | Data extraction, computation via code exec + DB query | Cheaper model |
| 2 | **Writing Specialist** | Drafts, summaries, memos | Cheaper model |
| 2 | **Code Execution Specialist** | Writes & runs code in the sandbox | Cheaper model |
| 3 | **Reviewer** | Validates specialist outputs (1–5 quality score + feedback) before they return to the supervisor | **Different provider** than the producing agent (Anthropic if producer used OpenAI) — this implements the guide's "multi-model agent routing" |

Each agent is a LangGraph node with typed (Pydantic) input/output schemas.

### 3.3 Data flow (happy path + branches)

1. **Intake** — client `POST /tasks` → task row in Postgres → run dispatched (Celery task, or inline in dev mode).
2. **Memory retrieval** — supervisor queries ChromaDB for similar past tasks, successful/failed approaches, user preferences, domain facts; retrieved memories are injected into the planning prompt (guide Phase 2.3).
3. **Planning** — supervisor emits a structured `ExecutionPlan`: subtasks with description, assigned specialist, required inputs, dependencies, expected output format, estimated complexity, plus overall confidence (guide Phase 1.2).
4. **Plan gate** — confidence < threshold, or user requested review → **escalate (Approve plan)**; execution pauses at a LangGraph interrupt and the state checkpoints to Postgres.
5. **Execution** — a scheduler node walks the subtask DAG; independent subtasks fan out in parallel. Each specialist reads working memory (Redis), calls its permitted tools through the registry (logged: inputs, outputs, latency, success/failure), writes results back to working memory.
6. **Review** — reviewer scores each deliverable. Reject → route back to the specialist with feedback (max 2 rework cycles). Score below threshold after rework, or 2 failures on the same subtask → **escalate**.
7. **Sensitive ops** — tools tagged sensitive (external side effects, deletions, external communications) require **Approve action** before invocation.
8. **Synthesis & delivery** — supervisor composes the final deliverable; task marked complete.
9. **Memory write-back** — an extraction step embeds what was asked, what approach worked, tools used, domain facts discovered, and observed user preferences into ChromaDB; Redis working memory for the task is cleared (guide Phase 2.1–2.2).
10. **Throughout** — every agent decision, tool call, memory retrieval, and escalation emits an OpenTelemetry span with custom attributes (agent, model, tokens, cost, status), exported to Postgres for the trace explorer.

### 3.4 Storage responsibilities

| Store | Holds |
| --- | --- |
| **Redis** | Task-scoped working memory (plan, completed-subtask outputs, intermediate results, error logs); Celery broker/result backend; tool rate-limit counters |
| **PostgreSQL** | Tasks, plans, subtasks, tool invocations, approvals, trace spans, cost records, LangGraph checkpoints, memory audit log, demo dataset for the DB-query tool |
| **ChromaDB** | Long-term semantic memory (embedded episodic summaries, domain facts, user preferences) with importance/recency metadata |

---

## 4. Repository Structure

```
agent-orchestration-system/
├── README.md
├── PROJECT_IMPLEMENTATION_PLAN.md      # this file
├── docker-compose.yml
├── docker/
│   ├── api.Dockerfile
│   ├── worker.Dockerfile
│   ├── ui.Dockerfile
│   └── sandbox.Dockerfile              # minimal image for code-execution tool
├── .env.example
├── pyproject.toml
├── Makefile
├── src/orchestrator/
│   ├── config.py                       # pydantic-settings; all thresholds & flags
│   ├── main.py                         # FastAPI app factory
│   ├── api/routes/                     # tasks.py, approvals.py, memory.py, traces.py, replay.py
│   ├── llm/                            # router.py (agent→provider/model map), clients.py, pricing.py, mock.py
│   ├── agents/
│   │   ├── base.py                     # BaseAgent: prompt assembly, LLM call, span emission
│   │   ├── supervisor.py
│   │   ├── reviewer.py
│   │   └── specialists/                # research.py, analysis.py, writing.py, code.py
│   ├── planning/                       # schemas.py (ExecutionPlan, Subtask), decomposer.py
│   ├── graph/                          # state.py, builder.py, edges.py, checkpointing.py
│   ├── tools/
│   │   ├── base.py                     # ToolSpec: name, description, I/O schemas, owners, rate limit, sensitive flag
│   │   ├── registry.py                 # registration, permission checks, invocation logging
│   │   ├── web_search.py, file_io.py, code_exec.py, db_query.py, api_call.py
│   │   └── mcp_adapter.py              # stretch (see §10)
│   ├── memory/                         # working.py (Redis), longterm.py (ChromaDB), retrieval.py, extraction.py, management.py
│   ├── hitl/                           # triggers.py, levels.py, queue.py, notify.py
│   ├── observability/                  # tracing.py (OTel setup + Postgres exporter), cost.py, replay.py
│   ├── workers/                        # celery_app.py, run_task.py, beat_jobs.py (memory consolidation/expiration)
│   └── db/                             # models.py (SQLAlchemy), session.py, migrations/ (Alembic), seed_demo_data.py
├── ui/
│   ├── review_app.py                   # Streamlit: approval queue, decision detail, chat panel, memory dashboard
│   └── trace_explorer.py               # Streamlit: trace tree, cost dashboards, replay controls
├── scripts/
│   ├── run_demo.py                     # scripted showcase scenario
│   └── record_fixtures.py              # capture live LLM responses as test fixtures
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── e2e/
│   └── fixtures/llm/                   # recorded responses for MOCK_LLM mode
└── docs/
    └── architecture.md                 # diagram source + design decisions
```

---

## 5. Phase-by-Phase Implementation

Day ranges follow the guide (12–14 days at 2–3 h/day).

---

### Phase 0 — Project Scaffolding **[ADDITION]** (½ day, before Day 1)

*The guide starts directly at agent architecture; a real repo needs bootstrap. Nothing here adds product scope.*

**Tasks**
1. Initialize repo, `pyproject.toml` (Python 3.11+; deps: `langgraph`, `langchain-openai`, `langchain-anthropic`, `fastapi`, `uvicorn`, `celery`, `redis`, `sqlalchemy`, `alembic`, `psycopg`, `chromadb`, `pydantic-settings`, `opentelemetry-sdk`, `streamlit`, `pytest`, `httpx`).
2. `config.py` with all env-driven settings (API keys, DB URLs, `RUN_MODE=inline|celery`, `MOCK_LLM`, confidence/review thresholds).
3. `docker-compose.yml` with infra services only: `postgres`, `redis`, `chromadb`.
4. FastAPI skeleton with `/health`; Alembic initialized; Makefile targets (`make dev`, `make test`, `make infra`).
5. `MOCK_LLM` fixture-playback client stub **[ADDITION — enables deterministic tests without API spend]**.

**Files:** `pyproject.toml`, `docker-compose.yml`, `.env.example`, `Makefile`, `src/orchestrator/{config.py,main.py}`, `src/orchestrator/llm/mock.py`, `src/orchestrator/db/{models.py,session.py}`, `tests/conftest.py`.

**Acceptance criteria**
- [ ] `docker compose up postgres redis chromadb` → all healthy.
- [ ] `make dev` serves `/health` → 200; `make test` runs a trivial passing test.
- [ ] Alembic migration creates an empty schema baseline.

---

### Phase 1 — Agent Architecture (Day 1–4) · guide Phase 1

**Tasks** (map 1:1 to guide items 1.1–1.4)

1. **Agent hierarchy (1.1):** `BaseAgent` with typed I/O; Supervisor, 4 Specialists, Reviewer as LangGraph nodes. LLM router maps each agent to provider+model (§3.2); reviewer routed to a different provider than the producer.
2. **Task decomposition engine (1.2):** `ExecutionPlan` / `Subtask` Pydantic schemas — description, assigned specialist, required inputs, expected output format, estimated complexity, `depends_on`, plan-level confidence. Supervisor uses structured output; invalid plans rejected and retried once with the validation error in the prompt; DAG validated acyclic.
3. **Tool registry (1.3):** `ToolSpec` (name, description, input/output schemas, owning specialists, rate limit, `sensitive` flag) + registry enforcing permissions and rate limits. Implement all 5 tools:
   - `web_search` — Tavily API, DuckDuckGo fallback **[CHOICE — guide names no provider]**; mocked in tests.
   - `file_read` / `file_write` — scoped to a per-task workspace directory.
   - `code_exec` — sandboxed: subprocess inside a dedicated minimal Docker container, no network, CPU/mem/wall-time limits **[CHOICE — guide says "sandboxed" without a mechanism]**.
   - `db_query` — read-only SQL against a seeded `demo` schema in Postgres **[CHOICE — guide names no target DB; seed data added so the tool is demonstrable]**.
   - `api_call` — HTTP GET/POST against an allowlist; POST tagged `sensitive`.
   Every invocation logged to `tool_invocations` (inputs, outputs, latency, success/failure).
4. **LangGraph state machine (1.4):** graph `intake → plan → execute → review → synthesize → deliver` with a scheduler node that dispatches DAG-ready subtasks (parallel fan-out via LangGraph `Send`). Conditional edges: specialist failure → retry with revised approach (max 2, then mark for escalation); reviewer reject → back to specialist with feedback; low confidence → escalation node (stub in this phase, real HITL in Phase 3). Postgres checkpointer wired **[CHOICE — `langgraph-checkpoint-postgres`; standard mechanism for the pause/resume the guide requires in Phase 3]**.
5. API: `POST /tasks`, `GET /tasks/{id}` (status, plan, subtask states, output). Inline run mode executes the graph in-process; Celery run mode added here as a thin wrapper (`workers/run_task.py`) but only exercised fully in Phase 5 **[SCOPE NOTE]**.

**Files:** `src/orchestrator/agents/**`, `planning/**`, `tools/**`, `graph/**`, `llm/**`, `api/routes/tasks.py`, `db/models.py` (+migration: tasks, plans, subtasks, tool_invocations), `docker/sandbox.Dockerfile`, `tests/unit/{test_plan_schema.py,test_registry.py,test_graph_routing.py,test_tools.py}`, `tests/integration/test_task_happy_path.py`.

**Acceptance criteria**
- [ ] `POST /tasks` with a multi-part request yields a valid `ExecutionPlan`: ≥2 subtasks, valid specialists, acyclic dependencies, confidence ∈ [0,1].
- [ ] Each specialist completes a subtask using ≥1 of its tools; a specialist calling a non-owned tool is rejected and the rejection is logged.
- [ ] Rate limit on a tool triggers backoff/failure after N calls (unit-tested with a limit of 2).
- [ ] Happy-path task runs intake→delivery with MOCK_LLM; two independent subtasks execute in parallel (assert overlapping span times or scheduler batching).
- [ ] Reviewer rejection routes back with feedback and succeeds on attempt 2; two consecutive specialist failures set `needs_escalation` on the state.
- [ ] All Phase 1 tests pass with `MOCK_LLM=1`; one live smoke test passes with real keys.

---

### Phase 2 — Memory System (Day 4–7) · guide Phase 2

**Tasks** (map to guide items 2.1–2.4)

1. **Working memory (2.1):** Redis hash/namespace per task id holding current plan, completed subtask outputs, intermediate results, error log. All agents read/write through `memory/working.py`. Cleared on task completion (and on delete endpoint).
2. **Long-term semantic memory (2.2):** post-completion extraction step (LLM call) producing typed memory records — task summary + what was asked, approach that worked, tools used, domain facts discovered, user preferences observed. Embedded and stored in ChromaDB collections (`episodes`, `facts`, `preferences`) with metadata: user_id, task_type, created_at, access_count, importance.
3. **Retrieval for planning (2.3):** before planning, query ChromaDB for similar past tasks & their plans, approaches that worked/failed, user preferences, relevant facts; inject top-k into the planning prompt in a labeled block; record which memory ids were retrieved (feeds tracing in Phase 4).
4. **Memory management (2.4):**
   - Importance scoring: bump on access; score = f(access_count, recency).
   - Consolidation: job that merges near-duplicate memories (cosine similarity above threshold) into higher-level summaries.
   - Expiration: decay stale, low-importance memories past a cutoff.
   - Runs as Celery beat jobs + manually triggerable endpoints **[CHOICE — guide names no scheduler]**.
   - Memory dashboard: what the system "remembers" per user (served as a page in the review UI, built in Phase 3; API endpoint built now) + `DELETE /memory/users/{user_id}` purging ChromaDB entries, working memory, and audit rows.

**Files:** `src/orchestrator/memory/**`, `api/routes/memory.py`, `workers/beat_jobs.py`, `db/models.py` (+migration: memory audit log), `tests/unit/{test_working_memory.py,test_importance.py,test_consolidation.py}`, `tests/integration/{test_memory_roundtrip.py,test_retrieval_informs_planning.py}`.

**Acceptance criteria**
- [ ] During execution, working memory contains the plan and grows with each completed subtask; key vanishes after completion.
- [ ] A completed task produces ≥1 memory record per applicable category in ChromaDB.
- [ ] Run task A, then similar task A′: retrieval returns ≥1 memory from A and the planning prompt provably contains it (assert on prompt assembly); retrieved ids recorded.
- [ ] Accessing a memory raises its importance; consolidation merges two seeded near-duplicates into one summary; expiration removes a seeded stale memory.
- [ ] `DELETE /memory/users/{id}` leaves zero ChromaDB entries and zero Redis keys for that user; dashboard endpoint returns grouped memories.

---

### Phase 3 — Human-in-the-Loop (Day 7–10) · guide Phase 3

**Tasks** (map to guide items 3.1–3.4)

1. **Escalation triggers (3.1):** implement all five as pure, testable predicates in `hitl/triggers.py`:
   1. plan confidence < `PLAN_CONFIDENCE_THRESHOLD` (default 0.7)
   2. specialist failed twice on the same subtask
   3. sensitive operation about to run (tool `sensitive` flag: financial ops, data deletion, external communications → in this system: `api_call` POST, `file` delete, `db` writes)
   4. reviewer quality score < `REVIEW_SCORE_THRESHOLD` (default 3/5) after rework
   5. user set `require_human_review=true` on task submission
2. **Approval queue (3.2):** on trigger — pause via LangGraph interrupt (state checkpointed to Postgres), write an `approvals` row packaging full context (original task, plan, completed steps, current step, proposed action + agent reasoning), notify via log + optional Slack-compatible webhook **[CHOICE — guide says "notify" without a channel]**. Execution resumes only on resolution.
3. **Approval levels (3.3):** `NOTIFY`, `APPROVE_ACTION`, `APPROVE_PLAN`, `TAKE_OVER`, with default trigger→level mapping (low confidence → APPROVE_PLAN; sensitive op → APPROVE_ACTION; double failure → APPROVE_ACTION with take-over offered; low review score → APPROVE_ACTION; user-requested → APPROVE_PLAN + final-deliverable approval). NOTIFY records and proceeds without pausing. Mapping configurable in `config.py`.
4. **Review interface (3.4):** Streamlit app **[CHOICE — guide offers "React or Streamlit"; Streamlit keeps the 14-day scope realistic]** showing: queue; per-item task context & execution progress; the decision point; proposed action with reasoning; relevant memories & similar past decisions (queried from ChromaDB/approvals history); buttons **Approve / Modify / Reject / Take over** (modify edits the proposed action JSON; take over supplies the output directly and agents stand down); and a **chat panel** where the human asks the agent clarifying questions answered from checkpointed state + working memory before deciding.
5. Resolution API: `GET /approvals`, `GET /approvals/{id}`, `POST /approvals/{id}/resolve` (action, payload, notes) — resumes the graph from the checkpoint with the human decision merged into state; records human review time.

**Files:** `src/orchestrator/hitl/**`, `graph/edges.py` (escalation wiring), `api/routes/approvals.py`, `ui/review_app.py`, `db/models.py` (+migration: approvals), `tests/unit/test_triggers.py`, `tests/integration/{test_pause_resume.py,test_approval_levels.py}`.

**Acceptance criteria**
- [ ] Each of the 5 triggers fires under a simulated condition and maps to its configured level (parameterized test).
- [ ] Escalation pauses the run mid-graph; the approvals row contains the full context package; no agent work happens while pending.
- [ ] Approve resumes exactly where paused; Reject terminates the task with a recorded reason; Modify executes the human-edited action; Take over uses the human output and skips the specialist; NOTIFY never pauses.
- [ ] Review UI lists pending items, renders context/reasoning/memories, resolves via all four buttons, and the chat panel returns grounded answers about the paused task.
- [ ] Human review duration is recorded per approval.

---

### Phase 4 — Observability and Debugging (Day 10–12) · guide Phase 4

**Tasks** (map to guide items 4.1–4.4)

1. **Execution tracing (4.1):** OpenTelemetry spans with custom attributes around every supervisor planning decision, specialist reasoning step + tool call, reviewer evaluation, memory retrieval (with retrieved ids and their influence), and escalation event/resolution. Attributes: agent, model, prompt/response refs, tokens, cost, status. Export via a **custom SpanExporter into Postgres** **[CHOICE/deviation — the guide says "use OpenTelemetry spans" but names no backend; Postgres avoids running Jaeger and gives the trace explorer a queryable store]**. Full prompts/responses stored in a companion table referenced by span id.
2. **Trace explorer UI (4.2):** Streamlit app rendering each task's span tree — per node: agent, decision, tools called, latency, cost, errors; color-coded success/warning/failure/escalated; click-to-expand full LLM prompt & response.
3. **Cost & performance tracking (4.3):** per task — total tokens by agent and model (from provider usage + static price table in `llm/pricing.py`), tool call counts, wall-clock time, human review time, total $. Aggregates across tasks: cost per task type, most expensive agents, tool usage patterns, escalation-rate trend. Served as API endpoints + a dashboard page in the trace explorer.
4. **Replay system (4.4):** every LLM call and tool call already records full inputs/outputs (Phase 1/4 tables). Replay mode: load a past execution, step through each agent decision substituting recorded outputs (deterministic, zero API cost); **modify any input at step k → execution resumes live from that point as a forked run**; compare fork vs original. **[SCOPE NOTE — comparison is a side-by-side step table with divergence highlighting, not a graphical graph-diff; fully satisfies the guide's debugging intent within scope.]**

**Files:** `src/orchestrator/observability/**`, `llm/pricing.py`, `api/routes/{traces.py,replay.py}`, `ui/trace_explorer.py`, `agents/base.py` + `tools/registry.py` + `memory/retrieval.py` (span emission hooks), `db/models.py` (+migration: spans, llm_calls, costs), `tests/unit/{test_cost_calc.py,test_span_export.py}`, `tests/integration/{test_trace_tree.py,test_replay_fork.py}`.

**Acceptance criteria**
- [ ] A completed demo task yields one trace tree containing spans for: planning, every subtask, every tool call, every reviewer evaluation, every memory retrieval, and any escalation — with parent/child links intact.
- [ ] Trace explorer renders the tree, color-codes statuses, and expands a node to the exact prompt and response.
- [ ] Cost endpoint returns per-task tokens by agent/model and a dollar total that matches a hand-computed fixture; aggregate endpoints return the four rollups from the guide.
- [ ] Replaying a past task with no modifications reproduces the original outputs step-for-step without any LLM API call; modifying step k's input produces a fork whose steps < k match the original and steps ≥ k may diverge, shown in the comparison view.

---

### Phase 5 — Integration and End-to-End Testing (Day 12–13) · guide Phase 5

**Tasks** (map to guide items 5.1–5.3)

1. **Demo scenario (5.1):** *"Research the top 3 open-source vector databases, extract and compare their GitHub statistics, analyze the trade-offs, and produce a one-page recommendation memo."* Exercises: decomposition into research/analysis/writing subtasks; research specialists fanning out in parallel (one per candidate DB); data analysis via code exec; the reviewer rejecting the first memo draft (quality bar on citations, enforced by review criteria); memory from a prior seeded run informing planning; final memo delivery tagged as user-requested review → human approves in the UI. `scripts/run_demo.py` drives it and prints progress + links to the two UIs.
2. **Containerize the full system (5.2):** docker-compose with all 7 services from the guide — `api`, `worker` (Celery, `RUN_MODE=celery` now fully exercised), `redis`, `postgres`, `chromadb`, `trace-explorer`, `review-ui` — plus the sandbox image built for `code_exec`. Healthchecks, dependency ordering, one-command startup, `.env`-driven config, and the demo script runnable against the composed stack.
3. **End-to-end tests (5.3):** the guide's six behaviors (see §7), running under `MOCK_LLM=1` with recorded fixtures for CI determinism, plus an optional live smoke marker.

**Files:** `docker-compose.yml` (full), `docker/*.Dockerfile`, `scripts/run_demo.py`, `scripts/record_fixtures.py`, `tests/e2e/**`, `Makefile` (`make demo`, `make e2e`).

**Acceptance criteria**
- [ ] `docker compose up` from a clean checkout (with `.env`) → all 7 services healthy; migrations and demo-data seed run automatically.
- [ ] `make demo` completes the showcase scenario end-to-end against the composed stack, pausing once for human approval in the review UI, and finishes with the memo + a browsable trace.
- [ ] `make e2e` passes all six guide-mandated e2e tests deterministically with no API keys required.
- [ ] Live smoke test (`pytest -m live`) passes with real keys.

---

### Phase 6 — Polish for Portfolio (Day 13–14) · guide Phase 6

**Tasks** (map to guide items 6.1–6.2)

1. **Record the demo (6.1):** under 5 minutes, showing the full lifecycle — complex request in, supervisor planning (trace view), specialists executing with tool calls, reviewer rejection + rework, human approving the sensitive/final step in the review UI, memory saving lessons learned, and the trace explorer walking every decision with costs.
2. **Write the narrative (6.2):** README leading with the architecture diagram and the guide's framing: *"I built a multi-agent orchestration system where AI agents decompose complex tasks, use tools to execute them, learn from past interactions via persistent memory, and escalate to humans when confidence is low. It's not an AI demo — it's production infrastructure for autonomous AI workflows."*

**Files:** `README.md`, `docs/architecture.md`, demo recording (link or `docs/demo.gif`).

**Acceptance criteria:** see §8 (README & demo requirements) — all items checked.

---

## 6. Setup, Configuration, Docker, and Local Execution

### 6.1 Prerequisites
Docker + docker-compose, Python 3.11+, `uv` or `pip`, an OpenAI API key and an Anthropic API key (Tavily key optional — web search falls back to DuckDuckGo).

### 6.2 Configuration (`.env.example`)

```
OPENAI_API_KEY=            ANTHROPIC_API_KEY=          TAVILY_API_KEY=        # optional
DATABASE_URL=postgresql+psycopg://orchestrator:orchestrator@postgres:5432/orchestrator
REDIS_URL=redis://redis:6379/0
CHROMA_HOST=chromadb       CHROMA_PORT=8000
RUN_MODE=celery            # inline | celery
MOCK_LLM=0                 # 1 = play recorded fixtures, no API calls
PLAN_CONFIDENCE_THRESHOLD=0.7
REVIEW_SCORE_THRESHOLD=3
MAX_SPECIALIST_RETRIES=2
APPROVAL_WEBHOOK_URL=      # optional Slack-compatible notification
```

### 6.3 Services and ports

| Service | Image/Build | Port | Role |
| --- | --- | --- | --- |
| `api` | `docker/api.Dockerfile` | 8080 | FastAPI orchestration API **[ADDITION — guide references "the orchestration API" without naming a framework]** |
| `worker` | `docker/worker.Dockerfile` | — | Celery worker + beat (memory jobs) |
| `redis` | `redis:7` | 6379 | Working memory + broker |
| `postgres` | `postgres:16` | 5432 | Persistent state |
| `chromadb` | `chromadb/chroma` | 8000 | Long-term memory |
| `review-ui` | `docker/ui.Dockerfile` | 8501 | Human review + memory dashboard |
| `trace-explorer` | `docker/ui.Dockerfile` | 8502 | Traces, costs, replay |

### 6.4 Local execution

```
cp .env.example .env       # add keys
make infra                 # docker compose up postgres redis chromadb (dev mode)
make dev                   # uvicorn with RUN_MODE=inline — fast iteration, no worker needed
make test                  # unit+integration, MOCK_LLM=1
docker compose up --build  # full 7-service system
make demo                  # scripted showcase scenario against the composed stack
```

---

## 7. End-to-End Testing

**Determinism strategy [ADDITION]:** `MOCK_LLM=1` swaps the LLM client for a fixture player (`tests/fixtures/llm/`); fixtures are captured once from live runs via `scripts/record_fixtures.py`. Tool calls with external effects (web search, api_call) are mocked at the tool boundary. This lets the full e2e suite run in CI with zero keys and zero cost; a separate `-m live` marker covers real-API smoke tests.

**The six e2e scenarios (guide Phase 5.3, 1:1):**

| # | Test | Asserts |
| --- | --- | --- |
| 1 | Task decomposition produces valid plans | Schema-valid, acyclic, known specialists, confidence present — across ≥3 differently-shaped requests |
| 2 | Specialists correctly use their tools | `tool_invocations` rows exist per specialist, only owned tools used, I/O logged |
| 3 | Reviewer catches deliberately bad outputs | Inject a corrupted specialist output → reviewer rejects with feedback → rework loop runs |
| 4 | Memory improves planning on repeated similar tasks | Second similar task retrieves ≥1 memory; planning prompt contains it; retrieval recorded in trace |
| 5 | Escalation triggers at the right moments | Each of the 5 triggers under simulation → correct approval level, pause, resume on approve |
| 6 | Graceful recovery from agent failures | Forced specialist exception → retry → alternate approach → escalate on 2nd failure; task never left in inconsistent state |

Plus one full-lifecycle test: demo scenario start-to-finish with a programmatic approval, asserting final output, memory write-back, cleared working memory, complete trace tree, and non-zero computed cost.

---

## 8. README and Demo Requirements

**README must contain:**
- Hero architecture diagram (agent hierarchy + decision flow — the guide says to lead with this).
- What/why paragraph using the guide's narrative framing (§ Phase 6.2).
- Feature list mapped to the four pillars: multi-agent orchestration, tool use, memory, HITL — plus observability.
- Quickstart: clone → `.env` → `docker compose up` → `make demo` (≤4 commands).
- Demo GIF or a link to the <5-min recording.
- Screenshots: review UI at a decision point; trace explorer tree; memory dashboard.
- Design decisions section (why LangGraph, why Redis+ChromaDB split, why reviewer uses a different provider, escalation-level mapping).
- Cost expectations for a demo run; limitations & next steps.

**Demo recording (<5 min) must show, in order:** complex request submitted → supervisor's plan (with confidence) → specialists executing in parallel with live tool calls → reviewer rejecting and specialist reworking → escalation appearing in the review UI → human inspecting reasoning/memories, chatting a clarifying question, approving → final deliverable → memory dashboard showing the new lessons learned → trace explorer walking the full decision tree with cost.

---

## 9. Phase-by-Phase Claude Code Execution Prompts

Run one prompt per session, in order. Each assumes the repo root as CWD and this file present.

**Prompt 0 — Scaffolding**
```
Read PROJECT_IMPLEMENTATION_PLAN.md (§4, §5 Phase 0, §6). Implement Phase 0 exactly: pyproject with the listed dependencies, config.py via pydantic-settings with every flag from §6.2, docker-compose with postgres/redis/chromadb only, FastAPI /health, Alembic baseline, Makefile targets (infra/dev/test), and the MOCK_LLM fixture-player client stub. No agent logic yet. Done when all Phase 0 acceptance criteria pass; show me `make test` and `docker compose up` output.
```

**Prompt 1 — Agent architecture**
```
Read PROJECT_IMPLEMENTATION_PLAN.md §3 and §5 Phase 1. Implement the agent hierarchy (supervisor, 4 specialists, reviewer with cross-provider routing), the ExecutionPlan/Subtask schemas with structured-output decomposition and DAG validation, the tool registry with all 5 tools (permissions, rate limits, sensitive flags, invocation logging), and the LangGraph state machine with parallel subtask dispatch, retry/rework conditional edges, an escalation stub, and the Postgres checkpointer. Expose POST /tasks and GET /tasks/{id} with inline run mode. Write the unit/integration tests listed for Phase 1 and make them pass under MOCK_LLM=1. Do not start memory, HITL, or observability work. Finish by running the full test suite and one live smoke test if keys are present.
```

**Prompt 2 — Memory system**
```
Read PROJECT_IMPLEMENTATION_PLAN.md §5 Phase 2. Implement Redis working memory (task-scoped, cleared on completion), ChromaDB long-term memory with post-completion extraction (episodes/facts/preferences), retrieval injected into the planning prompt with retrieved ids recorded, and memory management: importance scoring, consolidation, expiration (Celery beat jobs + manual endpoints), the memory dashboard endpoint, and DELETE /memory/users/{id}. Write the Phase 2 tests and make them pass under MOCK_LLM=1. Verify the retrieval-informs-planning integration test asserts the prompt actually contains a retrieved memory.
```

**Prompt 3 — Human-in-the-loop**
```
Read PROJECT_IMPLEMENTATION_PLAN.md §5 Phase 3. Implement all 5 escalation triggers as pure predicates, the approval queue using LangGraph interrupts + the Postgres checkpointer (full-context packaging, webhook/log notification), the 4 approval levels with the default trigger→level mapping from the plan, the approvals API (list/detail/resolve with resume), and the Streamlit review UI with queue, decision detail, proposed action + reasoning, relevant memories, Approve/Modify/Reject/Take-over buttons, and the clarifying-question chat panel grounded in checkpointed state. Record human review time. Write the Phase 3 tests and make them pass; demonstrate pause→approve→resume end-to-end.
```

**Prompt 4 — Observability**
```
Read PROJECT_IMPLEMENTATION_PLAN.md §5 Phase 4. Instrument everything with OpenTelemetry spans (planning, specialist steps, tool calls, reviewer evals, memory retrievals, escalations) exported to Postgres via a custom SpanExporter, with full prompts/responses stored by span id. Build the Streamlit trace explorer (color-coded tree, click-to-expand prompt/response), cost tracking per task and the four aggregate rollups, and the replay system: deterministic step-through from recorded calls, modify-at-step-k live fork, and side-by-side comparison. Write the Phase 4 tests and make them pass, including replay-reproduces-original with zero API calls.
```

**Prompt 5 — Integration & E2E**
```
Read PROJECT_IMPLEMENTATION_PLAN.md §5 Phase 5, §6, §7. Finish the full docker-compose (api, celery worker with RUN_MODE=celery, redis, postgres, chromadb, review-ui, trace-explorer) with healthchecks and auto-migration/seed. Implement scripts/run_demo.py for the vector-database research scenario from §5 Phase 5 and scripts/record_fixtures.py. Write the six e2e tests from §7 plus the full-lifecycle test, all passing under MOCK_LLM=1 via `make e2e`, and verify `make demo` completes against the composed stack with one human approval. Show me the compose startup, e2e run, and demo output.
```

**Prompt 6 — Polish**
```
Read PROJECT_IMPLEMENTATION_PLAN.md §8 and §11. Write the README with the architecture diagram (mermaid or embedded image), the narrative framing from the plan, quickstart, feature map, screenshots placeholders, design decisions, cost notes, and limitations. Write docs/architecture.md. Then walk §11's final completion checklist against the actual repo, fix anything unchecked, and give me the checklist with every item's pass/fail status plus a suggested shot list for the <5-minute demo recording.
```

---

## 10. Deviations and Additions from the Original Guide

Everything else in this plan follows the guide's Project 15 section directly. The complete list of divergences:

| # | Item | Tag | Rationale |
| --- | --- | --- | --- |
| 1 | Phase 0 scaffolding phase | **ADDITION** | Guide starts at agent design; repo bootstrap is a prerequisite, adds no product scope |
| 2 | FastAPI as the orchestration API framework | **ADDITION** | Guide's Phase 5 requires "the orchestration API" but names no framework |
| 3 | Streamlit for both UIs | **CHOICE** | Guide offers "React or Streamlit"; Streamlit keeps 14-day solo scope realistic |
| 4 | LangGraph interrupts + Postgres checkpointer for HITL pause/resume | **CHOICE** | Guide requires pause/resume; this is LangGraph's standard mechanism |
| 5 | OTel spans exported to Postgres via custom exporter (no Jaeger/collector service) | **DEVIATION (simplification)** | Guide mandates OTel spans but no backend; Postgres gives the trace explorer a queryable store without extra infra |
| 6 | Tavily (DuckDuckGo fallback) as web-search provider | **CHOICE** | Guide names no provider; mocked in tests |
| 7 | Code-exec sandbox = no-network Docker container with CPU/mem/time limits | **CHOICE** | Guide says "sandboxed" without a mechanism |
| 8 | Seeded `demo` schema in Postgres as the database-query tool target | **CHOICE** | Guide names no target DB; makes the tool demonstrable |
| 9 | Slack-compatible webhook + log for reviewer notification | **CHOICE** | Guide says "notifies the human reviewer" without a channel |
| 10 | MCP adapter staged as stretch after the custom registry | **SCOPE NOTE** | Guide stack says "Custom + MCP"; custom registry delivers all 5 required tools, MCP adapter added if time allows |
| 11 | `MOCK_LLM` recorded-fixture mode | **ADDITION** | Deterministic, key-free, zero-cost test runs; guide's e2e tests are impractical in CI without it |
| 12 | `RUN_MODE=inline\|celery` flag | **ADDITION** | Celery (per guide) is the composed-system mode; inline mode enables fast dev/test iteration |
| 13 | Replay comparison = side-by-side step table with divergence highlighting | **SCOPE NOTE** | Bounds guide item 4.4's "compare replayed vs original" to a realistic UI |

---

## 11. Final Completion Checklist

**Core system**
- [ ] Supervisor decomposes tasks into schema-valid, dependency-ordered plans with confidence scores
- [ ] All 4 specialists execute subtasks using only their permitted tools; parallel execution works
- [ ] Reviewer validates every specialist output; rejection → rework loop (max 2) works
- [ ] All 5 tools implemented with schemas, permissions, rate limits, sensitive flags, and full invocation logging
- [ ] LangGraph conditional edges: retry, rework-with-feedback, escalation

**Memory**
- [ ] Redis working memory: task-scoped, shared by all agents, cleared on completion
- [ ] ChromaDB long-term memory: extraction after completion; retrieval injected into planning
- [ ] Importance scoring, consolidation, expiration all demonstrable
- [ ] Memory dashboard + user-data delete endpoint work

**Human-in-the-loop**
- [ ] All 5 escalation triggers fire correctly and map to the 4 approval levels
- [ ] Pause/resume via checkpoints; full context packaged into the approval queue; notification sent
- [ ] Review UI: context, reasoning, memories, Approve/Modify/Reject/Take-over, chat panel
- [ ] Human review time recorded

**Observability**
- [ ] Full trace tree per execution (planning, tool calls, reviews, memory retrievals, escalations)
- [ ] Trace explorer: color-coded tree, expandable prompts/responses
- [ ] Cost tracking per task + the four aggregate rollups
- [ ] Replay: deterministic step-through, modify-and-fork, original-vs-fork comparison

**Integration & delivery**
- [ ] `docker compose up` → 7 healthy services from clean checkout
- [ ] `make demo` runs the showcase scenario end-to-end incl. one human approval
- [ ] All six e2e tests + full-lifecycle test pass under `MOCK_LLM=1`; live smoke passes with keys
- [ ] README complete per §8; architecture diagram leads; screenshots included
- [ ] Demo recording under 5 minutes covering the full lifecycle
