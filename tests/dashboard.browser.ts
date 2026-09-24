import { expect, test } from '@playwright/test';
import type { TranscriptWindow } from '../frontend/dashboard/src/api-types';
import type { Page, Session } from '../frontend/dashboard/src/api';

test('session list scrolls across pages and refreshes the loaded range without duplicates', async ({
  page,
  request,
}) => {
  const seed: Page<Session> = await (
    await request.get('/api/sessions?context=demo:transcript')
  ).json();
  let sessions = Array.from({ length: 120 }, (_, index) => ({
    ...seed.items[0]!,
    session_id: index === 0 ? 'fixture-thread' : `session-${index}`,
    title: `Session ${String(index).padStart(3, '0')}`,
  }));
  let fail = false;
  let pending: Promise<void> | undefined;
  await page.route('**/api/sessions?*', async (route) => {
    await pending;
    if (fail) return route.fulfill({ status: 503, json: { detail: 'Session list unavailable' } });
    const params = new URL(route.request().url()).searchParams;
    const offset = Number(params.get('offset'));
    const limit = Number(params.get('limit'));
    const matching = sessions.filter((item) => item.title.includes(params.get('q') ?? ''));
    await route.fulfill({
      json: {
        items: matching.slice(offset, offset + limit),
        total: matching.length,
        has_more: offset + limit < matching.length,
      },
    });
  });
  await page.goto('/dashboard?session=fixture-thread');
  const list = page.locator('.session-list');
  const rows = list.locator('.session-row');
  await expect(rows).toHaveCount(50);
  await page.getByRole('button', { name: '◉ Live', exact: true }).click();
  await expect(page.locator('.session-index .pagination')).toHaveCount(0);
  await list.evaluate((element) => {
    element.scrollTop = element.scrollHeight;
  });
  const position = await list.evaluate((element) => element.scrollTop);
  await expect(rows).toHaveCount(100);
  expect(Math.abs((await list.evaluate((element) => element.scrollTop)) - position)).toBeLessThan(
    3,
  );
  await expect(rows.first()).toContainText('Session 000');
  await expect(page).toHaveURL(/session=fixture-thread/);

  sessions = [{ ...sessions[0]!, session_id: 'new-session', title: 'New session' }, ...sessions];
  const sidebar = await page.locator('.session-index').elementHandle();
  const beforeRefresh = await list.evaluate((element) => element.scrollTop);
  let release!: () => void;
  pending = new Promise<void>((resolve) => {
    release = resolve;
  });
  try {
    await page.getByRole('button', { name: 'Refresh', exact: true }).click();
    await expect(list).toHaveAttribute('aria-busy', 'true');
    await expect(rows).toHaveCount(100);
    await expect(rows.first()).toContainText('Session 000');
    expect(await list.evaluate((element) => element.scrollTop)).toBe(beforeRefresh);
  } finally {
    release();
  }
  await expect(rows.first()).toContainText('New session');
  expect(
    await sidebar!.evaluate((element) => element === document.querySelector('.session-index')),
  ).toBe(true);
  await expect(rows).toHaveCount(100);
  expect(new Set(await rows.locator('strong').allTextContents()).size).toBe(100);
  await list.evaluate((element) => {
    element.scrollTop = element.scrollHeight;
  });
  await expect(rows).toHaveCount(121);
  await expect(rows.last()).toContainText('Session 119');

  const updated = {
    ...sessions.at(-1)!,
    created_at: '2026-09-01T00:00:00Z',
    updated_at: '2026-09-18T08:30:00Z',
  };
  sessions = [updated, ...sessions.slice(0, -1)];
  await page.getByRole('button', { name: 'Refresh', exact: true }).click();
  await expect(rows.first()).toContainText('Session 119');
  await expect(rows.first().locator('time')).toHaveAttribute('datetime', '2026-09-18T08:30:00.000Z');
  await expect(rows.first().locator('time')).toHaveAttribute('title', /^Updated:/);
  await expect(rows).toHaveCount(121);
  await expect(page).toHaveURL(/session=fixture-thread/);

  fail = true;
  await page.getByRole('button', { name: 'Refresh', exact: true }).click();
  await expect(page.locator('.session-index .error')).toContainText('Session list unavailable');
  await expect(rows).toHaveCount(121);
  fail = false;
  await page.getByRole('button', { name: 'Refresh', exact: true }).click();
  await expect(page.locator('.session-index .error')).toHaveCount(0);
  await page.getByLabel('Find session').fill('Session 11');
  await expect(rows).toHaveCount(10);
  expect(await list.evaluate((element) => element.scrollTop)).toBe(0);
  await page.getByLabel('Find session').clear();
  await expect(rows).toHaveCount(50);
});

