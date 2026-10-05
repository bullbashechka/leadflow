import { randomUUID } from 'node:crypto'
import { expect, test } from '@playwright/test'
import { enter, incomingLead, mockAccess, noOverflow, screenshot, waitForListResults } from './crmFixtures'
import type { Page } from '@playwright/test'

async function startMock(page: Page, context: Parameters<typeof mockAccess>[0], initial = [incomingLead(1)]) {
  const server = await mockAccess(context)
  server.leads = initial
  await page.clock.install()
  await page.goto('/')
  await enter(page, 'demo')
  await waitForListResults(page)
  return server
}

async function selectTag(page: Page, name: string) {
  await page.getByRole('combobox', { name: 'Направление' }).click()
  await page.getByText(name, { exact: true }).last().click()
}

test('auto-refresh: external arrivals appear without navigation or manual reload', async ({ page, context }) => {
  const server = await mockAccess(context)
  await page.clock.install()
  await page.goto('/')
  await enter(page, 'demo')
  await waitForListResults(page)
  server.leads.unshift(incomingLead(1))
  await page.clock.fastForward(5000)
  await expect(page.getByRole('button', { name: 'Открыть карточку: Тест автообновления 1', exact: true }).first()).toBeVisible()
  await expect(page.getByLabel('Всего заявок: 1', { exact: true })).toBeVisible()
  await noOverflow(page)
})

test('auto-refresh: preserves 150 loaded rows and scroll when 80 matching leads arrive', async ({ page, context }) => {
  test.setTimeout(60_000)
  const old = Array.from({ length: 150 }, (_, index) => incomingLead(150 - index))
  const server = await startMock(page, context, old)
  await page.getByRole('button', { name: 'Показать ещё', exact: true }).click()
  await expect(page.locator('[data-lead-id]:visible')).toHaveCount(100)
  await page.getByRole('button', { name: 'Показать ещё', exact: true }).click()
  await expect(page.locator('[data-lead-id]:visible')).toHaveCount(150)
  const row = page.locator(`[data-lead-id="${old[90].id}"]:visible`)
  await row.scrollIntoViewIfNeeded()
  const top = await row.evaluate((element) => element.getBoundingClientRect().top)
  const additions = Array.from({ length: 80 }, (_, index) => ({ ...incomingLead(230 - index),
    created_at: '2026-10-05T12:05:00.123456Z', source: index % 2 ? 'manual' : 'telegram_bot' }))
  server.leads = [...additions, ...old]
  await page.clock.fastForward(5000)
  const notice = page.getByRole('button', { name: 'Новые заявки: 80 ↑', exact: true })
  await expect(notice).toBeVisible()
  await expect(notice).toBeInViewport()
  await expect(page.locator('[data-lead-id]:visible')).toHaveCount(150)
  await expect.poll(async () => Math.abs(await row.evaluate((element) => element.getBoundingClientRect().top) - top)).toBeLessThan(2)
  await noOverflow(page)
  await screenshot(page, 'new-arrivals-while-reading', false)
  await notice.click()
  await expect(page.locator('[data-lead-id]:visible')).toHaveCount(230)
  await expect(notice).toHaveCount(0)
  await expect(page.getByRole('heading', { name: 'Заявки', exact: true })).toBeFocused()
  const rendered = await page.locator('[data-lead-id]:visible').evaluateAll((elements) => elements.map((element) => (element as HTMLElement).dataset.leadId))
  expect(rendered).toEqual(server.leads.map((lead) => lead.id))
  await screenshot(page, 'new-arrivals-accepted', false)
})

test('auto-refresh: manually returning to the beginning acknowledges new arrivals', async ({ page, context }) => {
  const old = Array.from({ length: 50 }, (_, index) => incomingLead(50 - index))
  const server = await startMock(page, context, old)
  await page.locator(`[data-lead-id="${old[30].id}"]:visible`).scrollIntoViewIfNeeded()
  server.leads.unshift(incomingLead(51))
  await page.clock.fastForward(5000)
  await expect(page.getByRole('button', { name: 'Новые заявки: 1 ↑', exact: true })).toBeVisible()
  await page.evaluate(() => window.scrollTo({ top: 0 }))
  await expect(page.getByRole('button', { name: /^Новые заявки:/ })).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Открыть карточку: Тест автообновления 51', exact: true })).toBeVisible()
})

