# Deterministic Workflow Foundation

This package is a headless, in-process Python policy and state kernel. Callers submit strict Pydantic commands to `WorkflowService`; the service validates authority, exact artifact bindings, registered evidence, projection shape, operation confirmation, and derived workflow state. It performs no network requests and does not read registered source paths.

## Requirements

- Python 3.12
- SQLite

Create an environment and install the package with its test dependencies:

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e '.[test]'
```

`SPECOPS_DATABASE_URL` is read when `WorkflowService` is constructed, never at import time. An explicit `database_url=` overrides it and is the preferred deterministic-test boundary.

## Anonymous public workflow

The executable example generates all actor, case, command, source, and package UUIDs. It migrates a previously nonexistent temporary SQLite path, creates the immutable authority slots, demonstrates an unauthorized participant command returning `AUTHORITY_REQUIRED`, registers evidence, creates and approves a package in both scopes, and reads the workflow view.

```bash
.venv/bin/python examples/anonymous_workflow.py
```

The expected milestones are:

```text
permitted_command=create_case 1
blocked_command=add_participant AUTHORITY_REQUIRED
```

The final JSON view is at `JIRA_REVIEW`: the package is approved, while no projection plan has been supplied yet. Missing future-stage work is not itself a health failure.

## Release verification

Run the complete bar from the repository root:

```bash
.venv/bin/pytest -q
```

The full-suite pytest plug-in requires the exact EV-001 through EV-042 marker inventory, rejects skips/failures/not-run evidence, and prints every EV ID with its exercising node and outcome. The checked-in snapshots cover all 21 command DTOs, 11 mutation results, six read/page models, and generic `ReplayResult`; every schema forbids unknown fields. The EV-042 test independently migrates a fresh SQLite path and inspects metadata, foreign keys, Alembic head, UTC round-trip, and both append-only audit triggers.