test('changing session filters discards an older pending page', async ({ page, request }) => {
  const seed: Page<Session> = await (
    await request.get('/api/sessions?context=demo:transcript')
  ).json();
  const sessions = Array.from({ length: 60 }, (_, index) => ({
    ...seed.items[0]!,
    session_id: `session-${index}`,
    title: `Saved session ${index}`,
  }));
  let release!: () => void;
  const pending = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route('**/api/sessions?*', async (route) => {
    const params = new URL(route.request().url()).searchParams;
    const offset = Number(params.get('offset'));
    if (offset > 0) await pending;
    const matching = params.get('q') ? [{ ...sessions[0]!, title: 'Filtered session' }] : sessions;
    await route.fulfill({
      json: {
        items: matching.slice(offset, offset + 50),
        total: matching.length,
        has_more: offset + 50 < matching.length,
      },
    });
  });
  await page.goto('/dashboard?session=fixture-thread');
  await expect(page.locator('.session-row')).toHaveCount(50);
  const requested = page.waitForRequest(
    (request) => new URL(request.url()).searchParams.get('offset') === '50',
  );
  await page.locator('.session-list').evaluate((element) => {
    element.scrollTop = element.scrollHeight;
  });
  await requested;
  await page.getByLabel('Find session').fill('Filtered');
  await expect(page.locator('.session-row')).toHaveCount(1);
  release();
  await expect(page.locator('.session-row')).toHaveText(/Filtered session/);
  await expect(page.locator('.session-list')).toHaveAttribute('aria-busy', 'false');
});

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

test('the transcript follows new messages at the bottom and resumes after scrolling back', async ({
  page,
  request,
}) => {
  const seed: TranscriptWindow = await (
    await request.get('/api/sessions/fixture-thread/transcript')
  ).json();
  const message = seed.entries.find((entry) => entry.entry_id.startsWith('message-'))!;
  const messages = Array.from({ length: 8 }, (_, index) => ({
    ...message,
    entry_id: `live-${index}`,
    first_seq: String(index + 1),
    revision_seq: String(index + 1),
  }));
  await page.route('**/api/sessions/fixture-thread/transcript*', async (route) => {
    const after = new URL(route.request().url()).searchParams.get('after');
    await route.fulfill({
      json: after
        ? {
            session_id: seed.session_id,
            generation: seed.generation,
            generated_at: seed.generated_at,
            changes: messages.slice(Number(after)).map((entry) => ({
              seq: entry.first_seq,
              upserts: [entry],
            })),
            next_cursor: String(messages.length),
            has_more: false,
          }
        : {
            ...seed,
            entries: messages,
            before_cursor: null,
            after_window_cursor: null,
            has_older: false,
            has_newer: false,
            change_cursor: String(messages.length),
          },
    });
  });
  function appendMessage() {
    const entry = {
      ...message,
      entry_id: `live-${messages.length}`,
      first_seq: String(messages.length + 1),
      revision_seq: String(messages.length + 1),
    };
    messages.push(entry);
    return page.locator(`[data-entry-id="${entry.entry_id}"]`);
  }

  await page.goto('/dashboard?session=fixture-thread');
  const scroll = page.getByLabel('Session transcript', { exact: true });
  const following = page.getByRole('button', { name: 'Following latest', exact: true });
  const bottomGap = () =>
    scroll.evaluate((element) => element.scrollHeight - element.scrollTop - element.clientHeight);
  await expect(page.locator('[data-entry-id]')).toHaveCount(8);
  await expect.poll(bottomGap).toBeLessThan(2);
  const box = (await scroll.boundingBox())!;
  await scroll.click({ position: { x: box.width / 2, y: box.height - 20 } });
  await expect(following).toBeVisible();
  const first = appendMessage();
  await expect(first).toBeVisible();
  await expect.poll(bottomGap).toBeLessThan(2);

  await scroll.focus();
  await Promise.all([
    scroll.evaluate(
      (element) =>
        new Promise<void>((resolve) => {
          element.addEventListener('scrollend', () => resolve(), { once: true });
        }),
    ),
    page.keyboard.press('PageUp'),
  ]);
  await expect(page.getByRole('button', { name: 'Jump to latest ↓', exact: true })).toBeVisible();
  await expect.poll(bottomGap).toBeGreaterThan(100);
  const position = await scroll.evaluate((element) => element.scrollTop);
  const second = appendMessage();
  await expect(page.getByRole('button', { name: /updated entries/ })).toBeVisible();
  expect(Math.abs((await scroll.evaluate((element) => element.scrollTop)) - position)).toBeLessThan(
    3,
  );
  await expect(second).toHaveCount(1);
  await expect(second).not.toBeInViewport();

  await page.keyboard.press('Control+End');
  await expect(following).toBeVisible();
  await expect(second).toBeVisible();
  await expect(page.getByRole('button', { name: /updated entries/ })).toHaveCount(0);
  await expect.poll(bottomGap).toBeLessThan(2);
  const third = appendMessage();
  await expect(third).toBeVisible();
  await expect.poll(bottomGap).toBeLessThan(2);

  await third
    .locator('div.markdown p')
    .first()
    .evaluate((element) => {
      const range = document.createRange();
      range.selectNodeContents(element);
      document.getSelection()!.removeAllRanges();
      document.getSelection()!.addRange(range);
    });
  const selection = await page.evaluate(() => document.getSelection()!.toString());
  const selectedPosition = await scroll.evaluate((element) => element.scrollTop);
  const duringSelection = appendMessage();
  await expect(page.getByRole('button', { name: /updated entries/ })).toBeVisible();
  await expect(duringSelection).toHaveCount(1);
  expect(await page.evaluate(() => document.getSelection()!.toString())).toBe(selection);
  expect(await scroll.evaluate((element) => element.scrollTop)).toBe(selectedPosition);

  await page.evaluate(() => document.getSelection()!.removeAllRanges());
  await expect(duringSelection).toBeInViewport();
  await expect(page.getByRole('button', { name: /updated entries/ })).toHaveCount(0);
  const afterSelection = appendMessage();
  await expect(afterSelection).toBeInViewport();
  await expect(page.locator('[data-entry-id]')).toHaveCount(messages.length);
});

