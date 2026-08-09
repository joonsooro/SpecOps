from __future__ import annotations


SW_IDS = tuple(f"SW-EV-{index:03d}" for index in range(1, 25))

# Every row names the executable evidence that must pass. Browser/TypeScript
# rows are executed by the release runner in addition to pytest.
SW_EVIDENCE = {
    "SW-EV-001": (("playwright", "frontend/e2e/workshop.spec.ts", "renders the governed three-panel projection and focuses exact evidence"),),
    "SW-EV-002": (
        ("pytest", "tests/workshop/test_feature_20_release.py", "test_authorized_terra_context_is_exact_and_excludes_contract"),
        ("pytest", "tests/workshop/test_feature_20_release.py", "test_terra_gateway_minimizes_committed_governance_identifiers"),
    ),
    "SW-EV-003": (("pytest", "tests/workshop/test_feature_20_release.py", "test_delegation_identity_scope_and_frontmatter_fail_closed"),),
    "SW-EV-004": (("pytest", "tests/test_feature_13_package_items.py", "test_partial_readiness_selective_revision_and_restart"),),
    "SW-EV-005": (("pytest", "tests/workshop/test_feature_16_live_transport.py", "test_binary_audio_text_interrupt_and_end_share_one_live_session"),),
    "SW-EV-006": (("pytest", "tests/workshop/test_feature_15_recovery.py", "test_final_transcripts_are_immutable_correctable_deduplicated_and_resumable"), ("pytest", "tests/workshop/test_feature_20_release.py", "test_interruption_marker_is_idempotent_and_recoverable")),
    "SW-EV-007": (("vitest", "frontend/src/audio/liveClient.test.ts", "distinguishes speech energy from silence within one 20 ms frame"), ("pytest", "tests/workshop/test_feature_20_release.py", "test_five_stage_spans_and_thirty_turn_latency_bar")),
    "SW-EV-008": (("pytest", "tests/workshop/test_feature_20_release.py", "test_five_stage_spans_and_thirty_turn_latency_bar"),),
    "SW-EV-009": (("pytest", "tests/workshop/test_feature_17_analyzer_gate.py", "test_substantive_voice_continuation_waits_for_typed_confirmed_foundation_commit"),),
    "SW-EV-010": (("pytest", "tests/workshop/test_feature_20_release.py", "test_semantic_grounding_rejects_an_unrelated_conclusion"), ("pytest", "tests/workshop/test_feature_17_analyzer_gate.py", "test_missing_fields_reused_keys_and_model_uuid_are_rejected")),
    "SW-EV-011": (("pytest", "tests/workshop/test_feature_17_analyzer_gate.py", "test_control_acknowledgement_question_and_extra_fields_are_strict"), ("pytest", "tests/workshop/test_feature_17_analyzer_gate.py", "test_substantive_voice_continuation_waits_for_typed_confirmed_foundation_commit")),
    "SW-EV-012": (("pytest", "tests/workshop/test_feature_17_analyzer_gate.py", "test_final_evidence_proposes_without_mutation_then_confirm_commits_exact_cross_domain_item"), ("playwright", "frontend/e2e/workshop.spec.ts", "visible controls keep native focus and formulation state")),
    "SW-EV-013": (("pytest", "tests/workshop/test_feature_17_analyzer_gate.py", "test_confirmation_requires_explicit_prompt_context_and_generic_ack_is_not_a_commit"),),
    "SW-EV-014": (("pytest", "tests/workshop/test_feature_17_analyzer_gate.py", "test_final_evidence_proposes_without_mutation_then_confirm_commits_exact_cross_domain_item"),),
    "SW-EV-015": (("pytest", "tests/test_feature_13_package_items.py", "test_delegated_cross_domain_item_is_ready_with_later_review_until_direct_approval"),),
    "SW-EV-016": (("pytest", "tests/workshop/test_feature_19_finish_handoff.py", "test_explicit_technical_blocker_creates_native_decision_request_and_may_finish"),),
    "SW-EV-017": (("pytest", "tests/test_feature_13_package_items.py", "test_partial_readiness_selective_revision_and_restart"),),
    "SW-EV-018": (("pytest", "tests/workshop/test_feature_19_finish_handoff.py", "test_finish_runs_medium_audit_keeps_voice_live_and_exposes_exact_idempotent_handoff"), ("pytest", "tests/workshop/test_feature_19_finish_handoff.py", "test_pending_patch_prevents_finish_before_medium_audit")),
    "SW-EV-019": (("playwright", "frontend/e2e/workshop.spec.ts", "Finish freezes formulation, preserves text, and renders exact handoff facts"),),
    "SW-EV-020": (("pytest", "tests/workshop/test_feature_20_release.py", "test_midstream_provider_and_device_failure_keep_text_fallback"), ("pytest", "tests/workshop/test_feature_19_finish_handoff.py", "test_reconnect_sends_only_authorized_exact_context_in_turn_version_order")),
    "SW-EV-021": (("pytest", "tests/workshop/test_feature_15_recovery.py", "test_unknown_after_foundation_commit_replays_same_command_without_duplicate_audit"), ("pytest", "tests/workshop/test_feature_15_recovery.py", "test_unexplained_foundation_revision_locks_session_without_alternate_state")),
    "SW-EV-022": (("pytest", "tests/workshop/test_feature_20_release.py", "test_operational_telemetry_redacts_and_secrets_stay_opaque"), ("pytest", "tests/workshop/test_feature_16_live_transport.py", "test_audio_buffers_are_hard_bounded_and_never_touch_disk")),
    "SW-EV-023": (("pytest", "tests/workshop/test_feature_20_release.py", "test_downstream_boundary_has_no_platform_runtime_surface"), ("pytest", "tests/workshop/test_feature_19_finish_handoff.py", "test_finish_runs_medium_audit_keeps_voice_live_and_exposes_exact_idempotent_handoff")),
    "SW-EV-024": (("live", "tests/workshop/live_workshop_probe.py", "real Gemini audio plus Terra decision and explicit governed commit"),),
}
