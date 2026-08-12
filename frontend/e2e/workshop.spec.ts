import { expect, test } from "@playwright/test";

const sourceLines = Array.from({ length: 46 }, (_, index) => {
  const line = index + 1;
  const content: Record<number, string> = {
    1: "# Filtered Orders CSV Export — Draft Technical Specification",
    4: "The export MUST include every order matching the active filter set.",
    12: "The CSV schema is fixed: Order ID, Customer, Status, Total, Created At, Updated At.",
    18: "Large exports MUST be generated asynchronously without truncating filtered rows.",
    27: "Authorization is rechecked when the export is requested and when the file is downloaded.",
    35: "Failed generation returns a stable error and never exposes a partial file.",
    43: "All output is UTF-8 with a single header row.",
  };
  return [line, content[line] ?? ""];
});

const sourceRef = {
  artifact_id: "44444444-4444-4444-8444-444444444444",
  version: 1,
  content_hash: "a".repeat(64),
  location: { kind: "LINE_RANGE", start: 18, end: 18 },
};

const bootstrap = {
  case_id: "33333333-3333-4333-8333-333333333333",
  pm_actor_id: "22222222-2222-4222-8222-222222222222",
  dev_lead_actor_id: "11111111-1111-4111-8111-111111111111",
  delegation_id: "55555555-5555-4555-8555-555555555555",
  delegation_valid_from: "2026-07-23T00:00:00Z",
  delegation_valid_until: "2026-08-22T23:59:59Z",
  delegation_command_scope: ["revise_spec_package", "mark_spec_package_item_ready", "approve_spec_package_item"],
  technical_source_lines: sourceLines,
};

const workshop = {
  session: {
    workshop_state: "ACTIVE",
    conversation_phase: "WORKSHOP",
    call_state: "LISTENING",
    revision_locked: false,
    revision_lock_reason: null,
  },
  final_transcripts: [{
    turn_sequence: 1,
    version: 1,
    normalized_text: "Every filtered row must be exported, even when the result is too large for an immediate download.",
    correction_of_version: null,
  }],
  pending_proposal: {
    record: { proposal_ref: "workshop-patch-demo-v1", status: "PENDING", version: 1 },
    result: {
      acknowledgement: "Drafted grounded package",
      next_question: "Should this cross-domain package be committed?",
      complete_package_proposal: {
        requirements: [{ proposal_key: "all-rows", statement: "Export every order matching the active filter set.", domain: "BUSINESS", source_refs: [sourceRef] }],
        technical_decisions: [{ proposal_key: "async-export", statement: "Generate large exports asynchronously without truncation.", domain: "TECHNICAL", source_refs: [sourceRef] }],
        acceptance_checks: [{ proposal_key: "no-truncation", statement: "A large filtered result contains every matching row.", domain: "CROSS_DOMAIN", related_unit_proposal_keys: ["all-rows", "async-export"], source_refs: [sourceRef] }],
        items: [{ proposal_key: "complete-export", title: "Complete filtered export", requirement_proposal_keys: ["all-rows"], technical_decision_proposal_keys: ["async-export"], acceptance_check_proposal_keys: ["no-truncation"] }],
      },
    },
  },
  governance: {
    package_readiness: "PARTIALLY_READY",
    items: [{
      binding: { item_id: "66666666-6666-4666-8666-666666666666", item_version: 1, semantic_hash: "b".repeat(64) },
      title: "CSV schema and encoding",
      domain: "TECHNICAL",
      readiness: "READY",
      review_obligation: "LATER_REVIEW",
      approval_scopes: ["TECHNICAL"],
    }],
  },
  review_requests: [],
};

test.beforeEach(async ({ page }) => {
  await page.route("**/api/bootstrap", (route) => route.fulfill({ json: bootstrap }));
  await page.route("**/api/workshop", (route) => route.fulfill({ json: workshop }));
  await page.route("**/api/proposals/control", (route) => route.fulfill({ json: { status: "COMMITTED" } }));
  await page.route("**/api/v4/cases/*/decision-review", (route) => route.fulfill({ json: null }));
});

