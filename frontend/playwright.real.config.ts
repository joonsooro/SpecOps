import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e-real",
  reporter: "line",
  use: {
    baseURL: "http://127.0.0.1:8000",
    trace: "retain-on-failure",
    ...devices["Desktop Chrome"],
  },
  webServer: {
    command: "PYTHONPATH=../src ${SPECOPS_PYTHON:-../.venv/bin/python} ../tests/workshop/task26_browser_server.py",
    url: "http://127.0.0.1:8000/api/bootstrap",
    reuseExistingServer: false,
  },
});
