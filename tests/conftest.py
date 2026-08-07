from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import pytest


EV_IDS = tuple(f"EV-{number:03d}" for number in range(1, 43))
EV_ID_SET = frozenset(EV_IDS)
_OUTCOMES: dict[str, str] = {}


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
    config._specops_ev_nodes = defaultdict(list)  # type: ignore[attr-defined]
    _OUTCOMES.clear()
    config._specops_ev_outcomes = _OUTCOMES  # type: ignore[attr-defined]
    config._specops_ev_release_run = _full_release_run(config)  # type: ignore[attr-defined]


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    nodes = config._specops_ev_nodes  # type: ignore[attr-defined]
    invalid: set[str] = set()
    for item in items:
        for marker in item.iter_markers(name="ev"):
            if len(marker.args) != 1 or not isinstance(marker.args[0], str):
                raise pytest.UsageError(f"{item.nodeid}: @pytest.mark.ev requires one string ID")
            ev_id = marker.args[0]
            if ev_id not in EV_ID_SET:
                invalid.add(ev_id)
            nodes[ev_id].append(item.nodeid)
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
    if bad:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    config = terminalreporter.config
    if not config._specops_ev_release_run:  # type: ignore[attr-defined]
        return
    nodes = config._specops_ev_nodes  # type: ignore[attr-defined]
    outcomes = config._specops_ev_outcomes  # type: ignore[attr-defined]
    terminalreporter.section("EV-001..EV-042 release evidence")
    for ev_id in EV_IDS:
        mapped = ", ".join(
            f"{nodeid} [{outcomes.get(nodeid, 'COLLECTED' if config.option.collectonly else 'NOT_RUN')}]"
            for nodeid in nodes[ev_id]
        )
        terminalreporter.write_line(f"{ev_id}: {mapped}")