test('tool output retains its tail or reading position across revisions and completion', async ({
  page,
  request,
}) => {
  const base = '/api/sessions/fixture-thread';
  const seed: TranscriptWindow = await (await request.get(`${base}/transcript`)).json();
  const tool = seed.entries.find((entry) => entry.entry_id === 'long-tool')!;
  let revision = 1;
  let state = 'running';
  let text =
    Array.from({ length: 6000 }, (_, index) => `line ${index}: captured tool output\n`).join('') +
    'TAIL-1\n';
  const current = () => ({
    ...tool,
    state,
    revision_seq: String(revision),
    blocks: [
      {
        ...tool.blocks[0]!,
        content_ref: `output-${revision}`,
        preview: text.slice(0, 2048),
        preview_truncated: true,
        recorded_bytes: text.length,
      },
    ],
  });
  await page.route(`**${base}/transcript*`, async (route) => {
    const after = new URL(route.request().url()).searchParams.get('after');
    await route.fulfill({
      json: after
        ? {
            session_id: seed.session_id,
            generation: seed.generation,
            generated_at: seed.generated_at,
            changes:
              after === String(revision) ? [] : [{ seq: String(revision), upserts: [current()] }],
            next_cursor: String(revision),
            has_more: false,
          }
        : {
            ...seed,
            entries: [current()],
            has_older: false,
            has_newer: false,
            before_cursor: null,
            after_window_cursor: null,
            change_cursor: String(revision),
          },
    });
  });
  await page.route(`**${base}/content/output-*`, async (route) => {
    const url = new URL(route.request().url());
    const offset = url.searchParams.has('tail')
      ? Math.max(0, text.length - 65536)
      : Number(url.searchParams.get('offset'));
    const end = Math.min(offset + 65536, text.length);
    await route.fulfill({
      json: {
        text: text.slice(offset, end),
        offset,
        content_ref: url.pathname.split('/').at(-1),
        next_offset: end < text.length ? end : null,
        recorded_bytes: text.length,
      },
    });
  });
  await page.goto('/dashboard?session=fixture-thread');
  const entry = page.locator('[data-entry-id="long-tool"]');
  const output = entry.locator('.content-block > pre');
  const tail = entry.getByRole('button', { name: 'Tail', exact: true });
  const gap = () =>
    output.evaluate((element) => element.scrollHeight - element.scrollTop - element.clientHeight);
  await expect(output).toContainText('TAIL-1');
  await expect.poll(gap).toBeLessThan(2);

  await output.evaluate((element) => {
    element.scrollTop = 180;
  });
  await expect(tail).toHaveAttribute('aria-pressed', 'false');
  const reading = await output.innerText();
  text += 'TAIL-2\n';
  revision += 1;
  await page.waitForResponse((response) => response.url().includes('/content/output-2?offset='));
  await expect(output).toHaveText(reading);
  expect(await output.evaluate((element) => element.scrollTop)).toBe(180);

  await tail.click();
  await expect(output).toContainText('TAIL-2');
  await expect.poll(gap).toBeLessThan(2);
  text += 'TAIL-3\n';
  revision += 1;
  await expect(output).toContainText('TAIL-3');
  await expect.poll(gap).toBeLessThan(2);
  state = 'completed';
  revision += 1;
  text += 'TOOL-DONE\n';
  await expect(entry.locator('.status')).toContainText('completed');
  await expect(output).toContainText('TOOL-DONE');
  await expect.poll(gap).toBeLessThan(2);
});

