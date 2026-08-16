import { expect, test } from "@playwright/test";

const sourceLines = Array.from({ length: 46 }, (_, index) => [index + 1, index === 17
  ? "Large exports MUST be generated asynchronously without truncating filtered rows."
  : index === 42 ? "All output is UTF-8 with a single header row." : ""]);

const bootstrap = {
  case_id: "33333333-3333-4333-8333-333333333333",
  pm_actor_id: "22222222-2222-4222-8222-222222222222",
  delegation_valid_from: "2026-07-23T00:00:00Z",
  delegation_valid_until: "2026-08-22T23:59:59Z",
  delegation_command_scope: ["revise_spec_package", "approve_spec_package_item"],
  technical_source_lines: sourceLines,
};

const question = {
  question_id: "18888888-8888-4888-8888-888888888888",
  question_version: 2,
  exact_text: "Which timezone defines the filtered export date boundary?",
  reason: "The Foundation-admitted data boundary remains open.",
  dependencies: [{ dependency_kind: "SOURCE_SET", entity_id: null, expected_version: null, source_set_hash: `sha256:${"a".repeat(64)}` }],
};

const priorQuestion = {
  ...question,
  question_id: "28888888-8888-4888-8888-888888888888",
  question_version: 1,
  exact_text: "Which encoding is required for the exported CSV?",
};

const response = {
  response_id: "77777777-7777-4777-8777-777777777777",
  session_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
  question_id: priorQuestion.question_id,
  question_version: 1,
  turn_sequence: 1,
  response_version: 1,
  normalized_text: "Use UTF-8 with one header row.",
  content_hash: "b".repeat(64),
  final_source_ref: { artifact_id: "66666666-6666-4666-8666-666666666666", version: 1, content_hash: "b".repeat(64), location: { kind: "JSON_POINTER", pointer: "/normalized_text" } },
  client_submission_id: "55555555-5555-4555-8555-555555555555",
  correction_of_response_id: null,
  input_channel: "CHAT",
  channel_confirmation_receipt_id: null,
  created_at: "2026-08-16T12:00:00Z",
};

const binding = {
  proposal_ref: "99999999-9999-4999-8999-999999999999",
  proposal_version: 1,
  base_case_revision: 12,
  payload_hash: "d".repeat(64),
};

const proposal = {
  binding,
  status: "PENDING",
  view: {
    view_id: binding.proposal_ref,
    items: [{
      handle: "A",
      classification: "PRODUCT",
      exact_statement: "Use the organization timezone for date boundaries.",
      rationale: "The PM must choose one deterministic date interpretation.",
    }],
  },
};

const workshop = {
  session_id: response.session_id,
  case_revision: 12,
  workshop_state: "ACTIVE",
  conversation_phase: "WORKSHOP",
  session_card: { readiness: "NEEDS_CLARIFICATION", review_obligation: "DECISION_REQUIRED" },
  committed_turns: [{ question: priorQuestion, response }],
  question_runway: { questions: [question], runway_depth: 1 },
  turn_submission_status: "READY",
  proposal_statuses: [proposal],
  completion_status: null,
  generated_at: "2026-08-16T12:00:00Z",
};

test.beforeEach(async ({ page }) => {
  await page.route("**/api/bootstrap", (route) => route.fulfill({ json: bootstrap }));
  await page.route("**/api/workshop/preparation", (route) => route.fulfill({ json: { phase: "READY", message: null, delayed_message: null } }));
  await page.route("**/api/workshop", (route) => route.fulfill({ json: workshop }));
  await page.route("**/api/telemetry/spans", (route) => route.fulfill({ json: {} }));
});

test("renders canonical question messages and submits the exact typed response intent", async ({ page }) => {
  let body: Record<string, unknown> | null = null;
  await page.route("**/api/workshop/responses", async (route) => {
    body = route.request().postDataJSON();
    return route.fulfill({ json: { recovery_code: "ANALYSIS_QUEUED", snapshot: response } });
  });
  await page.goto("/");
  await expect(page.getByText(question.exact_text)).toBeVisible();
  await expect(page.getByText(priorQuestion.exact_text)).toBeVisible();
  await expect(page.getByText("PM · CHAT")).toBeVisible();
  await page.getByLabel("Your typed response").fill("Use the organization timezone.");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByText("Your response is saved. Analysis will resume automatically.")).toBeVisible();
  expect(body).toMatchObject({
    question_id: question.question_id,
    expected_question_version: question.question_version,
    text: "Use the organization timezone.",
    correction_of_response_id: null,
    edit_target: null,
  });
  expect(body).not.toHaveProperty("input_channel");
  expect(body).not.toHaveProperty("channel_confirmation_receipt_id");
});

test("correction control loads the exact latest response binding and text", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Correct response" }).click();
  await expect(page.getByLabel("Correct committed response")).toHaveValue(response.normalized_text);
  await expect(page.getByRole("button", { name: "Save correction" })).toBeEnabled();
});

