import { expect, test } from "@playwright/test";

test("real FastAPI and built frontend persist and reconstruct authoritative chat offline", async ({ page }) => {
  const pageErrors: string[] = [];
  const sockets: string[] = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  page.on("websocket", (socket) => sockets.push(socket.url()));
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "CSV Export Workshop" })).toBeVisible();
  await expect.poll(async () => (await (await page.request.get("/api/workshop/preparation")).json()).phase, { timeout: 10_000 }).toBe("READY");
  const initialResponse = await page.request.get("/api/workshop");
  expect(initialResponse.ok()).toBe(true);
  const initial = await initialResponse.json();
  expect(initial.workshop_state).toBe("ACTIVE");
  expect(initial.question_runway.runway_depth).toBe(4);
  expect(initial.committed_turns).toEqual([]);
  await expect(page.getByText(initial.question_runway.questions[0].exact_text)).toBeVisible({ timeout: 5_000 });

  await page.getByLabel("Your typed response").fill("Use the organization timezone for date filtering.");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByText("Your response is saved. Analysis will resume automatically.")).toBeVisible();
  await expect(page.getByText("Use the organization timezone for date filtering.")).toBeVisible();

  await page.reload();
  await expect(page.getByText("Use the organization timezone for date filtering.")).toBeVisible();
  await page.getByRole("button", { name: "Correct response" }).click();
  await page.getByLabel("Correct committed response").fill("Use the account organization timezone for date filtering.");
  await page.getByRole("button", { name: "Save correction" }).click();
  await expect(page.getByText("Use the account organization timezone for date filtering.")).toBeVisible();

  const reconstructed = await (await page.request.get("/api/workshop")).json();
  expect(reconstructed.committed_turns).toHaveLength(2);
  expect(reconstructed.committed_turns[0].response.response_version).toBe(1);
  expect(reconstructed.committed_turns[1].response.response_version).toBe(2);
  expect(reconstructed.committed_turns.every((turn: { response: { input_channel: string; channel_confirmation_receipt_id: null } }) =>
    turn.response.input_channel === "CHAT" && turn.response.channel_confirmation_receipt_id === null)).toBe(true);

  expect([404, 405]).toContain((await page.request.post("/api/session/final-turn", { data: {} })).status());
  expect([404, 405]).toContain((await page.request.post("/api/v4/voice/final-transcripts", { data: {} })).status());
  expect(sockets.some((url) => url.endsWith("/ws/live"))).toBe(false);
  expect(pageErrors).toEqual([]);
});
