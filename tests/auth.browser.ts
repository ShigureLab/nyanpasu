import { readFile } from 'node:fs/promises';
import { expect, test } from '@playwright/test';

const token = 'browser-test-token';

test('login, reload, authenticated downloads and sign out preserve the selected session', async ({
  page,
  request,
}) => {
  expect((await request.get('/api/sessions')).status()).toBe(401);
  await page.goto('/dashboard?session=fixture-thread');
  await expect(page.getByRole('heading', { name: 'Unlock your dashboard' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Open dashboard' })).toBeEnabled();
  await page.getByLabel('Access token').fill('wrong-token');
  await page.getByRole('button', { name: 'Open dashboard' }).click();
  await expect(page.getByRole('alert')).toContainText('valid access token');
  expect(await page.evaluate(() => localStorage.getItem('nyanpasu.dashboard.token'))).toBeNull();
  await page.getByLabel('Access token').fill(token);
  await page.getByRole('button', { name: 'Open dashboard' }).click();
  await expect(page.locator('.transcript-scroll')).toBeVisible();
  await expect(page).toHaveURL(/session=fixture-thread/);
  await page.reload();
  await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Full message', exact: true })).toBeVisible();
  const exporting = page.waitForEvent('download');
  await page.getByRole('button', { name: 'Markdown ↓', exact: true }).click();
  const exported = await exporting;
  expect(exported.suggestedFilename()).toBe('fixture-thread.md');
  expect(await readFile((await exported.path())!, 'utf8')).toContain('MESSAGE-END');
  const downloading = page.waitForEvent('download');
  await page.getByRole('button', { name: 'Download full content', exact: true }).last().click();
  expect((await downloading).suggestedFilename()).toMatch(/\.txt$/);
  await page.getByRole('button', { name: 'Sign out' }).click();
  await expect(page.getByRole('heading', { name: 'Unlock your dashboard' })).toBeVisible();
  await expect(page.locator('.transcript-scroll')).toHaveCount(0);
  expect(await page.evaluate(() => localStorage.getItem('nyanpasu.dashboard.token'))).toBeNull();
  await expect(page).toHaveURL(/session=fixture-thread/);
});

test('401 clears visible data and allows login again; logout propagates to another tab', async ({
  page,
  context,
}) => {
  await page.addInitScript(
    (value) => localStorage.setItem('nyanpasu.dashboard.token', value),
    token,
  );
  await page.goto('/dashboard?session=fixture-thread');
  await expect(page.locator('.transcript-scroll')).toBeVisible();
  await page.route('**/api/overview', (route) =>
    route.fulfill({ status: 401, json: { detail: 'Expired token' } }),
  );
  await page.getByRole('button', { name: 'Refresh', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Unlock your dashboard' })).toBeVisible();
  await expect(page.locator('.transcript-scroll')).toHaveCount(0);
  expect(await page.evaluate(() => localStorage.getItem('nyanpasu.dashboard.token'))).toBeNull();
  await page.unroute('**/api/overview');
  await page.getByLabel('Access token').fill(token);
  await page.getByRole('button', { name: 'Open dashboard' }).click();
  await expect(page.locator('.transcript-scroll')).toBeVisible();
  const other = await context.newPage();
  await other.goto('/dashboard');
  await expect(other.getByRole('button', { name: 'Sign out' })).toBeVisible();
  await other.getByRole('button', { name: 'Sign out' }).click();
  await expect(page.getByRole('heading', { name: 'Unlock your dashboard' })).toBeVisible();
  await expect(page.locator('.transcript-scroll')).toHaveCount(0);
});
