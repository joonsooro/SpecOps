import { expect, test } from "@playwright/test";

test("real FastAPI and built frontend complete both deterministic V4 artifact seams offline", async ({ page }) => {
  const pageErrors: string[] = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "CSV Export Workshop" })).toBeVisible();
  await expect(page.getByText("Your Spec Workshop is ready.")).toBeVisible();
  await expect(page.getByText("No final turns yet. Start with the export’s success criteria.")).toBeVisible();
  const projection = await page.request.get("/api/workshop");
  expect(projection.ok()).toBe(true);
  const body = await projection.json();
  expect(body.protocol_version).toBe("1.0.0");
  expect(body.session.conversation_phase).toBe("WORKSHOP");
  expect(body.pending_proposal).toBeNull();
  expect(body.preparation.phase).toBe("READY");
  expect(body.runway.depth).toBe(4);

  await page.getByRole("button", { name: "Synthesize + audit" }).click();
  await expect(page.getByText("Spec Package synthesized and quality-audited")).toBeVisible();
  await page.getByRole("button", { name: "Open exact review" }).click();
  await expect(page.getByText("Exact Foundation review projection opened")).toBeVisible();
  await expect(page.getByText("Complete projection")).toBeVisible();

  await page.getByLabel("Text fallback").fill("I confirm the exact displayed Spec Package review.");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByText("Final PM evidence committed")).toBeVisible();
  await page.getByRole("button", { name: "Confirm exact artifact" }).click();
  await expect(page.getByText("Exact artifact version confirmed")).toBeVisible();

  const confirmed = await page.request.get(`/api/v4/cases/${body.case_id}/artifacts/SPEC_PACKAGE`);
  expect(confirmed.ok()).toBe(true);
  const artifact = await confirmed.json();
  expect(artifact.artifact_key).toMatch(/^SPEC-/);
  expect(artifact.payload_hash).toMatch(/^sha256:[a-f0-9]{64}$/);

  await page.getByLabel("Artifact type").selectOption("TECHNICAL_CONTRACT");
  await page.getByRole("button", { name: "Synthesize + audit" }).click();
  await expect(page.getByText("Technical Contract synthesized and quality-audited")).toBeVisible();
  await page.getByRole("button", { name: "Open exact review" }).click();
  await expect(page.getByText("Exact Foundation review projection opened")).toBeVisible();
  const technicalReview = await page.request.get(`/api/v4/cases/${body.case_id}/artifact-review`);
  expect(technicalReview.ok()).toBe(true);
  const technicalView = await technicalReview.json();
  expect(technicalView.view.source.confirmed_spec_artifact_id).toBe(artifact.artifact_id);
  expect(technicalView.view.source.confirmed_spec_record_revision).toBe(artifact.record_revision);

  await page.getByLabel("Text fallback").fill("I approve the exact displayed Technical Contract review.");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByText("Final PM evidence committed")).toBeVisible();
  await page.getByRole("button", { name: "Confirm exact artifact" }).click();
  await expect(page.getByText("Exact artifact version confirmed")).toBeVisible();
  const technical = await page.request.get(`/api/v4/cases/${body.case_id}/artifacts/TECHNICAL_CONTRACT`);
  expect(technical.ok()).toBe(true);
  expect((await technical.json()).artifact_key).toMatch(/^CONTRACT-/);
  expect(pageErrors).toEqual([]);
});
