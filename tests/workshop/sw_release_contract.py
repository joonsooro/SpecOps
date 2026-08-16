from __future__ import annotations


SW_IDS = tuple(f"SW-EV-{index:03d}" for index in range(1, 25))

# Every row names the executable evidence that must pass. Browser/TypeScript
# rows are executed by the release runner in addition to pytest.
SW_EVIDENCE = {
    "SW-EV-001": (("playwright", "frontend/e2e/workshop.spec.ts", "preserves the governed three-panel reading order and narrow layout"),),
    "SW-EV-002": (
        ("pytest", "tests/workshop/test_feature_24_productive_turn.py", "test_s2_c1_native_schemas_and_two_block_egress_are_identity_free"),
    ),
    "SW-EV-003": (("pytest", "tests/workshop/test_feature_20_release.py", "test_delegation_identity_scope_and_frontmatter_fail_closed"),),
    "SW-EV-004": (("pytest", "tests/test_feature_13_package_items.py", "test_partial_readiness_selective_revision_and_restart"),),
    "SW-EV-005": (("playwright", "frontend/e2e/workshop.spec.ts", "playback is user-triggered, exact-text, and does not open a live input socket"), ("pytest", "tests/workshop/test_feature_28_text_authoritative_workshop.py", "test_openai_chatbot_provider_is_luna_medium_stateless_and_selection_only")),
    "SW-EV-006": (("pytest", "tests/workshop/test_feature_28_text_authoritative_workshop.py", "test_ingress_idempotency_correction_and_channel_provenance_survive_restart"), ("playwright", "frontend/e2e/workshop.spec.ts", "correction control loads the exact latest response binding and text")),
    "SW-EV-007": (("pytest", "tests/workshop/test_feature_28_text_authoritative_workshop.py", "test_luna_selection_is_materialized_from_exact_supplied_foundation_questions"), ("playwright", "frontend/e2e/workshop.spec.ts", "renders canonical question messages and submits the exact typed response intent")),
    "SW-EV-008": (("pytest", "tests/workshop/test_feature_28_text_authoritative_workshop.py", "test_ingress_idempotency_correction_and_channel_provenance_survive_restart"), ("pytest", "tests/workshop/test_feature_28_text_authoritative_workshop.py", "test_v0_runtime_exposes_only_chat_ingress_and_read_only_exact_playback")),
    "SW-EV-009": (("pytest", "tests/workshop/test_feature_28_text_authoritative_workshop.py", "test_participant_turn_ingress_commits_every_authoritative_effect_atomically"),),
    "SW-EV-010": (("pytest", "tests/workshop/test_feature_20_release.py", "test_semantic_grounding_rejects_an_unrelated_conclusion"), ("pytest", "tests/workshop/test_feature_17_analyzer_gate.py", "test_missing_fields_reused_keys_and_model_uuid_are_rejected")),
    "SW-EV-011": (("pytest", "tests/workshop/test_feature_28_text_authoritative_workshop.py", "test_luna_selection_is_materialized_from_exact_supplied_foundation_questions"),),
    "SW-EV-012": (("pytest", "tests/workshop/test_feature_28_text_authoritative_workshop.py", "test_visual_edit_and_reject_are_explicit_idempotent_proposal_actions"), ("playwright", "frontend/e2e/workshop.spec.ts", "Confirm, Edit, and Reject use only their explicit proposal endpoints")),
    "SW-EV-013": (("playwright", "frontend/e2e/workshop.spec.ts", "renders canonical question messages and submits the exact typed response intent"), ("pytest", "tests/workshop/test_feature_28_text_authoritative_workshop.py", "test_visual_edit_and_reject_are_explicit_idempotent_proposal_actions")),
    "SW-EV-014": (("pytest", "tests/workshop/test_feature_17_analyzer_gate.py", "test_final_evidence_proposes_without_mutation_then_confirm_commits_exact_cross_domain_item"),),
    "SW-EV-015": (("pytest", "tests/test_feature_13_package_items.py", "test_delegated_cross_domain_item_is_ready_with_later_review_until_direct_approval"),),
    "SW-EV-016": (("pytest", "tests/workshop/test_feature_19_finish_handoff.py", "test_explicit_technical_blocker_creates_native_decision_request_and_may_finish"), ("playwright", "frontend/e2e/workshop.spec.ts", "Finish Workshop is a separate deliberate control with its exact intent")),
    "SW-EV-017": (("pytest", "tests/test_feature_13_package_items.py", "test_partial_readiness_selective_revision_and_restart"),),
    "SW-EV-018": (("pytest", "tests/workshop/test_feature_27_v0_preparation_runtime.py", "test_button_completion_endpoint_is_durable_idempotent_and_closes_new_turns"), ("playwright", "frontend/e2e/workshop.spec.ts", "Finish Workshop is a separate deliberate control with its exact intent")),
    "SW-EV-019": (("playwright", "frontend/e2e-real/text-workshop.spec.ts", "real FastAPI and built frontend persist and reconstruct authoritative chat offline"),),
    "SW-EV-020": (("pytest", "tests/workshop/test_feature_28_text_authoritative_workshop.py", "test_ingress_idempotency_correction_and_channel_provenance_survive_restart"), ("playwright", "frontend/e2e-real/text-workshop.spec.ts", "real FastAPI and built frontend persist and reconstruct authoritative chat offline")),
    "SW-EV-021": (("pytest", "tests/workshop/test_feature_28_text_authoritative_workshop.py", "test_ingress_idempotency_correction_and_channel_provenance_survive_restart"), ("pytest", "tests/workshop/test_feature_28_text_authoritative_workshop.py", "test_ingress_failure_rolls_back_snapshot_evidence_consumption_and_job")),
    "SW-EV-022": (("pytest", "tests/workshop/test_feature_20_release.py", "test_operational_telemetry_redacts_and_secrets_stay_opaque"), ("pytest", "tests/workshop/test_feature_28_text_authoritative_workshop.py", "test_v0_runtime_exposes_only_chat_ingress_and_read_only_exact_playback")),
    "SW-EV-023": (("pytest", "tests/workshop/test_feature_20_release.py", "test_downstream_boundary_has_no_platform_runtime_surface"), ("pytest", "tests/workshop/test_feature_28_text_authoritative_workshop.py", "test_ingress_idempotency_correction_and_channel_provenance_survive_restart")),
    "SW-EV-024": (("live", "tests/workshop/live_workshop_probe.py", "real Gemini audio plus Terra decision and explicit governed commit"),),
}
