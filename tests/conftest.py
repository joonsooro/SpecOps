from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path

import pytest

from release_contract import AUTHORIZATION_ROWS, EV_IDS, OPERATION_BRANCHES, RELEASE_METRICS

EV_ID_SET = frozenset(EV_IDS)
_OUTCOMES: dict[str, str] = {}
_METRICS: dict[str, list[tuple[str, int]]] = defaultdict(list)


def _full_release_run(config: pytest.Config) -> bool:
    """Only enforce the session-wide gate for the default/full test path."""
    args = [str(value) for value in config.args]
    return args == ["tests"] or (
        len(args) == 1 and Path(args[0]).name == "tests" and "::" not in args[0]
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "ev(id): binary release-bar evidence ID from app/evals.md",
    )
    config.addinivalue_line(
        "markers",
        "release_evidence(kind, *claims): executable authorization/operation claim coverage",
    )
    config._specops_ev_nodes = defaultdict(list)  # type: ignore[attr-defined]
    config._specops_release_claims = {
        "authorization": defaultdict(list),
        "operation": defaultdict(list),
    }  # type: ignore[attr-defined]
    _OUTCOMES.clear()
    _METRICS.clear()
    config._specops_ev_outcomes = _OUTCOMES  # type: ignore[attr-defined]
    config._specops_ev_release_run = _full_release_run(config)  # type: ignore[attr-defined]


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    nodes = config._specops_ev_nodes  # type: ignore[attr-defined]
    claims = config._specops_release_claims  # type: ignore[attr-defined]
    invalid: set[str] = set()
    for item in items:
        for marker in item.iter_markers(name="ev"):
            if len(marker.args) != 1 or not isinstance(marker.args[0], str):
                raise pytest.UsageError(f"{item.nodeid}: @pytest.mark.ev requires one string ID")
            ev_id = marker.args[0]
            if ev_id not in EV_ID_SET:
                invalid.add(ev_id)
            nodes[ev_id].append(item.nodeid)
        for marker in item.iter_markers(name="release_evidence"):
            if len(marker.args) < 2 or marker.args[0] not in {"authorization", "operation"}:
                raise pytest.UsageError(f"{item.nodeid}: invalid release_evidence marker")
            kind = marker.args[0]
            allowed = set(AUTHORIZATION_ROWS if kind == "authorization" else OPERATION_BRANCHES)
            for claim in marker.args[1:]:
                if not isinstance(claim, str) or claim not in allowed:
                    raise pytest.UsageError(f"{item.nodeid}: unknown {kind} claim {claim!r}")
                claims[kind][claim].append(item.nodeid)
    if invalid:
        raise pytest.UsageError(f"Unknown EV IDs collected: {sorted(invalid)}")
    if config._specops_ev_release_run:  # type: ignore[attr-defined]
        missing = EV_ID_SET.difference(nodes)
        extra = set(nodes).difference(EV_ID_SET)
        if missing or extra:
            raise pytest.UsageError(
                f"Release EV inventory mismatch; missing={sorted(missing)}, extra={sorted(extra)}"
            )


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if report.when != "call":
        return
    _OUTCOMES[report.nodeid] = "SKIPPED" if report.skipped else "PASSED" if report.passed else "FAILED"
    for key, value in report.user_properties:
        if key != "specops_metric":
            continue
        if not isinstance(value, dict) or value.get("name") not in RELEASE_METRICS or type(value.get("value")) is not int:
            raise pytest.UsageError(f"{report.nodeid}: invalid specops_metric evidence")
        _METRICS[value["name"]].append((report.nodeid, value["value"]))


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    config = session.config
    if not config._specops_ev_release_run or config.option.collectonly:  # type: ignore[attr-defined]
        return
    nodes = config._specops_ev_nodes  # type: ignore[attr-defined]
    outcomes = config._specops_ev_outcomes  # type: ignore[attr-defined]
    bad = {
        ev_id: [(nodeid, outcomes.get(nodeid, "NOT_RUN")) for nodeid in nodes[ev_id]]
        for ev_id in EV_IDS
        if any(outcomes.get(nodeid) != "PASSED" for nodeid in nodes[ev_id])
    }
    claims = config._specops_release_claims  # type: ignore[attr-defined]
    missing_claims = {
        kind: sorted(set(expected).difference(claims[kind]))
        for kind, expected in {
            "authorization": AUTHORIZATION_ROWS,
            "operation": OPERATION_BRANCHES,
        }.items()
    }
    bad_claims = {
        kind: {
            claim: [(nodeid, outcomes.get(nodeid, "NOT_RUN")) for nodeid in nodeids]
            for claim, nodeids in claims[kind].items()
            if not nodeids or any(outcomes.get(nodeid) != "PASSED" for nodeid in nodeids)
        }
        for kind in ("authorization", "operation")
    }
    bad_metrics = {
        name: values
        for name in RELEASE_METRICS
        if len(values := _METRICS.get(name, [])) != 1
        or values[0][1] != 0
        or outcomes.get(values[0][0]) != "PASSED"
    }
    evidence = {
        "inventory": list(EV_IDS),
        "ev_evidence": {ev_id: [{"nodeid": nodeid, "outcome": outcomes.get(nodeid, "NOT_RUN")} for nodeid in nodes[ev_id]] for ev_id in EV_IDS},
        "authorization_evidence": {claim: [{"nodeid": nodeid, "outcome": outcomes.get(nodeid, "NOT_RUN")} for nodeid in claims["authorization"].get(claim, [])] for claim in AUTHORIZATION_ROWS},
        "operation_evidence": {claim: [{"nodeid": nodeid, "outcome": outcomes.get(nodeid, "NOT_RUN")} for nodeid in claims["operation"].get(claim, [])] for claim in OPERATION_BRANCHES},
        "derived_metrics": {name: {"nodeid": values[0][0], "value": values[0][1]} if len(values := _METRICS.get(name, [])) == 1 else {"evidence": values} for name in RELEASE_METRICS},
        "passed": not bad and not any(missing_claims.values()) and not any(bad_claims.values()) and not bad_metrics,
    }
    (Path(__file__).parent / "release-evidence.json").write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    if not evidence["passed"]:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    config = terminalreporter.config
    if not config._specops_ev_release_run:  # type: ignore[attr-defined]
        return
    nodes = config._specops_ev_nodes  # type: ignore[attr-defined]
    outcomes = config._specops_ev_outcomes  # type: ignore[attr-defined]
    terminalreporter.section("EV-001..EV-044 executable release evidence")
    for ev_id in EV_IDS:
        mapped = ", ".join(
            f"{nodeid} [{outcomes.get(nodeid, 'COLLECTED' if config.option.collectonly else 'NOT_RUN')}]"
            for nodeid in nodes[ev_id]
        )
        terminalreporter.write_line(f"{ev_id}: {mapped}")
    terminalreporter.write_line("Derived runtime metrics:")
    for name in RELEASE_METRICS:
        terminalreporter.write_line(f"{name}: {_METRICS.get(name, [])}")
