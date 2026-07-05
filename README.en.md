<div align="center">

<h1><img src="docs/logo.svg" width="44" height="44" align="middle" alt="墨子 Mozi" />&nbsp; 墨子 · Mozi</h1>

**A local-first desktop AI platform** — one gateway to unify multi-model routing, one Vault to accumulate your knowledge, runnable end-to-end with zero API keys and zero network egress.

*本地优先的桌面 AI 平台：一座网关统一多模型路由，一座 Vault 沉淀你的知识，全程零 key、零外呼即可端到端运行。*

[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.12+-3776AB?logo=python&logoColor=white)
![Node](https://img.shields.io/badge/node-20+-339933?logo=node.js&logoColor=white)
![Tests](https://img.shields.io/badge/tests-264%20backend%20%2B%2026%20frontend-brightgreen)

</div>

<div align="center"><a href="README.md">中文</a> · <b>English</b></div>

Mozi·Chat (frontend) + UMA multi-model routing gateway + Vault & Mozi-KG (backend) + SQLite. The core loop: **Chat — Route — Knowledge**. Missing keys fall back to a local mock automatically, so it runs end-to-end with zero egress; add any model key to switch to real streaming. A Tauri/Rust native shell is a packaging item — today the full API contract and data model are implemented with a **Python (FastAPI) backend + React/TS frontend + SQLite**.

---

## Features

- **UMA multi-model routing gateway** — `/v1/chat` OpenAI-compatible SSE; 6 strategies + cost-awareness + privacy tier (sovereign mode picks domestic models only) + task detection + fallback chain.
- **Domestic-first adapter layer** — mock (zero key) + GLM / DeepSeek / Kimi / MiniMax / Volcengine Ark (OpenAI-compatible) + Claude (Anthropic) + GPT. Ark hosts DeepSeek-V4-Pro (1M context) and the Doubao Seed reasoning model.
- **Reasoning channel** — reasoning models (Doubao Seed / DeepSeek) stream their chain-of-thought over a separate `reasoning` event, shown live in the frontend "thinking" panel and never mixed into the answer body.
- **Agentic tool loop** — a bounded observe→act engine: the model picks tools and iterates until a final answer or `max_steps`, with every step passing the sovereign egress gate / allowed-tools sandbox / hard quota cap; models without function-calling fall back to a single shot.
- **Vault & Mozi-KG** — archive→chunk→embed→KG-extract writeback; hybrid retrieval BM25 + dense + RRF; Self-RAG-lite injection gate.
- **Data sovereignty** — `user_id` row-level isolation + a single audited egress gate; two hard capabilities: export (portability) and delete (right to be forgotten).
- **Session management** — full CRUD on `/v1/sessions`: create / list (incl. archived view) / rename / archive toggle / hard delete / fetch history.
- **Metering / audit / telemetry** — model_calls billing, usage_ledger quota, audit_log single egress gate, PostHog-shaped events.

---

## Architecture

```
React + TS (Mozi·Chat)            FastAPI (UMA gateway + Vault + Mozi-KG + Skill)     SQLite
  generative surface / cockpit ─HTTP─▶  /v1/chat (OpenAI-compat SSE + reasoning)  ──▶  18 tables
  · streaming chat + reasoning  proxy   /v1/sessions/* · /v1/vault/* · /v1/kg/* · /v1/skills/*   users/sessions/messages
  · session mgmt (rename/archive/del)   routing engine (6 policies+cost+privacy+fallback)        model_calls/vault_documents
  · model switch · injection toggle     retrieval (BM25+dense+RRF) · KG extract · archive loop    doc_chunks/embeddings/kg_*
  · routing/grounding/KG/telemetry      agentic tool loop (bounded observe→act) · tool bridge     skills/skill_calls/agent_steps
                                        adapters: mock(zero key) / GLM·DeepSeek·Kimi·MiniMax·Ark(Doubao) / Claude·GPT
```

Core loop: `persist → smart context injection (Vault retrieval + cite sources) → UMA routing → stream → archive (Q+A into Vault + KG writeback) → meter & bill`.

---

## Quick Start

Prerequisites: Python 3.12+, Node 20+. **No API key required** (missing keys fall back to a local mock, zero egress).

### One command (Makefile)

```bash
make bootstrap   # install backend + frontend deps
make dev         # run backend(8000) + frontend(5173) together
make test        # backend end-to-end smoke test (48 checks)
```

Open **http://localhost:5173**. `/v1` and `/health` are proxied to the backend by Vite.

### Manual

```bash
# backend (port 8000): boots with sequential schema migration + seed plans/demo user
cd backend && python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m uvicorn mozi_backend.main:app --port 8000     # http://localhost:8000/docs for all endpoints

# frontend (port 5173)
cd frontend && npm install && npm run dev
```

> If the backend isn't on 8000: `VITE_API_TARGET=http://127.0.0.1:<port> npm run dev`.

---

## Connecting Real Models (optional)

Set any key in `backend/.env.local` (never committed) and that model switches from mock to real streaming:

```bash
GLM_API_KEY=...          # GLM-5.2 (domestic)
DEEPSEEK_API_KEY=...     # DeepSeek V4 / V4-Flash (domestic)
KIMI_API_KEY=...         # Kimi K2.7 (domestic, code)
MINIMAX_API_KEY=...      # MiniMax M3 (domestic, long-context/multimodal)
ARK_API_KEY=...          # Volcengine Ark (domestic): DeepSeek-V4-Pro (1M ctx) + Doubao Seed reasoning (multimodal + CoT)
ANTHROPIC_API_KEY=...    # Claude (global)
OPENAI_API_KEY=...       # GPT (global)
```

Run `cp backend/.env.example backend/.env.local`, fill in a key, restart the backend. Keys live only in `.env.local` (gitignored) and are never committed.

---

## Data Sovereignty

Local-first means **your data always belongs to you**. The "right to be forgotten / data portability" hard capabilities both authenticate via `auth.current_user_id`, isolate by `user_id` at row level, and delete inside a `database.transaction` atomic transaction:

| Capability | Endpoint | Behavior |
|---|---|---|
| **Export (portability)** | `GET /v1/export` | Bundles all of a user's data into a downloadable JSON: `sessions / messages / vault_documents / doc_chunks / embeddings / kg_entities / kg_edges / usage` and derived tables. Writes one `export` audit row. |
| **Delete (right to be forgotten)** | `DELETE /v1/account` | Cascade-deletes every row for that `user_id` in one transaction (including the text and vectors inside the `chunks_fts` / `vec_chunks` retrieval virtual tables), leaving a single `delete` audit row as a compliance record; any failed step rolls back the whole thing. |

```bash
curl http://localhost:8000/v1/export -o my-mozi-data.json   # retrieve all your data
curl -X DELETE http://localhost:8000/v1/account             # exercise the right to be forgotten
```

> In multi-user / auth modes (`MOZI_MULTIUSER=1` / `MOZI_REQUIRE_AUTH=1`), export/delete act only on the `user_id` of the requesting identity — no cross-user reach.

---

## Verify

```bash
cd backend && .venv/bin/python smoke_test.py                      # smoke 48/48
cd backend && .venv/bin/python -m unittest discover -s tests      # unit/regression (264 tests, skip 5)
cd backend && .venv/bin/python -m tests.eval.run_eval             # offline eval, no baseline regression
cd frontend && npm test                                           # frontend SSE/fallback tests (26)
cd frontend && npm run build                                      # tsc --noEmit + vite production build
```

Coverage: migration/seed → archive (chunk+embed+KG) → hybrid retrieval → KG subgraph → routing (code/sovereign) → full streaming chat loop (incl. reasoning channel) → metering → session CRUD → Skill discover/load/invoke → adapter tool-calling → agentic tool loop (sandbox/egress/quota guards) → telemetry.

---

## Roadmap

Today's knowledge-graph extraction and vector retrieval are **heuristic / placeholder quality** — enough to close the end-to-end loop and stay runnable with zero key / zero egress, but not yet production-grade:

- **Embedding**: local deterministic placeholder vectors (not real BGE-M3 weights).
- **KG extraction**: heuristic rule-based splitting (not LLM semantic extraction); entity disambiguation and relation typing are approximate.
- **Vector storage**: `embeddings.vector` stored as JSON `float[]`, retrieval is an in-memory brute-force scan (no ANN index).

**Productionization path** (each swappable in place without breaking the existing schema or API contract): `BGE-M3` (real multilingual vector weights, deployable offline, sovereign-friendly) + `sqlite-vec` (local ANN index) + `LLM extraction` (model-driven entity/relation extraction and disambiguation).

**Packaging / further out**: Tauri/Rust native shell with notarized .dmg, Mozi-CRDT realtime collaboration, Stripe billing, more Mozi surfaces (Code / Cowork / Design / Video).

---

## Project Structure

```
mozi/
├── backend/
│   ├── mozi_backend/
│   │   ├── db/         # schema.sql(18 tables) · database(migrations) · dal · seed
│   │   ├── gateway/    # router · models(catalog) · orchestrator · agent_loop(tool loop) · adapters/ · egress · quota · api
│   │   ├── vault/      # embedder · chunking · bm25 · retrieval(RRF) · kg · service(archive loop) · api
│   │   ├── skills/     # loader(SKILL.md) · tools(tool bridge) · api
│   │   ├── telemetry/  # events(PostHog-shaped)
│   │   └── config · schemas · util · main
│   └── smoke_test.py · requirements.txt
├── frontend/
│   ├── src/   # App(session sidebar+reasoning) · Inspector(cockpit) · Seal(inline seal) · api(SSE) · types · styles
│   └── test/  # SSE parse/fallback-retry node --test suite
└── demo/mozi-blueprint-demo.html   # static frontend blueprint mockup
```

---

## License

[Apache-2.0](LICENSE). Font attribution: see [frontend/public/FONTS_NOTICE.md](frontend/public/FONTS_NOTICE.md) (the seal's 「墨」 glyph outline is derived from Chongxi Seal Script, Academia Sinica, CC BY-ND 3.0 TW).

Contribution guide: [CONTRIBUTING.md](CONTRIBUTING.md).
