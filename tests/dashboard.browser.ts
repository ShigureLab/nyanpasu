import { expect, test } from '@playwright/test';
import type { TranscriptWindow } from '../frontend/dashboard/src/api-types';

test('Claude native messages, tools, edits, search and live results share the dashboard', async ({
  page,
  request,
}) => {
  await page.goto('/dashboard?context=demo%3Aclaude');
  await expect(
    page.getByRole('heading', { name: 'Claude result', exact: true }),
  ).toBeVisible();
  await expect(page.locator('.session-index')).toContainText('Claude Code');
  await page.getByText('Session details', { exact: false }).first().click();
  await expect(page.locator('.session-metadata')).toContainText(
    'claude-test-model',
  );
  await expect(page.locator('.session-metadata')).toContainText('Claude Code');
  await expect(page.locator('.session-metadata')).not.toContainText('Codex');
  await expect(page.locator('[data-entry-id="claude-bash"]')).toContainText(
    'failed',
  );
  await expect(
    page.locator('[data-entry-id="claude-edit-call"]'),
  ).toContainText('verified explanation');
  await expect(
    page.getByText('Check the evidence before editing.', { exact: true }),
  ).toBeVisible();
  await page
    .getByRole('textbox', { name: 'Search complete session' })
    .fill('CLAUDE-NEEDLE');
  await page.getByRole('button', { name: 'Search', exact: true }).click();
  await page.locator('.search-results button').first().click();
  await expect(page.locator('.search-focus')).toContainText('CLAUDE-NEEDLE');
  await expect(page).toHaveURL(/entry=claude-bash/);
  await page.reload();
  await expect(page.locator('.search-focus')).toContainText('CLAUDE-NEEDLE');
  await page.keyboard.press('Escape');
  const session = new URL(page.url()).searchParams.get('session');
  const exported = await request.get(`/api/sessions/${session}/export`);
  expect(exported.status()).toBe(200);
  expect(await exported.text()).toContain('verified explanation');
  await request.post('/test/claude-append');
  await page.getByRole('button', { name: 'Refresh', exact: true }).click();
  await expect(
    page.locator('[data-entry-id="claude-live-tool"]'),
  ).toContainText('CLAUDE-LIVE-DONE');
  await expect(
    page.locator('[data-entry-id="claude-live-tool"]'),
  ).toContainText('completed');
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
});

test('messages render and copy in full; tool previews contain only consecutive source text', async ({
  page,
  context,
  request,
}) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write']);
  await page.goto('/dashboard?context=demo%3Atranscript');
  await expect(page.getByRole('heading', { name: 'Full message', exact: true })).toBeVisible();
  const session = new URL(page.url()).searchParams.get('session');
  const base = `/api/sessions/${session}`;
  const transcript: TranscriptWindow = await (await request.get(`${base}/transcript`)).json();
  const entry = transcript.entries.find((entry) => entry.phase === 'final_answer')!;
  const source = await request.get(`${base}/content/${entry.blocks[0]!.content_ref}?download=true`);
  const message = page.locator(`[data-entry-id="${entry.entry_id}"]`);
  await expect(message.locator('div.markdown')).toContainText('MESSAGE-END');
  await expect(message.getByRole('button', { name: 'Expand', exact: true })).toHaveCount(0);
  await message.getByRole('button', { name: 'Copy source', exact: true }).click();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(await source.text());

  const tool = transcript.entries.find((entry) => entry.command === 'cat captured-output.log')!;
  const output = page.locator(
    `[data-entry-id="${tool.entry_id}"] .content-block[aria-label="output"]`,
  );
  const full = await (
    await request.get(`${base}/content/${tool.blocks[0]!.content_ref}?download=true`)
  ).text();
  const preview = await output.locator('pre').innerText();
  expect(full.startsWith(preview)).toBe(true);
  await output.getByRole('button', { name: 'Expand', exact: true }).click();
  await expect
    .poll(async () => (await output.locator('pre').innerText()).length)
    .toBeGreaterThan(preview.length);
  const expanded = await output.locator('pre').innerText();
  expect(full.startsWith(expanded)).toBe(true);
  await output.getByRole('button', { name: 'Copy displayed text', exact: true }).click();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(expanded);
});

test('live updates preserve the reading anchor; pause and explicit refresh are independent', async ({
  page,
  request,
}) => {
  await page.goto('/dashboard?context=demo%3Atranscript');
  const scroll = page.locator('.transcript-scroll');
  await expect(page.getByRole('heading', { name: 'Trace a running agent session' })).toBeVisible();
  await expect(page.locator('[data-entry-id]')).toHaveCount(50);
  await scroll.evaluate((element) => {
    element.scrollTop = 350;
  });
  await scroll.hover();
  await page.mouse.wheel(0, -120);
  await expect(page.getByRole('button', { name: 'Jump to latest ↓', exact: true })).toBeVisible();
  const before = await scroll.evaluate((element) => element.scrollTop);
  await request.post('/test/append');
  await expect(page.getByRole('button', { name: /updated entries/ })).toBeVisible();
  expect(Math.abs((await scroll.evaluate((element) => element.scrollTop)) - before)).toBeLessThan(
    5,
  );
  await page.getByRole('button', { name: '◉ Live', exact: true }).click();
  let calls = 0;
  page.on('request', (request) => {
    if (request.url().includes('/transcript?after=')) calls += 1;
  });
  await request.post('/test/append');
  await page.waitForTimeout(1300);
  expect(calls).toBe(0);
  await page.getByRole('button', { name: 'Refresh', exact: true }).click();
  await expect.poll(() => calls).toBeGreaterThan(0);
  await expect(page.getByRole('button', { name: 'Ⅱ Paused', exact: true })).toBeVisible();
});

