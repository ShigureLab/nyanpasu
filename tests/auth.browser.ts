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

test.describe('memory', () => {
  test.beforeEach(async ({ page }) => {
    await page.addInitScript(
      (value) => localStorage.setItem('nyanpasu.dashboard.token', value),
      token,
    );
    await page.goto('/dashboard?view=memory');
  });

  test('global sections show one detail and filter audiences on the server', async ({ page }) => {
    const sections = page.getByRole('navigation', { name: 'Memory sections' });
    const summaries = page.getByRole('region', { name: 'Context summaries' });
    const details = page.getByRole('article', { name: 'Context summary details' });
    const source = page.getByRole('article', { name: 'Source summary details' });
    await expect(summaries).toBeVisible();
    await expect(sections.getByRole('button', { name: 'Summaries', exact: true })).toHaveAttribute(
      'aria-pressed',
      'true',
    );
    await expect(page.getByRole('region', { name: 'Injected memory' })).toHaveCount(0);
    await expect(sections.getByRole('button', { name: 'Injections', exact: true })).toHaveCount(0);
    await expect(page.locator('.memory-summary-row')).toHaveCount(3);
    await expect(details).toHaveCount(1);
    await expect(page.getByLabel('Memory results')).toHaveCount(0);
    await expect(page.getByLabel('Memory topic')).toHaveCount(0);

    const filtered = page.waitForResponse((response) => {
      const url = new URL(response.url());
      return url.pathname === '/api/memory' && url.searchParams.get('domain') === 'private:other';
    });
    await page.getByLabel('Memory audience', { exact: true }).selectOption('private:other');
    const filteredResponse = await filtered;
    expect(filteredResponse.status()).toBe(200);
    const data = await filteredResponse.json();
    expect(data.items.map((item: { domain: string }) => item.domain)).toEqual(['private:other']);
    expect(data.summaries.map((item: { domain: string }) => item.domain)).toEqual([
      'private:other',
    ]);
    await expect(page.locator('.memory-summary-row')).toHaveCount(1);
    await page.locator('.memory-summary-row').click();
    await expect(details).toContainText('Other private context');
    await expect(details).not.toContainText('Private workspace context');

    await sections.getByRole('button', { name: 'Sources', exact: true }).click();
    await expect(sections.getByRole('button', { name: 'Sources', exact: true })).toHaveAttribute(
      'aria-pressed',
      'true',
    );
    await expect(summaries).toHaveCount(0);
    await expect(page.locator('.memory-row')).toHaveCount(1);
    await expect(page.getByLabel('Memory results')).toContainText('Other private summary');
    await page.locator('.memory-row').click();
    await expect(source).toContainText('This source belongs to another private audience.');
    await page.getByLabel('Memory audience', { exact: true }).selectOption('');
    await expect(page.locator('.memory-row')).toHaveCount(4);
    await expect(page.getByLabel('Memory results')).toContainText('Private workspace summary');

    await sections.getByRole('button', { name: 'Summaries', exact: true }).click();
    await expect(source).toHaveCount(0);
    await expect(page.locator('.memory-summary-row')).toHaveCount(3);
    await page.locator('.memory-summary-row').filter({ hasText: 'Public' }).click();
    await details.getByRole('button', { name: 'Python source summary', exact: true }).click();
    await expect(summaries).toHaveCount(0);
    await expect(source).toContainText('Use pytest and shared fixtures.');
    await expect(page.locator('.memory-row[aria-pressed="true"]')).toContainText(
      'Python testing summary',
    );
    await expect(
      source.getByText('codex:fixture-thread:fixture-turn:pytest-result', { exact: true }),
    ).toBeHidden();
    await source.getByText(/^Evidence references \(\d+\)$/).click();
    await expect(
      source.getByText('codex:fixture-thread:fixture-turn:pytest-result', { exact: true }),
    ).toBeVisible();
    await source.getByText('Record details', { exact: true }).click();
    await expect(source.locator('code').filter({ hasText: /^[a-f0-9]{64}$/ })).toHaveCount(1);
    await expect(source).toContainText('Published');
    await expect(source).not.toContainText('Canonical key');
    await expect(source).not.toContainText('Merged from');
    await source.getByRole('button', { name: 'task:fixture-task', exact: true }).first().click();
    await expect(page).toHaveURL(/task=fixture-task/);
  });

  test('task injections disclose exact text while sources stay within task access', async ({
    page,
  }) => {
    await page.goto('/dashboard?view=tasks&task=fixture-task');
    await page.getByRole('button', { name: 'Inspect task memory', exact: false }).click();
    const sections = page.getByRole('navigation', { name: 'Memory sections' });
    const injected = page.getByRole('region', { name: 'Injected memory' });
    const summaries = page.getByRole('region', { name: 'Context summaries' });
    const source = page.getByRole('article', { name: 'Source summary details' });
    await expect(injected).toBeVisible();
    await expect(sections.getByRole('button', { name: 'Injections', exact: true })).toHaveAttribute(
      'aria-pressed',
      'true',
    );
    await expect(summaries).toHaveCount(0);
    await expect(page.getByLabel('Memory results')).toHaveCount(0);
    await expect(injected.locator('pre')).toBeHidden();
    await injected.getByText('Exact injected text', { exact: true }).click();
    await expect(injected.locator('pre')).toBeVisible();
    await expect(injected.locator('pre')).toContainText('Python source summary');
    await expect(injected.locator('pre')).toContainText('Private workspace context');
    await expect(injected).not.toContainText('Other private context');
    await injected.getByText('Selection decisions', { exact: true }).click();
    await expect(injected).toContainText('Current context');
    await injected.getByText('demo:transcript', { exact: true }).first().click();
    await expect(injected.getByText('current_context', { exact: true }).first()).toBeVisible();

    await sections.getByRole('button', { name: 'Summaries', exact: true }).click();
    await expect(injected).toHaveCount(0);
    await expect(page.locator('.memory-summary-row')).toHaveCount(2);
    await expect(
      page.getByLabel('Memory audience', { exact: true }).locator('option[value="private:other"]'),
    ).toHaveCount(0);
    await page.locator('.memory-summary-row').filter({ hasText: 'private:fixture' }).click();
    const details = page.getByRole('article', { name: 'Context summary details' });
    await expect(details).toContainText('demo:transcript');
    await expect(details).toContainText('generation 1');
    await expect(details).toContainText('Private workspace context');
    await expect(summaries).not.toContainText('Other private context');
    await details.getByRole('button', { name: 'read the authorized source', exact: true }).click();
    await expect(source).toContainText("fixture task's private audience");
    await expect(page.getByLabel('Memory results')).not.toContainText('Other private summary');
    await expect(
      page.getByLabel('Memory topic').locator('option[value="other-private-topic"]'),
    ).toHaveCount(0);
    await page.getByLabel('Memory topic').selectOption('private-topic');
    await expect(page.locator('.memory-row')).toHaveCount(1);

    await page.getByRole('button', { name: 'All memory', exact: true }).click();
    await expect(page.getByRole('region', { name: 'Injected memory' })).toHaveCount(0);
    await expect(page.locator('.memory-summary-row')).toHaveCount(3);
    await expect(source).toHaveCount(0);
    await sections.getByRole('button', { name: 'Sources', exact: true }).click();
    await expect(page.locator('.memory-row')).toHaveCount(4);
    await expect(page.getByLabel('Memory results')).toContainText('Other private summary');
    await expect(source).not.toContainText("fixture task's private audience");
  });

  test('session memory keeps older task evidence accessible after a later update', async ({
    page,
  }) => {
    await page.route(/\/api\/memory\/[a-f0-9]{32}(?:\?|$)/, async (route) => {
      const response = await route.fetch();
      const data = await response.json();
      await route.fulfill({
        response,
        json: {
          ...data,
          task_id: 'fixture-runtime',
          sources: [...data.sources, 'task:fixture-runtime'],
        },
      });
    });
    await page
      .getByRole('navigation', { name: 'Memory sections' })
      .getByRole('button', { name: 'Sources', exact: true })
      .click();
    await page.locator('.memory-row').filter({ hasText: 'Python testing summary' }).click();
    const source = page.getByRole('article', { name: 'Source summary details' });
    await expect(
      source.getByText('demo:transcript · generation 1', { exact: true }).first(),
    ).toBeVisible();
    await expect(source.locator('.memory-source-task')).toContainText('task:fixture-runtime');
    await source.getByText(/^Evidence references \(\d+\)$/).click();
    const references = source.locator('.memory-sources');
    await expect(
      references.getByRole('button', { name: 'task:fixture-runtime', exact: true }),
    ).toBeVisible();
    await references.getByRole('button', { name: 'task:fixture-task', exact: true }).click();
    await expect(page).toHaveURL(/task=fixture-task/);
    await expect(
      page.locator('.task-detail').getByText('fixture-task', { exact: true }),
    ).toBeVisible();

    await page.goBack();
    await page
      .getByRole('navigation', { name: 'Memory sections' })
      .getByRole('button', { name: 'Sources', exact: true })
      .click();
    await page.locator('.memory-row').filter({ hasText: 'Python testing summary' }).click();
    await source
      .locator('.memory-source-task')
      .getByRole('button', { name: 'task:fixture-runtime', exact: true })
      .click();
    await expect(page).toHaveURL(/task=fixture-runtime/);
    await expect(
      page.locator('.task-detail').getByText('fixture-runtime', { exact: true }),
    ).toBeVisible();
  });

  test('mobile search, section resets and maintenance links stay usable', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    const sections = page.getByRole('navigation', { name: 'Memory sections' });
    await page.route(
      /\/api\/memory\/[a-f0-9]{32}(?:\?|$)/,
      async (route) => {
        const response = await route.fetch();
        const data = await response.json();
        expect(data.domain).toBe('public');
        expect(data.title).toBe('Python testing summary');
        data.body +=
          '\n\n' +
          Array.from(
            { length: 40 },
            (_, index) =>
              `Supporting observation ${index + 1}: pytest evidence was checked against the saved fixture.`,
          ).join('\n\n');
        await new Promise((resolve) => setTimeout(resolve, 150));
        await route.fulfill({ response, json: data });
      },
      { times: 1 },
    );
    await page.locator('.memory-summary-row').filter({ hasText: 'Public' }).click();
    await page
      .getByRole('article', { name: 'Context summary details' })
      .getByRole('button', { name: 'Python source summary', exact: true })
      .click();
    const source = page.getByRole('article', { name: 'Source summary details' });
    const heading = source.getByRole('heading', { name: 'Python testing summary', exact: true });
    await expect
      .poll(async () => (await heading.boundingBox())?.y ?? Infinity)
      .toBeLessThan(page.viewportSize()!.height / 2);
    await expect(heading).toBeInViewport({ ratio: 1 });
    await expect(
      source.getByText('Use pytest and shared fixtures.', { exact: true }),
    ).toBeInViewport({ ratio: 1 });
    await expect(page.locator('.topbar')).toBeInViewport({ ratio: 1 });
    await sections.getByRole('button', { name: 'Summaries', exact: true }).click();

    await page.getByLabel('Search memory').fill('Other private context');
    await page.getByRole('button', { name: 'Search', exact: true }).click();
    await expect(page.locator('.memory-summary-row')).toHaveCount(1);
    await page.locator('.memory-summary-row').click();
    await expect(page.getByRole('article', { name: 'Context summary details' })).toContainText(
      'Other private context',
    );

    await sections.getByRole('button', { name: 'Sources', exact: true }).click();
    await expect(page.getByLabel('Search memory')).toHaveValue('');
    await expect(page.locator('.memory-row')).toHaveCount(4);
    await page.getByLabel('Memory topic').selectOption('python');
    await expect(page.locator('.memory-row')).toHaveCount(1);
    await sections.getByRole('button', { name: 'Summaries', exact: true }).click();
    await expect(page.getByLabel('Memory topic')).toHaveCount(0);
    await expect(page.locator('.memory-summary-row')).toHaveCount(3);
    await sections.getByRole('button', { name: 'Sources', exact: true }).click();
    await expect(page.getByLabel('Memory topic')).toHaveValue('');
    await expect(page.locator('.memory-row')).toHaveCount(4);
    await page.getByLabel('Search memory').fill('leases');
    await page.getByRole('button', { name: 'Search', exact: true }).click();
    await expect(page.locator('.memory-row')).toHaveCount(1);
    await page.locator('.memory-row').click();
    await expect(page.getByRole('article', { name: 'Source summary details' })).toContainText(
      'Check active task leases.',
    );
    await expect(source.getByText('Check active task leases.', { exact: true })).toBeInViewport({
      ratio: 1,
    });
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth),
    ).toBe(true);

    await page.getByText('Maintenance tasks', { exact: true }).click();
    await page.getByRole('button', { name: 'Extraction tasks', exact: true }).click();
    await expect(page.getByLabel('Task kind')).toHaveValue('memory_extraction');
    await expect(page.locator('.task-list .task-row')).toHaveCount(1);
    await page.getByRole('button', { name: 'Memory', exact: true }).click();
    await page.getByText('Maintenance tasks', { exact: true }).click();
    await page.getByRole('button', { name: 'Consolidation tasks', exact: true }).click();
    await expect(page.getByLabel('Task kind')).toHaveValue('memory_consolidation');
    await expect(page.locator('.task-list .task-row')).toHaveCount(1);
  });
});
