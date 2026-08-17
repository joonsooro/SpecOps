# SpecOps

SpecOps is an AI-assisted specification workshop that turns an ambiguous product request into an evidence-backed, reviewable specification package. It combines a conversational interface with a deterministic workflow foundation so that AI can propose product semantics without silently controlling identity, authority, approval, or release readiness.

The central design rule is:

> AI proposes. Deterministic code validates, records, and decides what may advance.

This repository is the implementation artifact for the SpecOps Capstone project. It contains the web application, API, workflow foundation, persistence model, provider adapters, generated contracts, migrations, and verification suite.

## The problem

AI product-building workflows often move too quickly from an underspecified request to generated code. The model fills gaps, assumptions become invisible, and reviewers cannot reliably trace a technical decision back to its supporting evidence.

SpecOps inserts a governed specification layer before downstream delivery. The workshop captures product intent, identifies ambiguity, proposes requirements and technical decisions, binds claims to registered sources, and requires explicit confirmation before producing a handoff-ready package.

## What the Capstone demonstrates

- A text-authoritative workshop presented as a compact, scrollable Luna chat.
- Evidence-grounded analysis of registered product and technical sources.
- Structured proposals for requirements, decisions, checks, and delivery items.
- Explicit confirmation, edit, rejection, and finish controls.
- Deterministic enforcement of authority, identity, revision, and readiness rules.
- Persistent recovery across browser refreshes and process restarts.
- Contract-first boundaries between probabilistic AI providers and trusted application state.
- A release suite covering schemas, migrations, API behavior, browser behavior, and workflow invariants.

The demonstration reads its registered project sources from the adjacent Spec Engineering workspace; provider credentials and runtime state remain server-side.

## Architecture

```text
Participant typed response or explicit review action
       |
       v
React workshop UI
  compact, scrollable Luna conversation
       |
       | HTTP + WebSocket
       v
FastAPI boundary + conversation projector
       |
       | idempotent CHAT ingress and review commands
       v
Deterministic foundation
       - authority and identity
       - schema and evidence validation
       - confirmation controls
       - revisions and idempotent replay
       - readiness and handoff
       |
       +------ durable leased job ------> Durable Analyzer worker
                                               |
                                               v
                                      Deterministic V4 orchestrator
                                               |
              +--------------------------------+-------------------------------+
              |                                                                |
              | supplied canonical question refs                               | typed, hash-bound request
              v                                                                v
     OpenAI GPT-5.6 Luna                                              OpenAI GPT-5.6 Terra
     Guidance selector                                               Analyzer / evaluator
              |                                                                |
              | selected question ref                                          | semantic candidate
              +--------------------------------+-------------------------------+
                                               |
                                               | validated Foundation commands only
                                               v
                                      Deterministic foundation
                                               |
                                               v
                                         SQLite storage
```

### Frontend

The React and TypeScript frontend in `frontend/` presents the canonical workshop as a compact conversation, keeps Luna and participant messages in a scrollable history, and provides a composer at the bottom. It sends typed responses and explicit review actions to the server and renders server-owned projections. It does not hold provider credentials or own canonical workflow state.

### Application boundary

The FastAPI application in `src/specops_workshop/` exposes the HTTP and WebSocket boundary. `ParticipantTurnIngress` commits an idempotent typed `CHAT` turn against the exact active question, while the conversation projector reconstructs the durable participant view. Committed typed text and explicit user controls are the only authoritative V0 input path.

### Deterministic orchestrator

The V4 orchestrator is **deterministic application logic, not an orchestrator agent or another AI model**. It does not invent requirements, choose product semantics, or autonomously pursue goals. Its rule-bound control flow selects the operation, constructs typed and hash-bound requests from committed state, derives stable command identities, applies revision and idempotency checks, and routes provider candidates through the corresponding Foundation command.

Its job is to coordinate the trusted sequence around probabilistic provider calls: load the current Foundation snapshot, manage the Analyzer context, call the appropriate Terra operation, request bounded Luna Guidance when needed, and ask the Foundation to admit or reject the result. For artifact generation it also triggers a separate quality-evaluation context and records the resulting audit through the Foundation.

“Deterministic” describes this orchestration and admission path—not provider-generated content. Luna and Terra responses remain probabilistic.

### AI adapters

**The SpecOps Guidance model is OpenAI GPT-5.6 Luna, configured at medium reasoning effort.** Luna can select only from canonical question references already supplied by the Foundation. It cannot author evidence, mutate workflow state, or commit a decision.

**The SpecOps Analyzer is OpenAI GPT-5.6 Terra, also configured at medium reasoning effort.** Through the OpenAI Responses API, Terra produces bounded semantic proposals and performs independent artifact-quality evaluations. The orchestrator treats every provider response as untrusted input and submits it to the Foundation; it cannot directly become committed application state.

The repository retains a Gemini live-voice transport, but microphone and voice-authoritative input are outside the current text-authoritative V0 demo path.

### Deterministic foundation

The packages in `src/specops_workflow/` and `src/specops_contracts/` form the trusted state and policy core. Unlike the orchestrator, which coordinates use-case steps, the Foundation owns the canonical workflow rules and decides whether a command or AI-generated candidate is admissible. It enforces command contracts, authority, immutable source bindings, idempotent replay, explicit confirmation, derived readiness, audit history, and typed read models.

### Persistence and contracts

SQLite and Alembic provide local persistence and migrations. JSON Schema, generated OpenAPI artifacts, Pydantic models, and generated TypeScript types keep the provider, server, and browser boundaries aligned.

## Latest development update

The active V0 branch now supports one focused specification-workshop cycle:

- Luna and participant turns appear together in a compact chat, with the active composer fixed below the scrollable history.
- Preparation admits an initial four-question runway from the registered product and technical sources.
- Participant turns are serialized: the participant can keep drafting locally while Terra analyzes the previous committed turn, but Send remains pending until analysis finishes.
- Luna Guidance is selection-only and bounded to exact Foundation-admitted question references.
- A shallow or empty runway produces participant-facing waiting guidance instead of stopping the prototype.
- Analysis that produces no decision proposal surfaces a clarification outcome and preserves the revised next question.
- Finish requires at least one committed typed turn and no unresolved proposal.
- Canonical conversation context and typed-turn audit receipts recover across refresh and server restart.

A manual live V0 demo has completed successfully. The formal credentialed full-cycle evaluation remains postponed, so this README does not claim that the complete production-readiness scorecard has passed.

## Repository map

```text
frontend/                 React/TypeScript workshop interface
migrations/               Alembic database migrations
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
- OpenAI GPT-5.6 Luna for bounded Guidance selection
- OpenAI GPT-5.6 Terra for analysis and artifact-quality evaluation
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

SpecOps is a local Capstone demonstration, not a production SaaS release. The current scope does not claim production authentication, multi-tenant isolation, managed cloud deployment, direct Jira/GitHub execution, or a voice-authoritative workshop. AI-backed behavior also depends on provider availability and valid credentials. The deterministic foundation reduces workflow drift; it cannot make probabilistic model output fully deterministic.

Secrets belong only in a local `.env` file and must never be committed. Runtime databases, generated build output, test reports, and local environment files are excluded through `.gitignore`.