test("renders the exact Foundation decision view used by Voice confirmation", async ({ page }) => {
  const decisionReview = {
    protocol_version: "1.0.0",
    view_type: "DECISION_BATCH_REVIEW",
    view_id: "99999999-9999-4999-8999-999999999999",
    view_hash: `sha256:${"d".repeat(64)}`,
    session_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    based_on_case_revision: 12,
    derived_from_cluster_ids: [],
    items: [{
      review_item_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
      handle: "A",
      pending_decision_id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
      pending_decision_version: 1,
      classification: "PRODUCT",
      exact_statement: "Use UTF-8 for every CSV export.",
      rationale: "Consumers need one stable encoding.",
      problem_origins: [{
        problem_id: "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
        problem_version: 1,
        problem_statement: "The export encoding was not confirmed.",
        resolution_kind: "FULL",
        evidence_summary: "The technical source specifies UTF-8.",
      }],
    }],
    generated_at: "2026-08-12T12:00:00Z",
  };
  await page.unroute("**/api/v4/cases/*/decision-review");
  await page.route("**/api/v4/cases/*/decision-review", (route) => route.fulfill({ json: decisionReview }));
  await page.goto("/");
  const review = page.getByRole("region", { name: "Decision batch" });
  await expect(review).toBeVisible();
  await expect(review.getByText("Use UTF-8 for every CSV export.")).toBeVisible();
  await expect(review.getByText("The export encoding was not confirmed.")).toBeVisible();
  await expect(review.getByText("VOICE BOUND")).toBeVisible();
  await expect(review.getByRole("button")).toHaveCount(0);
});

test("renders the governed three-panel projection and focuses exact evidence", async ({ page }, testInfo) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "CSV Export Workshop" })).toBeVisible();
  await expect(page.getByText("Technical authority delegated")).toBeVisible();
  await expect(page.getByText("Proposed · awaiting PM confirmation")).toBeVisible();
  await expect(page.getByRole("button", { name: "Confirm" })).toBeEnabled();
  if (testInfo.project.name === "narrow") {
    expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBe(0);
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({ path: "/private/tmp/specops-feature18-narrow.png", animations: "disabled", fullPage: true });
  }
  await page.getByRole("button", { name: "Focus technical source line 18" }).first().click();
  await expect(page.locator("#line-18")).toBeFocused();
  await expect(page.locator("#line-18")).toHaveClass(/is-bound/);
  await expect(page.getByRole("button", { name: /BOUND EVIDENCE · L18/ })).toBeVisible();

  const order = await page.locator("main > section").evaluateAll((sections) => sections.map((section) => section.getAttribute("aria-labelledby")));
  expect(order).toEqual(["voice-title", "source-title", "package-title"]);

  if (testInfo.project.name === "desktop") {
    const metrics = await page.locator("main").evaluate((main) => ({
      columns: getComputedStyle(main).gridTemplateColumns.split(" ").map((value) => Number.parseFloat(value)),
      heights: Array.from(main.children).map((child) => getComputedStyle(child).height),
      overflow: Array.from(main.children).map((child) => getComputedStyle(child).overflowY),
    }));
    expect(metrics.columns[0] / metrics.columns.reduce((a, b) => a + b, 0)).toBeCloseTo(.32, 1);
    expect(metrics.columns[1] / metrics.columns.reduce((a, b) => a + b, 0)).toBeCloseTo(.42, 1);
    expect(metrics.heights).toEqual(["900px", "900px", "900px"]);
    expect(metrics.overflow).toEqual(["auto", "auto", "auto"]);
    await page.screenshot({ path: "/private/tmp/specops-feature18-desktop.png", animations: "disabled" });
  } else {
    const boxes = await Promise.all(["voice-title", "source-title", "package-title"].map(async (id) => page.locator(`[aria-labelledby="${id}"]`).boundingBox()));
    expect(boxes[0]!.y).toBeLessThan(boxes[1]!.y);
    expect(boxes[1]!.y).toBeLessThan(boxes[2]!.y);
    await expect(page.getByLabel("Text fallback")).toBeVisible();
    await expect(page.getByRole("button", { name: "Confirm" })).toBeVisible();
  }
});

test("visible controls keep native focus and formulation state", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Edit" }).focus();
  await expect(page.getByRole("button", { name: "Edit" })).toBeFocused();
  await page.getByRole("button", { name: "Edit" }).click();
  await expect(page.getByLabel("Edit instruction")).toBeVisible();
  await expect(page.getByRole("button", { name: "Apply edit" })).toBeDisabled();
  await page.getByLabel("Edit instruction").fill("Keep the fixed six-column order explicit.");
  await expect(page.getByRole("button", { name: "Apply edit" })).toBeEnabled();
});

