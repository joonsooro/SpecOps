# SpecOps

SpecOps is an AI-assisted specification workshop that turns an ambiguous product request into an evidence-backed, reviewable specification package. It combines a conversational interface with a deterministic workflow foundation so that AI can propose product semantics without silently controlling identity, authority, approval, or release readiness.

The central design rule is:

> AI proposes. Deterministic code validates, records, and decides what may advance.

This repository is the implementation artifact for the SpecOps Capstone project. It contains the web application, API, workflow foundation, persistence model, provider adapters, generated contracts, migrations, and verification suite.

## The problem

AI product-building workflows often move too quickly from an underspecified request to generated code. The model fills gaps, assumptions become invisible, and reviewers cannot reliably trace a technical decision back to its supporting evidence.

SpecOps inserts a governed specification layer before downstream delivery. The workshop captures product intent, identifies ambiguity, proposes requirements and technical decisions, binds claims to registered sources, and requires explicit confirmation before producing a handoff-ready package.

## What the Capstone demonstrates

- A live voice and text workshop for refining a product request.
- Evidence-grounded analysis of registered product and technical sources.
- Structured proposals for requirements, decisions, checks, and delivery items.
- Explicit confirmation, edit, rejection, and finish controls.
- Deterministic enforcement of authority, identity, revision, and readiness rules.
- Persistent recovery across browser refreshes and process restarts.
- Contract-first boundaries between probabilistic AI providers and trusted application state.
- A release suite covering schemas, migrations, API behavior, browser behavior, and workflow invariants.

The included `resources/` directory contains the source material used by the Capstone demonstration.

## Architecture

```text
PM voice or text
       |
       v
React workshop UI
       |
       | HTTP + WebSocket
       v
FastAPI application boundary
       |
       +--------------------+----------------------+
       |                    |                      |
       v                    v                      v
Gemini 3.1 Flash      GPT-5.6 Terra          V4 orchestrator
Live Voice Agent      Analyzer / evaluator
       |                    |                      |
       +-------- AI proposals and transcripts ----+
                                                    |
                                                    v
                                      Deterministic foundation
                                      - authority and identity
                                      - schema validation
                                      - confirmation controls
                                      - revisions and replay
                                      - readiness and handoff
                                                    |
                                                    v
                                             SQLite storage
```

### Frontend

The React and TypeScript frontend in `frontend/` presents the workshop, captures microphone audio, communicates over HTTP and WebSocket, and renders server-owned projections. It does not hold provider credentials or own canonical workflow state.

### Application boundary

The FastAPI application in `src/specops_workshop/` coordinates browser sessions, voice transport, source registration, AI-provider calls, recovery, and the versioned V4 workshop protocol.

### AI adapters

**The SpecOps Voice Agent is Google Gemini 3.1 Flash Live Preview** (`gemini-3.1-flash-live-preview`), which supplies the real-time conversational voice channel. **The SpecOps Analyzer is OpenAI GPT-5.6 Terra, configured at medium reasoning effort.** Through the OpenAI Responses API, Terra produces bounded semantic proposals and performs independent artifact-quality evaluations. Provider output is treated as untrusted input: it must satisfy strict schemas and workflow rules before it can affect committed state.

### Deterministic foundation

The packages in `src/specops_workflow/` and `src/specops_contracts/` form the trusted core. They enforce command contracts, authority, immutable source bindings, idempotent replay, explicit confirmation, derived readiness, audit history, and typed read models.

### Persistence and contracts

SQLite and Alembic provide local persistence and migrations. JSON Schema, generated OpenAPI artifacts, Pydantic models, and generated TypeScript types keep the provider, server, and browser boundaries aligned.

## Repository map

```text
frontend/                 React/TypeScript workshop interface
migrations/               Alembic database migrations
resources/                Capstone demonstration sources
scripts/                  Contract generation and release verification
src/specops_contracts/    Versioned schemas and protocol contracts
src/specops_workflow/     Deterministic policy and state foundation
src/specops_workshop/     FastAPI runtime and AI-provider orchestration
tests/                    Foundation, workshop, contract, and release tests
```

## Technology

- Python 3.12
- FastAPI and Uvicorn
- Pydantic and JSON Schema
- SQLAlchemy, Alembic, and SQLite
- React, TypeScript, and Vite
- Google Gemini 3.1 Flash Live Preview for the Voice Agent
- OpenAI GPT-5.6 Terra for the Analyzer and artifact-quality evaluator
- Pytest, Vitest, and Playwright

## Local foundation setup

Create a Python 3.12 environment and install the deterministic foundation with its test dependencies:

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e '.[test]'
```

Run the anonymous workflow example:

```bash
.venv/bin/python examples/anonymous_workflow.py
```

The example uses generated identities and a temporary SQLite database. It demonstrates both an authorized command and an authority rejection without calling an external AI provider.

## Verification

Run the deterministic test suite from the repository root:

```bash
.venv/bin/pytest -q
```

The release suite exercises the workflow contracts, schema snapshots, authority boundaries, SQLite migrations, append-only audit protections, recovery behavior, generated API contracts, and browser integration. Live provider verification is intentionally separate because it requires external credentials and network access.

## Scope and limitations

SpecOps is a local Capstone demonstration, not a production SaaS release. The current scope does not claim production authentication, multi-tenant isolation, managed cloud deployment, or direct Jira/GitHub execution. AI-backed behavior also depends on provider availability and valid credentials. The deterministic foundation reduces workflow drift; it cannot make probabilistic model output fully deterministic.

Secrets belong only in a local `.env` file and must never be committed. Runtime databases, generated build output, test reports, and local environment files are excluded through `.gitignore`.
