import { defineConfig } from '@playwright/test';

// The e2e spec lives under this frontend project (tests/e2e) so it can resolve @playwright/test
// from frontend/node_modules -- a spec at the repo root cannot. `webServer` builds the UI's
// server: `chronotrace serve` mounts the built _ui at / and the API over ./recordings (relative
// to this frontend/ dir). reuseExistingServer lets a dev run against a server already up.
export default defineConfig({
  testDir: './tests/e2e',
  use: {
    baseURL: 'http://localhost:8000',
  },
  webServer: {
    command: 'python -m chronotrace.cli serve --dir ./recordings',
    url: 'http://localhost:8000',
    reuseExistingServer: !process.env.CI, // fresh server in CI; reuse a dev's running one locally
    timeout: 120_000,
  },
});
