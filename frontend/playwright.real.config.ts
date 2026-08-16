import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e-real",
  reporter: "line",
  use: {
    baseURL: "http://127.0.0.1:8018",
    trace: "retain-on-failure",
    ...devices["Desktop Chrome"],
  },
  webServer: {
    command: "PYTHONPATH=../src ${SPECOPS_PYTHON:-../../../../backend/.venv/bin/python} ../tests/workshop/task28_browser_server.py",
    url: "http://127.0.0.1:8018/api/bootstrap",
    reuseExistingServer: false,
  },
});
