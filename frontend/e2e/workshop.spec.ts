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

const transcriptEventId = "77777777-7777-4777-8777-777777777777";
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
  preparation: {
    phase: "READY",
    message: "Your Spec Workshop is ready.",
    started_at: "2026-08-12T11:59:00Z",
    updated_at: "2026-08-12T12:00:00Z",
    ready_at: "2026-08-12T12:00:00Z",
    failure_code: null,
    delayed: false,
    delayed_message: null,
  },
  runway: {
    guidance_id: "88888888-8888-4888-8888-888888888888",
    depth: 6,
    questions: Array.from({ length: 6 }, (_, index) => ({
      question_id: `${index + 1}8888888-8888-4888-8888-888888888888`,
      question_version: 1,
      position: index + 1,
      exact_text: `Admitted clarification question ${index + 1}?`,
      reason: "Foundation admitted this independent question.",
    })),
    asked: [],
  },
  analyzer_jobs: [],
  completion: { state: "ACTIVE", completed_at: null },
  session: {
    workshop_state: "ACTIVE",
    conversation_phase: "WORKSHOP",
    call_state: "LISTENING",
    revision_locked: false,
    revision_lock_reason: null,
  },
  final_transcripts: [{
    event_id: transcriptEventId,
    turn_sequence: 1,
    version: 1,
    normalized_text: "I confirm the exact displayed decision and artifact review.",
    correction_of_version: null,
    speaker_actor_id: bootstrap.pm_actor_id,
  }],
  pending_proposal: null,
  governance: null,
  review_requests: [],
  handoff: null,
};

test("keeps Voice and microphone disabled until the exact admitted runway is READY", async ({ page }) => {
  await page.unroute("**/api/workshop");
  await page.route("**/api/workshop", (route) => route.fulfill({
    json: {
      ...workshop,
      session: { ...workshop.session, call_state: "PREPARING" },
      preparation: {
        ...workshop.preparation,
        phase: "VALIDATING_INITIAL_RUNWAY",
        message: "Validating the initial clarification runway…",
        ready_at: null,
        delayed: true,
        delayed_message: "SpecOps Analyzer is taking a little longer to formulate your Workshop plan. Your documents are safe, and preparation is continuing.",
      },
      runway: { ...workshop.runway, depth: 3, questions: workshop.runway.questions.slice(0, 3) },
    },
  }));
  await page.goto("/");
  await expect(page.getByRole("button", { name: "Start Spec Workshop" })).toBeDisabled();
  await expect(page.getByText("Validating the initial clarification runway…")).toBeVisible();
  await expect(page.getByText("SpecOps Analyzer is taking a little longer to formulate your Workshop plan. Your documents are safe, and preparation is continuing.")).toBeVisible();
  await expect(page.getByLabel("3 of 4 admitted questions ready")).toBeVisible();
  await expect(page.getByLabel("Text fallback")).toBeDisabled();
  await expect(page.getByText(/%/)).toHaveCount(0);
});

const artifactReview = {
  confirmation_id: "12121212-1212-4212-8212-121212121212",
  view_id: "13131313-1313-4313-8313-131313131313",
  view_hash: `sha256:${"e".repeat(64)}`,
  confirmed: false,
  view: {
    view_type: "spec_package_view",
    mode: "review",
    header: { package_name: "Filtered Orders CSV", overall_readiness: "FORMULATING" },
    items: [{ id: "14141414-1414-4414-8414-141414141414", title: "Complete export", summary: "Every matching row is included." }],
    projection_integrity: { all_material_items_included: true },
  },
};

const decisionReview = {
  protocol_version: "1.0.0",
  view_type: "DECISION_BATCH_REVIEW",
  view_id: "99999999-9999-4999-8999-999999999999",
  view_hash: `sha256:${"d".repeat(64)}`,
  session_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
  based_on_case_revision: 12,
  derived_from_cluster_ids: [],
  items: ["A", "B"].map((handle, index) => ({
    review_item_id: `${index + 1}bbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb`,
    handle,
    pending_decision_id: `${index + 1}ccccccc-cccc-4ccc-8ccc-cccccccccccc`,
    pending_decision_version: 1,
    classification: "PRODUCT",
    exact_statement: handle === "A" ? "Use UTF-8 for every CSV export." : "Never truncate a large export.",
    rationale: "Consumers need one stable contract.",
    problem_origins: [{
      problem_id: `${index + 1}ddddddd-dddd-4ddd-8ddd-dddddddddddd`,
      problem_version: 1,
      problem_statement: "The export behavior was not confirmed.",
      resolution_kind: "FULL",
      evidence_summary: "The technical source supplies the constraint.",
    }],
  })),
  generated_at: "2026-08-12T12:00:00Z",
};

test.beforeEach(async ({ page }) => {
  await page.route("**/api/bootstrap", (route) => route.fulfill({ json: bootstrap }));
  await page.route("**/api/workshop", (route) => route.fulfill({ json: workshop }));
  await page.route("**/api/v4/cases/*/decision-review", (route) => route.fulfill({ json: null }));
  await page.route("**/api/v4/cases/*/artifact-review", (route) => route.fulfill({ json: null }));
});

