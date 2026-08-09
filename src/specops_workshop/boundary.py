from __future__ import annotations

import ast
import re
from pathlib import Path


FORBIDDEN_IMPORT_ROOTS = frozenset({
    "atlassian",
    "github",
    "jira",
    "pygithub",
})
FORBIDDEN_ENDPOINT_PATTERNS = (
    re.compile(r"https?://[^\s\"']*atlassian\.net", re.IGNORECASE),
    re.compile(r"https?://api\.github\.com", re.IGNORECASE),
)


def assert_downstream_boundary(backend_root: Path) -> None:
    """Fail closed if Workshop gains a Jira/GitHub runtime dependency or endpoint."""
    workshop_root = backend_root / "src" / "specops_workshop"
    violations: list[str] = []
    for path in sorted(workshop_root.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if name.split(".", 1)[0].lower() in FORBIDDEN_IMPORT_ROOTS:
                    violations.append(f"forbidden import in {path.name}")
        if any(pattern.search(source) for pattern in FORBIDDEN_ENDPOINT_PATTERNS):
            violations.append(f"forbidden platform endpoint in {path.name}")

    pyproject = (backend_root / "pyproject.toml").read_text(encoding="utf-8")
    dependency_region = pyproject.split("[tool.setuptools", 1)[0].casefold()
    if any(re.search(rf"[\"']{name}(?:[<>=~!\[]|[\"'])", dependency_region) for name in FORBIDDEN_IMPORT_ROOTS):
        violations.append("forbidden platform dependency in pyproject.toml")
    if violations:
        raise ValueError("Workshop downstream boundary violation: " + "; ".join(violations))