test('live updates preserve the reading anchor; pause and explicit refresh are independent', async ({
  page,
  request,
}) => {
  await page.goto('/dashboard?context=demo%3Atranscript');
  const scroll = page.locator('.transcript-scroll');
  await expect(page.getByRole('heading', { name: 'Trace a running agent session' })).toBeVisible();
  await expect(page.locator('[data-entry-id]')).toHaveCount(50);
  await expect(page.locator('[data-entry-id="final"] div.markdown')).toContainText('MESSAGE-END');
  await scroll.hover();
  await Promise.all([
    scroll.evaluate(
      (element) =>
        new Promise<void>((resolve) => {
          element.addEventListener('scrollend', () => resolve(), { once: true });
        }),
    ),
    page.mouse.wheel(0, -600),
  ]);
  await expect(page.getByRole('button', { name: 'Jump to latest ↓', exact: true })).toBeVisible();
  const reading = page.locator('[data-entry-id="final"]');
  const before = await reading.evaluate((element) => element.getBoundingClientRect().top);
  await request.post('/test/append');
  await expect(page.getByRole('button', { name: /updated entries/ })).toBeVisible();
  expect(
    Math.abs((await reading.evaluate((element) => element.getBoundingClientRect().top)) - before),
  ).toBeLessThan(5);
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
  request,
}) => {
  const earlier: TranscriptWindow = await (
    await request.get('/api/sessions/fixture-thread/transcript?around=message-0')
  ).json();
  const ref = earlier.entries.find((entry) => entry.entry_id === 'message-0')!.blocks[0]!
    .content_ref;
  let fullReads = 0;
  page.on('request', (request) => {
    if (request.url().includes(`/content/${ref}?`)) fullReads += 1;
  });
  await page.goto('/dashboard?session=fixture-thread');
  await expect(page.locator('[data-entry-id]')).toHaveCount(50);
  await page.route('**/transcript?before=*', async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 250));
    await route.continue();
  });
  const scroll = page.locator('.transcript-scroll');
  await page.getByRole('button', { name: 'Following latest', exact: true }).click();
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
  await expect(page.locator('[data-entry-id="message-0"]')).not.toContainText('EARLIER-END');
  expect(fullReads).toBe(0);
  const offset = await scroll.evaluate(
    (element, id) =>
      element.querySelector(`[data-entry-id="${id}"]`)!.getBoundingClientRect().top -
      element.getBoundingClientRect().top,
    anchor.id,
  );
  expect(Math.abs(offset - anchor.offset)).toBeLessThan(3);
  await expect(page.locator('[data-entry-id="final"]')).toHaveCount(1);
  await page.locator('[data-entry-id="message-0"] .entry-heading').scrollIntoViewIfNeeded();
  await expect(page.locator('[data-entry-id="message-0"] div.markdown')).toContainText(
    'EARLIER-END',
  );
  expect(fullReads).toBeGreaterThan(0);
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

test('waiting parent links to child evidence and preserves task navigation', async ({ page }) => {
  await page.goto('/dashboard?view=tasks&task=fixture-review');
  await expect(page.getByRole('region', { name: 'Subtasks', exact: true })).toBeVisible();
  await expect(page.getByText('Waiting for result', { exact: true })).toBeVisible();
  await page.getByLabel('Task status').selectOption('waiting');
  await expect(page.locator('.task-list')).toContainText('Review with subtasks');
  const child = page
    .getByRole('region', { name: 'Subtasks', exact: true })
    .locator('.subtask-row')
    .filter({ hasText: 'completed' });
  await child.getByRole('button').click();
  await expect(page.getByRole('region', { name: 'Subtask evidence' })).toContainText(
    'Reference design verified',
  );
  const downloadPromise = page.waitForEvent('download');
  await page.getByRole('button', { name: 'reference.md', exact: true }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe('reference.md');
  await page.getByRole('button', { name: 'fixture-review', exact: true }).click();
  await expect(page.getByText('Waiting for result', { exact: true })).toBeVisible();
  await expect(page.getByLabel('Task status')).toHaveValue('waiting');
});