test('auto-refresh: keeps the tag filter and ignores nonmatching arrivals', async ({ page, context }) => {
  const server = await startMock(page, context)
  await selectTag(page, 'Сайт')
  await expect(page.getByLabel('Всего заявок: 1', { exact: true })).toBeVisible()
  server.leads.unshift(incomingLead(2, [{ id: 2, name: 'Реклама', is_system: true }]))
  await page.clock.fastForward(5000)
  await expect(page.getByLabel('Всего заявок: 1', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Открыть карточку: Тест автообновления 2', exact: true })).toHaveCount(0)
  server.leads.unshift(incomingLead(3))
  await page.clock.fastForward(5000)
  await expect(page.getByLabel('Всего заявок: 2', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Открыть карточку: Тест автообновления 3', exact: true })).toBeVisible()
})

test('auto-refresh: preserves the draft and focus without notifications or automatic saving', async ({ page, context }) => {
  const server = await startMock(page, context)
  await page.getByRole('button', { name: 'Добавить заявку', exact: true }).click()
  await page.getByLabel('Имя', { exact: true }).fill('Несохранённый тестовый черновик')
  await page.getByLabel('Контакт 1', { exact: true }).fill('draft@example.com')
  await page.getByLabel('Запрос', { exact: true }).fill('Этот текст должен остаться в форме')
  server.leads.unshift(incomingLead(2))
  await page.clock.fastForward(5000)
  await expect(page.getByLabel('Имя', { exact: true })).toHaveValue('Несохранённый тестовый черновик')
  await expect(page.getByLabel('Запрос', { exact: true })).toHaveValue('Этот текст должен остаться в форме')
  await expect(page.getByLabel('Запрос', { exact: true })).toBeFocused()
  await expect(page.getByRole('button', { name: /^Новые заявки:/ })).toHaveCount(0)
  expect(server.saves).toHaveLength(0)
  await noOverflow(page)
  await screenshot(page, 'draft-during-refresh')
})

test('auto-refresh: a background failure keeps rows and last success until automatic recovery', async ({ page, context }) => {
  const server = await startMock(page, context)
  const row = page.getByRole('button', { name: 'Открыть карточку: Тест автообновления 1', exact: true })
  server.available = false
  await page.clock.fastForward(5000)
  const warning = page.getByRole('alert').filter({ hasText: 'Последнее обновление:' })
  await expect(warning).toBeVisible()
  await expect(row).toBeVisible()
  await screenshot(page, 'refresh-connection-warning')
  await page.clock.fastForward(5000)
  await expect(warning).toHaveCount(1)
  server.available = true
  server.leads.unshift(incomingLead(2))
  await page.clock.fastForward(5000)
  await expect(page.getByRole('button', { name: 'Открыть карточку: Тест автообновления 2', exact: true })).toBeVisible()
  await expect(warning).toHaveCount(0)
  await noOverflow(page)
})

test('auto-refresh: a failed filter change hides old results and recovers under the selected tag', async ({ page, context }) => {
  const server = await startMock(page, context, [incomingLead(1, [{ id: 2, name: 'Реклама', is_system: true }])])
  server.available = false
  await selectTag(page, 'Сайт')
  await expect(page.getByRole('alert').filter({ hasText: 'Не удалось загрузить заявки' })).toBeVisible()
  await expect(page.locator('[data-lead-id]:visible')).toHaveCount(0)
  await expect(page.getByText('Пока нет заявок', { exact: true })).toHaveCount(0)
  server.available = true
  server.leads.unshift(incomingLead(2))
  await page.clock.fastForward(5000)
  await expect(page.getByRole('button', { name: 'Открыть карточку: Тест автообновления 2', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Открыть карточку: Тест автообновления 1', exact: true })).toHaveCount(0)
})

test('auto-refresh: returning to a visible tab refreshes immediately and leaves the draft intact', async ({ page, context }) => {
  const server = await startMock(page, context)
  await page.getByRole('button', { name: 'Добавить заявку', exact: true }).click()
  await page.getByLabel('Имя', { exact: true }).fill('Черновик после возвращения')
  const beforeHide = server.listReads
  await page.evaluate(() => {
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' })
    document.dispatchEvent(new Event('visibilitychange'))
  })
  server.leads.unshift(incomingLead(2))
  await page.clock.fastForward(20_000)
  expect(server.listReads).toBe(beforeHide)
  await page.evaluate(() => {
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'visible' })
    document.dispatchEvent(new Event('visibilitychange'))
  })
  await expect.poll(() => server.listReads).toBeGreaterThan(beforeHide)
  await expect(page.getByLabel('Имя', { exact: true })).toHaveValue('Черновик после возвращения')
  expect(server.saves).toHaveLength(0)
})

test('auto-refresh: own creation does not acknowledge another arrival or interrupt the card', async ({ page, context }) => {
  const old = Array.from({ length: 50 }, (_, index) => incomingLead(50 - index))
  const server = await startMock(page, context, old)
  const row = page.locator(`[data-lead-id="${old[30].id}"]:visible`)
  await row.scrollIntoViewIfNeeded()
  const top = await row.evaluate((element) => element.getBoundingClientRect().top)
  await page.getByRole('button', { name: 'Добавить заявку', exact: true }).evaluate((element) => (element as HTMLButtonElement).click())
  await page.getByLabel('Имя', { exact: true }).fill('Собственная тестовая заявка')
  await page.getByLabel('Контакт 1', { exact: true }).fill('own@example.com')
  await page.getByLabel('Запрос', { exact: true }).fill('Тест исключения собственной заявки')
  server.leads.unshift(incomingLead(51))
  await page.clock.fastForward(5000)
  await page.getByRole('button', { name: /^(Сохранить|Создать) заявку$/, exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Собственная тестовая заявка', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: /^Новые заявки:/ })).toHaveCount(0)
  await page.getByRole('button', { name: 'К списку заявок', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Новые заявки: 1 ↑', exact: true })).toBeVisible()
  await expect.poll(async () => Math.abs(await row.evaluate((element) => element.getBoundingClientRect().top) - top)).toBeLessThan(2)
  expect(server.saves).toHaveLength(1)
})

test('auto-refresh: looking at new leads in one tab does not reset another tab', async ({ page, context }) => {
  const old = Array.from({ length: 50 }, (_, index) => incomingLead(50 - index))
  const server = await startMock(page, context, old)
  await page.locator(`[data-lead-id="${old[30].id}"]:visible`).scrollIntoViewIfNeeded()
  const other = await context.newPage()
  await other.clock.install()
  await other.goto('/')
  await waitForListResults(other)
  server.leads.unshift(incomingLead(51))
  await page.clock.fastForward(5000)
  await expect(page.getByRole('button', { name: 'Новые заявки: 1 ↑', exact: true })).toBeVisible()
  await other.clock.fastForward(5000)
  await expect(other.getByRole('button', { name: 'Открыть карточку: Тест автообновления 51', exact: true })).toBeVisible()
  await expect(other.getByRole('button', { name: /^Новые заявки:/ })).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Новые заявки: 1 ↑', exact: true })).toBeVisible()
  await other.close()
})

test('real API: persisted external creation appears within ten seconds without duplicates', async ({ page, context }) => {
  const password = process.env.CRM_TEST_PASSWORD
  if (!password) throw new Error('Run scripts/test_auth_browser.py to supply temporary credentials')
  await page.goto('/')
  await enter(page, password)
  await waitForListResults(page)
  const session = await (await context.request.get('/api/auth/session/')).json()
  const tags = await (await context.request.get('/api/tags/')).json()
  const site = tags.results.find((tag: { name: string }) => tag.name === 'Сайт')
  expect(site).toBeDefined()
  await selectTag(page, 'Сайт')
  await expect(page.getByRole('combobox', { name: 'Направление' })).toBeVisible()
  const name = `Тест обновления ${randomUUID()}`
  const payload = { submission_id: randomUUID(), name, contacts: ['refresh@example.com'], request: 'Вымышленная заявка для сквозной проверки', tag_ids: [site.id] }
  const started = Date.now()
  const response = await context.request.post('/api/leads/', { data: payload,
    headers: { 'X-CSRFToken': session.csrf_token, Origin: process.env.CRM_TEST_BASE_URL ?? 'http://localhost:15173' } })
  expect(response.status()).toBe(201)
  const stored = await response.json()
  const link = page.getByRole('button', { name: `Открыть карточку: ${name}`, exact: true })
  await expect(link).toBeVisible({ timeout: 10_000 })
  const delay = Date.now() - started
  expect(delay).toBeLessThanOrEqual(10_000)
  console.log(`Persisted external arrival: ${delay} ms (${test.info().project.name})`)
  const repeated = await context.request.post('/api/leads/', { data: payload,
    headers: { 'X-CSRFToken': session.csrf_token, Origin: process.env.CRM_TEST_BASE_URL ?? 'http://localhost:15173' } })
  expect(repeated.status()).toBe(200)
  expect((await repeated.json()).id).toBe(stored.id)
  await expect(link).toHaveCount(1)
  await link.click()
  await expect(page.getByRole('heading', { name, exact: true })).toBeVisible()
  await expect(page.getByRole('link', { name: 'refresh@example.com', exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'К списку заявок', exact: true }).click()
  await expect(link).toBeVisible()
  await noOverflow(page)
})