test("keeps the next draft local while prior turn analysis is pending", async ({ page }) => {
  const pendingWorkshop = { ...workshop, turn_submission_status: "ANALYSIS_PENDING" };
  await page.unroute("**/api/workshop");
  await page.route("**/api/workshop", (route) => route.fulfill({ json: pendingWorkshop }));
  let responseCalls = 0;
  await page.route("**/api/workshop/responses", (route) => {
    responseCalls += 1;
    return route.fulfill({ status: 409, json: { detail: { code: "TURN_ANALYSIS_IN_PROGRESS" } } });
  });

  await page.goto("/");
  const composer = page.getByLabel("Your typed response");
  await composer.fill("Keep this draft local until analysis finishes.");
  await expect(composer).toHaveValue("Keep this draft local until analysis finishes.");
  await expect(page.getByRole("button", { name: "Analyzing previous response…" })).toBeDisabled();
  await expect(page.getByText("Your previous response is saved. Analysis must finish before you send another.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Correct response" })).toBeDisabled();
  expect(responseCalls).toBe(0);
});

test("keeps the Workshop usable while zero-runway analysis looks for ambiguities", async ({ page }) => {
  const zeroRunwayWorkshop = {
    ...workshop,
    question_runway: { questions: [], runway_depth: 0 },
    turn_submission_status: "ANALYSIS_PENDING",
    proposal_statuses: [],
  };
  await page.unroute("**/api/workshop");
  await page.route("**/api/workshop", (route) => route.fulfill({ json: zeroRunwayWorkshop }));

  await page.goto("/");
  await expect(page.getByText("No questions are available right now. The analyzer is checking for new ambiguities. This may take a moment.")).toBeVisible();
  await expect(page.getByText(question.exact_text)).not.toBeVisible();
  await expect(page.getByRole("button", { name: "Play exact question" })).not.toBeVisible();
  await expect(page.getByLabel("Your typed response")).toBeDisabled();
  await expect(page.getByRole("button", { name: "Finish Workshop" })).toBeEnabled();
});

test("playback is user-triggered, exact-text, and does not open a live input socket", async ({ page }) => {
  let playback: Record<string, unknown> | null = null;
  const sockets: string[] = [];
  page.on("websocket", (socket) => sockets.push(socket.url()));
  await page.route("**/api/workshop/playback", async (route) => {
    playback = route.request().postDataJSON();
    return route.fulfill({ json: { accepted: true, exact_text: question.exact_text } });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Play exact question" }).click();
  expect(playback).toEqual({
    question_id: question.question_id,
    question_version: question.question_version,
    exact_text: question.exact_text,
  });
  expect(sockets.some((url) => url.endsWith("/ws/live"))).toBe(false);
  await expect(page.getByText("Playback has no input or control authority.")).toBeVisible();
});

test("Confirm, Edit, and Reject use only their explicit proposal endpoints", async ({ page }) => {
  const calls: { url: string; body: Record<string, unknown> }[] = [];
  await page.route("**/api/workshop/proposals/*/*", async (route) => {
    calls.push({ url: route.request().url(), body: route.request().postDataJSON() });
    return route.fulfill({ json: { status: route.request().url().endsWith("/confirm") ? "COMMITTED" : "EDIT_REQUESTED" } });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Confirm" }).click();
  expect(calls[0].url).toContain(`/api/workshop/proposals/${binding.proposal_ref}/confirm`);
  expect(calls[0].body.binding).toEqual(binding);

  await page.getByRole("button", { name: "Edit" }).click();
  expect(calls[1].url).toContain(`/api/workshop/proposals/${binding.proposal_ref}/edit`);
  await expect(page.getByText(/Editing proposal/)).toBeVisible();

  await page.getByRole("button", { name: "Reject" }).click();
  expect(calls[2].url).toContain(`/api/workshop/proposals/${binding.proposal_ref}/reject`);
});

test("Finish Workshop is a separate deliberate control with its exact intent", async ({ page }) => {
  let body: Record<string, unknown> | null = null;
  const noProposal = { ...workshop, proposal_statuses: [] };
  await page.unroute("**/api/workshop");
  await page.route("**/api/workshop", (route) => route.fulfill({ json: noProposal }));
  await page.route("**/api/workshop/finish", async (route) => {
    body = route.request().postDataJSON();
    return route.fulfill({ json: { state: "FINISHING_ANALYSIS", replayed: false } });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Finish Workshop" }).click();
  expect(body).toMatchObject({ expected_case_revision: 12 });
  expect(body).toHaveProperty("client_action_id");
  await expect(page.getByText("Workshop finish committed. Existing analysis is draining before handoff.")).toBeVisible();
});

test("preserves the governed three-panel reading order and narrow layout", async ({ page }, testInfo) => {
  await page.goto("/");
  const order = await page.locator("main > section").evaluateAll((sections) => sections.map((section) => section.getAttribute("aria-labelledby")));
  expect(order).toEqual(["chat-title", "source-title", "govern-title"]);
  await expect(page.getByText("Technical authority delegated")).toBeVisible();
  if (testInfo.project.name === "narrow") {
    expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBe(0);
    await expect(page.getByLabel("Your typed response")).toBeVisible();
  } else {
    const columns = await page.locator("main").evaluate((main) => getComputedStyle(main).gridTemplateColumns.split(" ").map(Number.parseFloat));
    const total = columns.reduce((sum, value) => sum + value, 0);
    expect(columns[0] / total).toBeCloseTo(.32, 1);
    expect(columns[1] / total).toBeCloseTo(.42, 1);
  }
});
