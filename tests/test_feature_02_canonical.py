from specops_workflow.canonical import artifact_hash, canonical_json, sha256
from specops_workflow.enums import ArtifactKind


def test_artifact_golden_vectors():
    assert artifact_hash(ArtifactKind.SPEC_PACKAGE, 1, 1, {"requirements": []}) == "c7a90a1c94b0e55bc55a42fe99c08d3149a891e75b8b22095bda04f1bae32675"
    assert artifact_hash(ArtifactKind.PROJECTION_PLAN, 1, 1, {"dependencies": ["a", "b"], "labels": ["a", "z"]}) == "33819f262772cf5f8c356edc08a92f16868e89a21bf0fb32e405fd4af662c0d9"
    assert artifact_hash(ArtifactKind.STATUS_POLICY, 1, 2, {"mappings": [], "rules": []}) == "056050a53c18bca89bec435279ea34a9649f4ae09270f8439772f18bb8f77db1"


def test_projection_item_golden_vector():
    item = {"body":{"acceptance_checks":[],"dependency_item_ids":[],"generation_key":"specops:00000000-0000-4000-8000-000000000004:jira:00000000-0000-4000-8000-000000000001","jira_key":None,"package_hash":"0"*64,"package_id":"00000000-0000-4000-8000-000000000003","package_version":1,"provisional":False,"source_unit_ids":["00000000-0000-4000-8000-000000000002"]},"dependency_item_ids":[],"domain":"BUSINESS","implementation_required":False,"item_id":"00000000-0000-4000-8000-000000000001","kind":"TASK","parent_item_id":None,"primary_jira_item_id":None,"repository":None,"source_unit_ids":["00000000-0000-4000-8000-000000000002"],"title":"Example"}
    assert sha256({"item": item, "project_key": "OPS", "schema": "projection-item-v1", "target": "JIRA"}) == "eb9c6b618b249afed2ceefcd414e6c4d030f39640a95457053e0ce3979a15032"


def test_operation_golden_vector_and_key_order():
    value = {"action":"CREATE","case_id":"00000000-0000-4000-8000-000000000004","contributing_rule_ids":[],"expected_remote_revision":None,"external_identity":None,"generation_key":"specops:00000000-0000-4000-8000-000000000004:jira:00000000-0000-4000-8000-000000000001","item_ref":{"item_id":"00000000-0000-4000-8000-000000000001","kind":"CURRENT","plan_hash":"1"*64,"plan_id":"00000000-0000-4000-8000-000000000005","plan_version":1},"package_binding":{"artifact_id":"00000000-0000-4000-8000-000000000003","artifact_kind":"SPEC_PACKAGE","hash":"0"*64,"version":1},"plan_binding":{"artifact_id":"00000000-0000-4000-8000-000000000005","artifact_kind":"PROJECTION_PLAN","hash":"1"*64,"version":1},"request_owned_content_hash":"2"*64,"schema":"operation-request-v1","status_policy_binding":None,"system":"JIRA","target_normalized_status":None}
    assert sha256(value) == "e5d5d286c021d0c46dc125e0cc3a7d7868e6d768a905946edc8160e7f0e841a6"
    assert canonical_json({"z": 1, "a": 2}) == b'{"a":2,"z":1}'

