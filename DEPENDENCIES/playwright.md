# Playwright and Chromium

Required for Dashboard browser interaction tests. Optional for running Nyanpasu or building Dashboard assets without browser tests.

## Prerequisites and installation

Install the [frontend](frontend.md) and [Python development](python-development.md) dependencies. `@playwright/test` comes from the repository's frontend manifest; install its matching browser and OS dependencies from the repository root:

```bash
pnpm exec playwright install --with-deps chromium
pnpm run test:browser
```

The browser test command builds the Dashboard and uses an isolated fixture server. OS dependency installation may require administrator access; supported platforms and alternatives are in the [official browser installation guide](https://playwright.dev/docs/browsers).
