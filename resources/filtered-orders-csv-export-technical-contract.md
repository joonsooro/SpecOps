# Filtered Orders CSV Export - Draft Technical Contract

**Derived from:** Business Specification: Filtered Orders CSV Export and `filtered-orders-csv-export-technical-spec.md`<br>
**Status:** Draft; not implementation-ready until listed decisions close<br>
**Contract version:** `orders-export-contract.v0`<br>
**CSV schema:** `orders-csv.v1`

## 1. Customer promise

An authorized Operations Manager can request one complete CSV of all orders matching the filters captured at that moment, independent of table pagination, and receive an unambiguous success, no-data, expiry, permission, limit, or failure result.

## 2. Negative contract

The system never silently caps rows, never substitutes current filters for captured filters, never exposes a partial file as successful, never lets table-view permission stand in for export permission, never makes a reusable public download link, never uses binary floating-point to format money, and never allows CSV text to execute as a spreadsheet formula.

## 3. Always / Ask First / Never

### Always

| ID | Rule | Source |
|---|---|---|
| A-01 | Use one canonical server-side filter/query definition for table count and export membership. | FR-1, AC-2/3 |
| A-02 | Capture immutable filters, timezone, schema version, requester, authorization scope, and `snapshot_at` when the request is accepted. | UX-4, AC-4 |
| A-03 | Export every matching row, independent of page size and current page. | FR-2, AC-1 |
| A-04 | Generate exactly the six `orders-csv.v1` columns in the contracted order and format. | FR-3, AC-5/8/9/10 |
| A-05 | Treat more than 10,000 rows as an async-mode trigger, not a cap. | FR-5, AC-7 |
| A-06 | Require live `orders.export`, requester ownership, tenant match, and unchanged access scope before download. | FR-6, AC-6 |
| A-07 | Stream into a private temporary object, verify row count and checksum, then atomically publish. | NFR, AC-11 |
| A-08 | Record request, outcome, count, filters, user, timing, and download/expiry events in the audit system. | NFR, AC-12 |
| A-09 | Fail closed on source-data integrity, formatting, authorization, snapshot, or verification errors. | FR-8, AC-11 |
| A-10 | Keep export resource consumption isolated and bounded so browsing remains responsive. | NFR |

### Ask first

| ID | Rule | Required approver |
|---|---|---|
| AF-01 | Before changing the six columns, order, encoding, line ending, date format, money format, or sanitization rule, approve a new schema version and migration/communication plan. | PM + downstream consumers |
| AF-02 | Before adding a business hard maximum, provide capacity evidence, the exact count shown to users, and narrowing guidance. | PM + Engineering |
| AF-03 | Before adding email, external sharing, or an export-history page, update product scope, privacy review, and retention design. | PM + Security |
| AF-04 | Before allowing a user other than the requester to download, define delegation, audit, and revocation semantics. | PM + IAM/Security |
| AF-05 | Before weakening snapshot semantics, state the user-visible reconciliation consequence and replace the acceptance tests. | PM + Finance + Engineering |

### Never

| ID | Rule | Source |
|---|---|---|
| N-01 | Never include pagination, current page, or client result count in export membership. | FR-1/2 |
| N-02 | Never trust client-provided tenant, authorization scope, row count, timezone, or file name. | FR-1/6/7 |
| N-03 | Never ignore an invalid filter, unknown status, or unsupported schema version. | FR-1/3/8 |
| N-04 | Never publish a file whose generated row count differs from the snapshot count. | FR-5, AC-11 |
| N-05 | Never persist or display a reusable public object-storage URL. | FR-6, NFR |
| N-06 | Never log CSV rows, customer names, file content, secrets, or download credentials. | NFR security |
| N-07 | Never recalculate order totals, convert currency, round during export, or format through binary floating point. | Business rules, AC-9 |
| N-08 | Never return an empty or partial CSV as a successful complete export. | FR-4/8 |
| N-09 | Never reuse a temporary object across worker attempts or make it downloadable. | AC-11 |
| N-10 | Never change the content of an existing export when filters or source orders later change. | UX-4, AC-4 |

## 4. Interface contracts

### C-01: Canonical filter

