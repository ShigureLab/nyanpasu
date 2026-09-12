import { expect, test } from '@playwright/test';
import type { TranscriptWindow } from '../frontend/dashboard/src/api-types';

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

test('full-content search, entry deep link and original event remain readable', async ({
  page,
}) => {
  await page.goto('/dashboard?context=demo%3Atranscript');
  await page.getByRole('textbox', { name: 'Search complete session' }).fill('SEARCH-NEEDLE');
  await page.getByRole('button', { name: 'Search', exact: true }).click();
  await page.locator('.search-results button').first().click();
  await expect(page).toHaveURL(/entry=e_/);
  await expect(page.locator('.search-focus')).toContainText('SEARCH-NEEDLE');
  const url = page.url();
  await page.reload();
  await expect(page.locator('.search-focus')).toContainText('SEARCH-NEEDLE');
  expect(page.url()).toBe(url);
  await page.getByRole('button', { name: /Original events/ }).click();
  await expect(page.locator('.event')).not.toHaveCount(0);
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog', { name: 'Entry details' })).toHaveCount(0);
  await page.getByLabel('Theme', { exact: true }).selectOption('dark');
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole('button', { name: 'Sessions ☰' }).click();
  await expect(page.locator('.session-index')).toBeVisible();
});