test("renders the immutable artifact projection and real V4 pipeline controls", async ({ page }) => {
  await page.unroute("**/api/v4/cases/*/artifact-review");
  await page.route("**/api/v4/cases/*/artifact-review", (route) => route.fulfill({ json: artifactReview }));
  await page.goto("/");
  const review = page.getByRole("region", { name: "Filtered Orders CSV" });
  await expect(review).toContainText("Complete projection");
  await expect(review).toContainText("Complete export");
  await expect(review).toContainText("AWAITING HUMAN CONFIRMATION");
  await expect(page.getByRole("region", { name: "Artifact pipeline" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Synthesize + audit" })).toBeEnabled();
  await expect(page.getByRole("button", { name: "Open exact review" })).toBeEnabled();
  await expect(page.getByRole("button", { name: "Confirm exact artifact" })).toBeEnabled();
});

test("partial browser decisions use the current Foundation view and leave omissions pending", async ({ page }) => {
  let body: Record<string, unknown> | null = null;
  await page.unroute("**/api/v4/cases/*/decision-review");
  await page.route("**/api/v4/cases/*/decision-review", (route) => route.fulfill({ json: decisionReview }));
  await page.route("**/api/v4/decisions/current/respond", async (route) => {
    body = route.request().postDataJSON();
    return route.fulfill({ json: { receipt_type: "DECISION_BATCH_RESPONSE" } });
  });
  await page.goto("/");
  await page.getByLabel("Action for A").selectOption("CONFIRM");
  await expect(page.getByLabel("Action for B")).toHaveValue("");
  await page.getByRole("button", { name: "Apply selected decisions" }).click();
  await expect(page.getByText("Selected decisions committed atomically; unselected decisions remain pending")).toBeVisible();
  expect(body).not.toBeNull();
  const request = body as { response_transcript_event_id: string; selections: Array<{ handle: string; action: string }>; actor_authentication: { actor_id: string } };
  expect(request.response_transcript_event_id).toBe(transcriptEventId);
  expect(request.actor_authentication.actor_id).toBe(bootstrap.pm_actor_id);
  expect(request.selections).toEqual([{ handle: "A", action: "CONFIRM", revision_span: null }]);
});

test("artifact confirmation sends the exact final transcript authentication binding", async ({ page }) => {
  let confirmation: Record<string, unknown> | null = null;
  await page.unroute("**/api/v4/cases/*/artifact-review");
  await page.route("**/api/v4/cases/*/artifact-review", (route) => route.fulfill({ json: artifactReview }));
  await page.route("**/api/v4/artifacts/current/confirm", async (route) => {
    confirmation = route.request().postDataJSON();
    return route.fulfill({ json: { receipt_type: "ARTIFACT_CONFIRMATION" } });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Confirm exact artifact" }).click();
  await expect(page.getByText("Exact artifact version confirmed")).toBeVisible();
  const request = confirmation as { confirmation_transcript_event_id: string; actor_authentication: { actor_id: string; assertion_transcript_event_id: string } };
  expect(request.confirmation_transcript_event_id).toBe(transcriptEventId);
  expect(request.actor_authentication).toMatchObject({
    actor_id: bootstrap.pm_actor_id,
    assertion_transcript_event_id: transcriptEventId,
  });
});

test("Finish workshop uses the durable V4 endpoint and locks further input", async ({ page }) => {
  let requestBody: { operation_key: string } | null = null;
  let completed = false;
  await page.unroute("**/api/workshop");
  await page.route("**/api/workshop", (route) => route.fulfill({
    json: completed ? {
      ...workshop,
      completion: { state: "CLEANUP_PENDING", completed_at: "2026-08-12T12:01:00Z" },
      session: {
        ...workshop.session,
        workshop_state: "CLEANUP_PENDING",
        conversation_phase: "COMPLETE",
        call_state: "ENDED",
        revision_locked: true,
        revision_lock_reason: "The PM completed the Spec Workshop.",
      },
    } : workshop,
  }));
  await page.route("**/api/v4/workshop/complete", async (route) => {
    requestBody = route.request().postDataJSON();
    completed = true;
    return route.fulfill({ json: { state: "CLEANUP_PENDING", replayed: false } });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Finish workshop" }).click();
  await expect(page.getByText("Spec Workshop completed. Finishing committed analysis and provider cleanup.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Finish workshop" })).toBeDisabled();
  await expect(page.getByLabel("Text fallback")).toBeDisabled();
  expect(requestBody?.operation_key).toMatch(/^browser-finish-/);
});

test("preserves the governed three-panel reading order and narrow layout", async ({ page }, testInfo) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "CSV Export Workshop" })).toBeVisible();
  await expect(page.getByText("Technical authority delegated")).toBeVisible();
  const order = await page.locator("main > section").evaluateAll((sections) => sections.map((section) => section.getAttribute("aria-labelledby")));
  expect(order).toEqual(["voice-title", "source-title", "package-title"]);
  if (testInfo.project.name === "narrow") {
    expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBe(0);
    await expect(page.getByLabel("Text fallback")).toBeVisible();
    await expect(page.getByRole("button", { name: "Synthesize + audit" })).toBeVisible();
  } else {
    const metrics = await page.locator("main").evaluate((main) => ({
      columns: getComputedStyle(main).gridTemplateColumns.split(" ").map((value) => Number.parseFloat(value)),
      heights: Array.from(main.children).map((child) => getComputedStyle(child).height),
      overflow: Array.from(main.children).map((child) => getComputedStyle(child).overflowY),
    }));
    expect(metrics.columns[0] / metrics.columns.reduce((a, b) => a + b, 0)).toBeCloseTo(.32, 1);
    expect(metrics.columns[1] / metrics.columns.reduce((a, b) => a + b, 0)).toBeCloseTo(.42, 1);
    expect(metrics.heights).toEqual(["900px", "900px", "900px"]);
    expect(metrics.overflow).toEqual(["auto", "auto", "auto"]);
  }
});