- Input: date range, canonical status values, and customer selector/search as defined by the table.
- Server context: requester, tenant, row-level scope, and organization timezone.
- Output: immutable normalized filter plus `filters_hash`.
- Invariant: table count and export count use the same predicate for the same filter and snapshot.
- Excluded: pagination and display sort.

### C-02: Export creation

- Endpoint: `POST /api/v1/order-exports`.
- Auth: `orders.export` plus existing order visibility checks.
- Idempotency: required per user action.
- Success: `201 completed` or `202 queued` using the same export resource.
- No data: `422 NO_MATCHING_ORDERS`, no file.
- Large: queued, never truncated.
- Captures: filter, schema, timezone, snapshot, requester, tenant, and authorization-scope fingerprint.

### C-03: Export status

- Endpoint: `GET /api/v1/order-exports/{export_id}`.
- States: `queued`, `running`, `verifying`, `completed`, `failed`, `expired`, and operational `cancelled`.
- State transitions are monotonic except controlled retry within a non-terminal state.
- `processed_rows` is progress only; `generated_row_count` is final only after verification.

### C-04: Download

- Endpoint: `GET /api/v1/order-exports/{export_id}/content`.
- Requires `completed`, unexpired file, requester ownership, tenant match, live `orders.export`, and matching authorization-scope fingerprint.
- Streams the immutable private object with `Cache-Control: private, no-store` and `X-Content-Type-Options: nosniff`.
- Audits successful and denied attempts.

### C-05: CSV bytes

- Schema: `orders-csv.v1`.
- Header: `order_id,order_date,customer,status,currency,total`.
- Encoding/delimiter/line ending: UTF-8 with BOM, comma, CRLF, pending final consumer sign-off.
- Ordering: `business_timestamp ASC, order_id ASC`.
- Formula defense: apostrophe-prefix dangerous textual cells, pending final sign-off.
- Completeness: `generated_row_count == matched_row_count` before completion.

## 5. Decomposition into buildable units

These are ordered to prevent downstream implementation from inventing unresolved behavior. Time limits are investigation/build checkpoints, not delivery estimates.

| ID | Unit | In | Out / done when | Kill switch | Logical output | Trace |
|---|---|---|---|---|---|---|
| U-01 | Resolve product/data decisions | Business spec + D-01..D-15 | Signed decision log; no schema, date, customer, money, consistency, permission, notification, or retention ambiguity remains | 60-min PM session: if any binding decision lacks owner and due date, contract stays Draft | Decision log in technical spec | D-01..D-15 |
| U-02 | Canonical filter and shared query | Existing table query + visibility rules | One normalized filter and one predicate/query service used by table count and export; parity fixtures pass | 1 day: if table semantics cannot be located or stated, halt export work and repair table contract | Shared order-query module + parity tests | A-01, C-01 |
| U-03 | Snapshot proof | Query service + datastore capabilities + volume data | Chosen mechanism proves fixed membership and six values during concurrent mutations | 1 day spike: if native snapshot/temporal read is not viable, choose staging and re-estimate before continuing | Snapshot adapter + concurrency test | A-02, N-10 |
| U-04 | Export resource and persistence | C-02/C-03 + retention policy | Metadata schema and state machine enforce valid transitions, idempotency, TTL, counts, checksum, and error codes | 4 hours: if a terminal state can transition or idempotency can create two resources in tests, halt | Export repository/state module | C-02, C-03 |
| U-05 | Permission and scope binding | IAM permission + row-level policy | Create/status/download checks pass negative matrix; scope fingerprint invalidates stale access | 1 day: if current IAM cannot revalidate scope, halt and obtain Security-approved alternative | Export authorization policy + tests | A-06, C-04, N-02/N-05 |
| U-06 | CSV encoder | Signed `orders-csv.v1` + typed order projection | Golden files pass exact byte, Unicode, escaping, line-ending, ID, date, money, and formula corpus | 4 hours: if target spreadsheet/finance app interprets a dangerous test as formula or corrupts IDs/Unicode, halt schema sign-off | Versioned CSV encoder + golden corpus | A-04, C-05, N-07 |
| U-07 | Streaming generator and verifier | Snapshot adapter + encoder + private storage | Bounded-memory generation; row count/checksum verified; temp object atomically finalized; fault tests expose no partial file | 1 day: if memory grows with row count or a failed attempt becomes downloadable once, halt | Generator + object lifecycle adapter | A-07, N-04/N-08/N-09 |
| U-08 | Inline/async orchestration | Export resource + worker/queue | `<=10k` attempts inline within budget; slow/large work continues async on same resource; retries are idempotent | Scale test: if export load breaches agreed dashboard DB/latency budget, halt rollout and tune isolation/limits | API controller + queue worker | A-05/A-10, C-02/C-03 |
| U-09 | Authenticated content delivery | Completed object + auth policy | Only valid requester downloads finalized bytes; expired/revoked/scope-changed cases fail and audit | Security test: any cross-user, cross-tenant, revoked, or expired read succeeds once -> release blocker | Download controller | A-06, C-04 |
| U-10 | UI state and notification | API/resource states + Orders Admin filters | Button, captured request, inline download, async progress, completion, no-data, retry, expiry, and access-change states meet UX contract | 4 hours: if navigation mutates captured export or completion is lost inside the app session, halt | Orders Admin export UI + app notification binding | UX, AC-4 |
| U-11 | Audit and metrics | Lifecycle events + policy | All required events/metrics contain IDs and safe metadata; no row/credential leakage; alerts configured | Test run: any lifecycle transition lacks audit or any sensitive row value enters telemetry -> release blocker | Audit adapter + dashboards/alerts | A-08, N-06, AC-12 |
| U-12 | End-to-end, scale, and security gate | U-02..U-11 | Acceptance suite passes at 0, 137, 10k, 10,001, p95/p99/worst case; recovery and retention proven | Any row mismatch, partial publish, auth bypass, formula execution, or unbounded resource use -> no launch | CI/integration suite + readiness report | AC-1..AC-12 |
| U-13 | Feature-flagged rollout | Readiness report + runbook | Internal validation, canary, monitoring, rollback, and lifecycle deletion evidence complete | Canary breaches agreed failure, queue-age, DB-load, or security threshold -> disable new creation | Feature flag + operational runbook | NFR, success measures |

