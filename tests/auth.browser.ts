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
  await expect(page.getByRole('heading', { name: 'Full message', exact: true })).toBeVisible();
  await expect(page).toHaveURL(/session=fixture-thread/);
  let release!: () => void;
  const pending = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route('**/api/overview', async (route) => {
    await pending;
    await route.continue();
  });
  let sidebar;
  let header;
  try {
    await page.reload();
    await expect(page.locator('.session-index')).toBeVisible();
    sidebar = await page.locator('.session-index').elementHandle();
    header = await page.locator('.topbar').elementHandle();
    await expect(page.getByRole('heading', { name: 'Unlock your dashboard' })).toHaveCount(0);
    await expect(page.getByLabel('Access token')).toHaveCount(0);
    await expect(page.getByRole('heading', { name: 'Full message', exact: true })).toBeVisible();
    await page.getByRole('button', { name: '◉ Live', exact: true }).click();
    await page.getByLabel('Find session').fill('Trace');
  } finally {
    release();
  }
  await page.getByRole('button', { name: 'Refresh', exact: true }).click();
  await expect(page.locator('.service-state')).toContainText('Service available');
  expect(
    await sidebar!.evaluate((element) => element === document.querySelector('.session-index')),
  ).toBe(true);
  expect(await header!.evaluate((element) => element === document.querySelector('.topbar'))).toBe(
    true,
  );
  await expect(page.getByLabel('Find session')).toHaveValue('Trace');
  await expect(page.getByRole('button', { name: 'Ⅱ Paused', exact: true })).toBeVisible();
  await page.unroute('**/api/overview');
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
  await expect(page.getByRole('heading', { name: 'Full message', exact: true })).toBeVisible();
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
  await expect(page.getByRole('heading', { name: 'Full message', exact: true })).toBeVisible();
  const other = await context.newPage();
  await other.goto('/dashboard');
  await expect(other.getByRole('button', { name: 'Sign out' })).toBeVisible();
  await other.getByRole('button', { name: 'Sign out' }).click();
  await expect(page.getByRole('heading', { name: 'Unlock your dashboard' })).toBeVisible();
  await expect(page.locator('.transcript-scroll')).toHaveCount(0);
});

test('temporary API failures preserve the dashboard shell and saved token', async ({ page }) => {
  await page.addInitScript(
    (value) => localStorage.setItem('nyanpasu.dashboard.token', value),
    token,
  );
  await page.route('**/api/overview', (route) =>
    route.fulfill({ status: 503, json: { detail: 'Temporarily unavailable' } }),
  );
  await page.goto('/dashboard?session=fixture-thread');
  await expect(page.locator('.global-error')).toContainText('Temporarily unavailable');
  await expect(page.getByRole('heading', { name: 'Full message', exact: true })).toBeVisible();
  await expect(page.getByLabel('Access token')).toHaveCount(0);
  expect(await page.evaluate(() => localStorage.getItem('nyanpasu.dashboard.token'))).toBe(token);
  const sidebar = await page.locator('.session-index').elementHandle();
  await page.unroute('**/api/overview');
  await page.getByRole('button', { name: 'Refresh', exact: true }).click();
  await expect(page.locator('.global-error')).toHaveCount(0);
  await expect(page.locator('.service-state')).toContainText('Service available');
  expect(
    await sidebar!.evaluate((element) => element === document.querySelector('.session-index')),
  ).toBe(true);
});

test('memory search shows merge evidence and only task-authorized private knowledge', async ({
  page,
}) => {
  await page.goto('/dashboard?view=memory');
  await page.getByLabel('Access token').fill(token);
  await page.getByRole('button', { name: 'Open dashboard' }).click();
  await expect(page.getByLabel('Memory results')).not.toContainText('Private workspace guide');
  await expect(page.getByLabel('Memory topic').locator('option')).not.toContainText([
    'private-topic',
  ]);
  await page.getByLabel('Memory topic').selectOption('python');
  await expect(page.locator('.memory-row')).toHaveCount(1);
  await page.locator('.memory-row').click();
  const note = page.getByRole('article', { name: 'Memory details' });
  await expect(note).toContainText('repository:Relax');
  await expect(note).toContainText('Use pytest and shared fixtures.');
  await expect(note.getByRole('region', { name: 'Merged memories' }).locator('li')).toHaveCount(1);
  await expect(
    note
      .locator('dl > div')
      .filter({ has: page.getByText('Revision', { exact: true }) })
      .locator('code'),
  ).toHaveText(/^[a-f0-9]{64}$/);
  await note.getByRole('button', { name: 'task:fixture-task', exact: true }).click();
  await expect(page).toHaveURL(/task=fixture-task/);
  await page.getByRole('button', { name: 'Inspect task memory', exact: false }).click();
  await page.getByLabel('Memory topic').selectOption('private-topic');
  await expect(page.locator('.memory-row')).toHaveCount(1);
  await page.locator('.memory-row').click();
  await expect(note).toContainText("fixture task's private audience");
  await page.getByRole('button', { name: 'Public memory', exact: true }).click();
  await expect(page.locator('.memory-row')).toHaveCount(2);
  await expect(page.getByLabel('Memory results')).not.toContainText('Private workspace guide');
  await expect(note).not.toContainText("fixture task's private audience");
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByLabel('Search memory').fill('leases');
  await page.getByRole('button', { name: 'Search', exact: true }).click();
  await expect(page.locator('.memory-row')).toHaveCount(1);
  await page.locator('.memory-row').click();
  await expect(note).toContainText('Check active task leases.');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  );
});