test('full-content search, entry deep link and timestamps remain readable', async ({ page }) => {
  await page.goto('/dashboard?context=demo%3Atranscript');
  await page.getByRole('textbox', { name: 'Search complete session' }).fill('SEARCH-NEEDLE');
  await page.getByRole('button', { name: 'Search', exact: true }).click();
  await page.locator('.search-results button').first().click();
  await expect(page).toHaveURL(/entry=long-tool/);
  await expect(page.locator('.search-focus')).toContainText('SEARCH-NEEDLE');
  const url = page.url();
  await page.reload();
  await expect(page.locator('.search-focus')).toContainText('SEARCH-NEEDLE');
  expect(page.url()).toBe(url);
  await expect(page.getByRole('button', { name: /Source items|Original Codex item/ })).toHaveCount(
    0,
  );
  await expect(page.getByRole('dialog', { name: 'Entry details' })).toContainText('Started');
  await expect(page.locator('[data-entry-id] time').first()).toHaveAttribute(
    'datetime',
    /2026-09-14/,
  );
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog', { name: 'Entry details' })).toHaveCount(0);
  await page.getByLabel('Theme', { exact: true }).selectOption('dark');
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole('button', { name: 'Sessions ☰' }).click();
  await expect(page.locator('.session-index')).toBeVisible();
});

test('earlier history is prepended and asynchronous message expansion preserves the reading anchor', async ({
  page,
}) => {
  await page.goto('/dashboard?session=fixture-thread');
  await expect(page.locator('[data-entry-id]')).toHaveCount(50);
  await page.route('**/transcript?before=*', async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 250));
    await route.continue();
  });
  const scroll = page.locator('.transcript-scroll');
  await scroll.evaluate((element) => {
    element.scrollTop = 140;
  });
  await scroll.hover();
  const requested = page.waitForRequest((request) => request.url().includes('/transcript?before='));
  await page.mouse.wheel(0, -100);
  await requested;
  const anchor = await scroll.evaluate((element) => {
    const top = element.getBoundingClientRect().top;
    const entry = [...element.querySelectorAll<HTMLElement>('[data-entry-id]')].find(
      (item) => item.getBoundingClientRect().bottom > top,
    )!;
    return { id: entry.dataset.entryId!, offset: entry.getBoundingClientRect().top - top };
  });
  await expect(page.locator('[data-entry-id]')).toHaveCount(79);
  await expect(page.locator('[data-entry-id="message-0"] div.markdown')).toContainText(
    'EARLIER-END',
  );
  const offset = await scroll.evaluate(
    (element, id) =>
      element.querySelector(`[data-entry-id="${id}"]`)!.getBoundingClientRect().top -
      element.getBoundingClientRect().top,
    anchor.id,
  );
  expect(Math.abs(offset - anchor.offset)).toBeLessThan(3);
  await expect(page.locator('[data-entry-id="final"]')).toHaveCount(1);
});

test('later history appends to a deep-linked window without losing earlier entries', async ({
  page,
}) => {
  await page.goto('/dashboard?session=fixture-thread&entry=input');
  await expect(page.locator('[data-entry-id]')).toHaveCount(25);
  await page.route('**/transcript?after_window=*', async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 200));
    await route.continue();
  });
  const scroll = page.locator('.transcript-scroll');
  const requested = page.waitForRequest((request) =>
    request.url().includes('/transcript?after_window='),
  );
  await scroll.evaluate((element) => {
    element.scrollTop = element.scrollHeight;
  });
  await requested;
  const anchor = await scroll.evaluate((element) => {
    const top = element.getBoundingClientRect().top;
    const entry = [...element.querySelectorAll<HTMLElement>('[data-entry-id]')].find(
      (item) => item.getBoundingClientRect().bottom > top,
    )!;
    return { id: entry.dataset.entryId!, offset: entry.getBoundingClientRect().top - top };
  });
  await expect(page.locator('[data-entry-id]')).toHaveCount(75);
  await expect(page.locator('[data-entry-id="input"]')).toHaveCount(1);
  const offset = await scroll.evaluate(
    (element, id) =>
      element.querySelector(`[data-entry-id="${id}"]`)!.getBoundingClientRect().top -
      element.getBoundingClientRect().top,
    anchor.id,
  );
  expect(Math.abs(offset - anchor.offset)).toBeLessThan(3);
});

test('session metadata, task dates and structured backend diagnostics are visible', async ({
  page,
}) => {
  await page.goto('/dashboard?session=fixture-thread');
  await expect(page.locator('.session-metadata')).toContainText('Native session ID');
  await expect(page.locator('.session-metadata')).toContainText('fixture-thread');
  await expect(page.locator('.session-metadata')).toContainText('demo:transcript');
  await expect(page.locator('.session-metadata')).toContainText('test-model');
  await page.getByRole('button', { name: 'Tasks', exact: true }).click();
  await expect(page.locator('.task-times time').first()).toHaveAttribute('datetime', /^\d{4}-\d{2}-\d{2}T/);
  await page.getByRole('button', { name: 'Runtime', exact: true }).click();
  await expect(page.locator('.diagnostic')).toHaveCount(2);
  await expect(page.locator('.diagnostic').first()).toContainText(
    'Reconnecting after a network interruption',
  );
  await expect(page.locator('.diagnostic').first().locator('time')).toHaveAttribute(
    'datetime',
    '2026-09-14T00:00:00.000Z',
  );
  await page.getByLabel('Diagnostic level').selectOption('warn');
  await expect(page.locator('.diagnostic')).toHaveCount(1);
  await expect(page.locator('.diagnostic pre')).toHaveCount(0);
});