## 6. Dependency and substrate register

| ID | Category | Proposed choice | Status | Owner / due |
|---|---|---|---|---|
| SUB-01 | Orders data source | Existing Orders Admin transactional/read datastore | Open: query and snapshot capability must be identified | Engineering / before estimate |
| SUB-02 | Metadata persistence | Existing approved relational persistence | Open: map `OrderExport` schema | Engineering / before build |
| SUB-03 | Queue/jobs | Existing approved background-job platform | Open: name service and retry semantics | Platform / before build |
| SUB-04 | File storage | Existing private object store with lifecycle rules | Open: name service and encryption/lifecycle controls | Platform + Security / before build |
| SUB-05 | Auth | Existing identity plus new `orders.export` entitlement | Open: role mapping and scope fingerprint API | IAM + PM / before build |
| SUB-06 | Notification | Existing global in-app notification plus status polling/event | Open: confirm survives route navigation | Frontend + PM / before UX sign-off |
| SUB-07 | Observability | Existing audit log, metrics, tracing, and alerting | Open: retention and PII-safe field policy | Security + SRE / before launch |
| SUB-08 | Timezone | Organization profile IANA timezone service | Open: authoritative config path and fallback telemetry | Platform / before build |
| SUB-09 | Currency metadata | Existing exact amount representation and ISO currency exponent source | Open: storage model validation | Data + Finance / before schema freeze |

No new third-party provider is approved by this draft. Engineering must map each logical dependency to an existing approved platform component or bring a separate architecture decision.

## 7. Contract sign-off gates

This contract moves from Draft to Approved only when:

1. PM decisions D-01 through D-15 are signed or assigned with a non-blocking due date.
2. SUB-01 through SUB-09 name real approved services or explicit local components.
3. The repository locations and owning teams for U-02 through U-13 are recorded.
4. Operations/Finance accepts golden examples for `orders-csv.v1`.
5. Security accepts download reauthorization, scope-change behavior, formula defense, audit fields, and retention.
6. Volume testing supports thresholds and any operational maximum.
7. The Definition of Ready in the technical specification is satisfied.
