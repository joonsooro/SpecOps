#!/usr/bin/env python3
"""Generate the executable V4 contract and provider schema artifacts.

The approved bundle in ``Spec_Eng`` is the source of truth.  Run without flags
to regenerate or with ``--check`` to prove that committed generated artifacts
have not drifted.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import shutil
import sys
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
GENERATED_PACKAGE = BACKEND_ROOT / "src" / "specops_workshop" / "v4"
SHARED_PACKAGE = BACKEND_ROOT / "src" / "specops_contracts"
SCHEMA_DESTINATION = SHARED_PACKAGE / "schemas"
NATIVE_DESTINATION = GENERATED_PACKAGE / "generated" / "openai"
CONTRACT_FILENAMES = (
    "artifact-envelope.schema.json",
    "spec-package.schema.json",
    "spec-package-payload.schema.json",
    "final-spec-package-view.schema.json",
    "technical-contract.schema.json",
    "technical-contract-payload.schema.json",
    "technical-contract-view.schema.json",
)


def locate_contract_bundle() -> Path:
    override = os.environ.get("SPECOPS_V4_CONTRACT_ROOT")
    candidates = [Path(override)] if override else []
    candidates.extend(
        (
            BACKEND_ROOT.parent / "Spec_Eng" / "spec-workshop-contracts-and-interaction-model-v4",
            BACKEND_ROOT.parents[1] / "spec-workshop-contracts-and-interaction-model-v4",
        )
    )
    for candidate in candidates:
        if (candidate / "workshop_interaction_protocol_v1.py").is_file():
            return candidate.resolve()
    raise SystemExit("cannot locate the approved V4 contract bundle")


def write_or_check(path: Path, content: bytes, *, check: bool) -> None:
    if check:
        if not path.is_file() or path.read_bytes() != content:
            raise SystemExit(f"generated artifact drift: {path.relative_to(BACKEND_ROOT)}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def generate(*, check: bool) -> None:
    source = locate_contract_bundle()
    contract_source = (source / "workshop_interaction_protocol_v1.py").read_bytes()
    audit_contract_source = (source / "artifact_quality_audit_v1.py").read_bytes()
    quality_contract_source = (source / "semantic-quality-contract.yaml").read_bytes()
    write_or_check(SHARED_PACKAGE / "workshop_v1.py", contract_source, check=check)
    write_or_check(
        SHARED_PACKAGE / "artifact_quality_v1.py", audit_contract_source, check=check
    )
    write_or_check(
        SHARED_PACKAGE / "semantic-quality-contract.yaml",
        quality_contract_source,
        check=check,
    )
    for filename in CONTRACT_FILENAMES:
        payload = json.dumps(
            json.loads((source / filename).read_text(encoding="utf-8")),
            indent=2,
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8") + b"\n"
        write_or_check(SCHEMA_DESTINATION / filename, payload, check=check)

    sys.path.insert(0, str(BACKEND_ROOT / "src"))
    importlib.invalidate_caches()
    compiler = importlib.import_module("specops_workshop.v4.schema_compiler")
    for operation, schema in compiler.all_native_schemas().items():
        payload = json.dumps(schema, indent=2, ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n"
        write_or_check(NATIVE_DESTINATION / f"{operation.lower()}.schema.json", payload, check=check)
    audit_schema = json.dumps(
        compiler.artifact_quality_native_schema(),
        indent=2,
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8") + b"\n"
    write_or_check(
        NATIVE_DESTINATION / "artifact_quality_audit.schema.json",
        audit_schema,
        check=check,
    )

    provenance = {
        "contract_source": "workshop_interaction_protocol_v1.py",
        "contract_sha256": hashlib.sha256(contract_source).hexdigest(),
        "protocol_version": "1.0.0",
        "artifact_quality_audit_protocol_version": "1.0.0",
        "artifact_quality_contract_source": "artifact_quality_audit_v1.py",
        "artifact_quality_contract_sha256": hashlib.sha256(audit_contract_source).hexdigest(),
        "semantic_quality_contract_version": "2.1.0",
        "semantic_quality_contract_sha256": hashlib.sha256(quality_contract_source).hexdigest(),
        "artifact_schema_versions": {
            "envelope": "2.0.0",
            "spec_package": "4.0.0",
            "technical_contract": "4.0.0",
            "read_models": "3.0.0",
        },
        "openai_operations": sorted(compiler.all_native_schemas()),
        "openai_artifact_quality_schema": "artifact_quality_audit",
    }
    write_or_check(
        GENERATED_PACKAGE / "generated" / "provenance.json",
        json.dumps(provenance, indent=2, sort_keys=True).encode("utf-8") + b"\n",
        check=check,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args()
    generate(check=arguments.check)


if __name__ == "__main__":
    main()