test("a rendered pending proposal confirms once and remains committed after refresh", async ({ page }) => {
  let pending = true;
  let confirmationCalls = 0;
  await page.unroute("**/api/workshop");
  await page.route("**/api/workshop", (route) => route.fulfill({
    json: { ...workshop, pending_proposal: pending ? workshop.pending_proposal : null },
  }));
  await page.unroute("**/api/proposals/control");
  await page.route("**/api/proposals/control", (route) => {
    confirmationCalls += 1;
    if (confirmationCalls > 1) {
      return route.fulfill({ status: 409, json: { detail: "Proposal already resolved" } });
    }
    pending = false;
    return route.fulfill({ json: { status: "COMMITTED" } });
  });

  await page.goto("/");
  await expect(page.getByText("Proposed · awaiting PM confirmation")).toBeVisible();
  await page.getByRole("button", { name: "Confirm" }).click();
  await expect(page.getByText("Proposal committed to the governed package")).toBeVisible();
  await expect(page.getByText("Proposed · awaiting PM confirmation")).toHaveCount(0);
  await page.reload();
  await expect(page.getByText("Proposed · awaiting PM confirmation")).toHaveCount(0);
  expect(confirmationCalls).toBe(1);
});

test("Finish freezes formulation, preserves text, and renders exact handoff facts", async ({ page }) => {
  let finished = false;
  const packageBinding = {
    artifact_kind: "SPEC_PACKAGE",
    artifact_id: "77777777-7777-4777-8777-777777777777",
    version: 1,
    semantic_hash: "c".repeat(64),
  };
  const readyBinding = {
    package_id: packageBinding.artifact_id,
    package_version: 1,
    package_hash: packageBinding.semantic_hash,
    item_id: "66666666-6666-4666-8666-666666666666",
    item_version: 1,
    item_hash: "b".repeat(64),
  };
  const handoff = {
    package_binding: packageBinding,
    ready_item_bindings: [readyBinding],
    blocked_review_requests: [],
    later_review_requests: [{
      review_request_id: "88888888-8888-4888-8888-888888888888",
      item_binding: readyBinding,
      kind: "LATER_REVIEW",
      question: "Dev Lead, do you approve this delegated technical item?",
      evidence_refs: [sourceRef],
      attempted_resolution: "Approved under active scoped technical delegation.",
      reviewer_actor_ids: [bootstrap.dev_lead_actor_id],
      status: "OPEN",
      resolution_text: null,
      resolution_source_refs: [],
      resolved_by_actor_ids: [],
      created_at: "2026-08-09T12:00:00Z",
      resolved_at: null,
    }],
    transcript_source_refs: [sourceRef],
  };
  const projection = () => ({
    ...workshop,
    pending_proposal: null,
    session: {
      ...workshop.session,
      workshop_state: finished ? "COMPLETED" : "ACTIVE",
      conversation_phase: finished ? "HANDOFF_READY" : "WORKSHOP",
    },
    handoff: finished ? handoff : null,
  });
  await page.unroute("**/api/workshop");
  await page.route("**/api/workshop", (route) => route.fulfill({ json: projection() }));
  await page.route("**/api/finish", (route) => {
    finished = true;
    return route.fulfill({ json: { handoff, workshop_state: "COMPLETED", conversation_phase: "HANDOFF_READY" } });
  });
  await page.goto("/");
  await page.getByRole("button", { name: /Finish workshop/ }).click();
  await expect(page.getByText("Workshop formulation is frozen. The handoff summary remains live.")).toBeVisible();
  await expect(page.getByRole("button", { name: /Finish workshop/ })).toBeDisabled();
  await expect(page.getByText(/READY · 66666666…6666 · v1/)).toBeVisible();
  await expect(page.getByText(/LATER · 66666666…6666/)).toBeVisible();
  await expect(page.getByRole("button", { name: "Confirm" })).toHaveCount(0);
  await page.getByLabel("Text fallback").fill("Summarize the committed handoff.");
  await expect(page.getByRole("button", { name: "Send" })).toBeEnabled();
  await expect(page.getByText(/Jira|GitHub/i)).toHaveCount(0);
});
