import { expect, test } from '@playwright/test'
import { enter, incomingLead, mockAccess, noOverflow, screenshot, waitForListResults } from './crmFixtures'

test('redesign: desktop keeps the list beside one selected detail through responsive transitions', async ({ page, context }, info) => {
  test.skip(info.project.name !== 'desktop')
  const server = await mockAccess(context)
  server.leads = [incomingLead(2), incomingLead(1)]
  await page.goto('/')
  await enter(page, 'demo')
  await waitForListResults(page)
  const first = page.getByRole('button', { name: 'Открыть карточку: Тест автообновления 2', exact: true })
  await first.click()
  await expect(first).toBeVisible()
  const detail = page.getByRole('region', { name: 'Карточка заявки', exact: true })
  await expect(detail).toBeVisible()
  await expect(detail).toHaveCount(1)
  for (const width of [1199, 768, 375, 1200, 1440]) {
    await page.setViewportSize({ width, height: 1000 })
    await expect(detail).toHaveCount(1)
    await expect(detail.getByRole('heading', { name: 'Тест автообновления 2', exact: true })).toBeVisible()
    if (width < 1200) await expect(first).toHaveCount(0)
    else await expect(first).toBeVisible()
    await noOverflow(page)
  }
  await page.getByRole('button', { name: 'Открыть карточку: Тест автообновления 1', exact: true }).click()
  await expect(detail.getByRole('heading', { name: 'Тест автообновления 1', exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'К списку заявок', exact: true }).click()
  await expect(detail).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Открыть карточку: Тест автообновления 1', exact: true })).toBeFocused()
})

test('redesign: contact expansion does not open a card and blank row space does', async ({ page, context }) => {
  const server = await mockAccess(context)
  const lead = { ...incomingLead(1), contacts: [
    { type: 'email', value: 'one@example.com' }, { type: 'email', value: 'two@example.com' },
    { type: 'telegram', value: '@demo_contact' }, { type: 'phone', value: '+79991234567' },
  ] }
  server.leads = [lead]
  await page.goto('/')
  await enter(page, 'demo')
  await waitForListResults(page)
  const row = page.locator(`[data-lead-id="${lead.id}"]:visible`)
  await row.getByRole('button', { name: 'Ещё 2', exact: true }).click()
  await expect(row.getByText('@demo_contact', { exact: true })).toBeVisible()
  await expect(page.getByRole('region', { name: 'Карточка заявки', exact: true })).toHaveCount(0)
  await row.getByText(lead.request, { exact: true }).click()
  const detail = page.getByRole('region', { name: 'Карточка заявки', exact: true })
  await expect(detail).toBeVisible()
  await expect(detail.getByRole('link', { name: '@demo_contact', exact: true })).toHaveAttribute('href', 'https://t.me/demo_contact')
})

test('redesign: arrivals at the top remain visible while desktop details are selected', async ({ page, context }, info) => {
  test.skip(info.project.name !== 'desktop')
  const server = await mockAccess(context)
  server.leads = [incomingLead(1)]
  await page.clock.install()
  await page.goto('/')
  await enter(page, 'demo')
  await waitForListResults(page)
  await page.getByRole('button', { name: 'Открыть карточку: Тест автообновления 1', exact: true }).click()
  server.leads.unshift(incomingLead(2))
  await page.clock.fastForward(5000)
  await expect(page.getByRole('button', { name: 'Открыть карточку: Тест автообновления 2', exact: true })).toBeVisible()
  await expect(page.getByRole('region', { name: 'Карточка заявки', exact: true }).getByRole('heading', { name: 'Тест автообновления 1', exact: true })).toBeFocused()
})

test('redesign: closing the desktop panel preserves the latest reading position', async ({ page, context }, info) => {
  test.skip(info.project.name !== 'desktop')
  const server = await mockAccess(context)
  const leads = Array.from({ length: 45 }, (_, index) => incomingLead(45 - index))
  server.leads = leads
  await page.goto('/')
  await enter(page, 'demo')
  await waitForListResults(page)
  await page.getByRole('button', { name: `Открыть карточку: ${leads[0].name}`, exact: true }).click()
  await page.locator(`[data-lead-id="${leads[25].id}"]:visible`).scrollIntoViewIfNeeded()
  const anchor = await page.locator('[data-lead-id]:visible').evaluateAll(elements => {
    const row = elements.find(element => element.getBoundingClientRect().bottom > 0) as HTMLElement
    return { id: row.dataset.leadId, top: row.getBoundingClientRect().top }
  })
  const row = page.locator(`[data-lead-id="${anchor.id}"]:visible`)
  const top = anchor.top
  await page.setViewportSize({ width: 1199, height: 1000 })
  await expect(page.locator('[data-lead-id]:visible')).toHaveCount(0)
  await page.setViewportSize({ width: 1200, height: 1000 })
  await expect.poll(async () => Math.abs(await row.evaluate(element => element.getBoundingClientRect().top) - top)).toBeLessThan(4)
  await page.setViewportSize({ width: 1440, height: 1000 })
  await page.getByRole('button', { name: 'К списку заявок', exact: true }).click()
  await expect.poll(async () => Math.abs(await row.evaluate(element => element.getBoundingClientRect().top) - top)).toBeLessThan(4)
  const firstVisibleOpener = await page.locator('[data-lead-id]:visible button[aria-label^="Открыть карточку:"]').evaluateAll(elements =>
    elements.find(element => element.getBoundingClientRect().bottom > 0 && element.getBoundingClientRect().top < innerHeight)?.getAttribute('aria-label'))
  expect(firstVisibleOpener).toBeTruthy()
  await expect(page.getByRole('button', { name: firstVisibleOpener!, exact: true })).toBeFocused()
})

test('redesign: keyboard selection, detail retry and filter change keep one correct card', async ({ page, context }, info) => {
  test.skip(info.project.name !== 'desktop')
  const server = await mockAccess(context)
  const lead = incomingLead(1)
  server.leads = [lead, incomingLead(2, [{ id: 2, name: 'Реклама', is_system: true }])]
  await page.goto('/')
  await enter(page, 'demo')
  await waitForListResults(page)
  await page.route(`**/api/leads/${lead.id}/`, route => route.fulfill({ status: 503, json: {
    code: 'unavailable', message: 'Тестовая ошибка загрузки', field_errors: {},
  } }))
  const opener = page.getByRole('button', { name: `Открыть карточку: ${lead.name}`, exact: true })
  await opener.focus()
  await page.keyboard.press('Tab')
  await page.keyboard.press('Shift+Tab')
  await expect(opener).toBeFocused()
  await screenshot(page, 'redesign-keyboard-focus', false)
  await page.keyboard.press('Enter')
  const detail = page.getByRole('region', { name: 'Карточка заявки', exact: true })
  await expect(detail.getByRole('heading', { name: lead.name, exact: true })).toBeFocused()
  await expect(detail.getByRole('alert')).toContainText('Не удалось обновить карточку')
  await expect(detail.getByRole('link', { name: 'test@example.com', exact: true })).toBeVisible()
  await page.unroute(`**/api/leads/${lead.id}/`)
  await detail.getByRole('button', { name: 'Повторить', exact: true }).click()
  await expect(detail.getByRole('alert')).toHaveCount(0)
  await page.getByRole('combobox', { name: 'Направление' }).click()
  await page.getByText('Реклама', { exact: true }).last().click()
  await expect(detail).toHaveCount(0)
  await expect(page.getByLabel('Всего заявок: 1', { exact: true })).toBeVisible()
  await expect(opener).toHaveCount(0)
})

test('redesign: switching selection ignores a late detail response', async ({ page, context }, info) => {
  test.skip(info.project.name !== 'desktop')
  const server = await mockAccess(context)
  const first = incomingLead(1)
  const second = incomingLead(2)
  server.leads = [first, second]
  let release: () => void = () => {}
  const gate = new Promise<void>(resolve => { release = resolve })
  let started = false
  await page.route(`**/api/leads/${first.id}/`, async route => {
    started = true
    await gate
    await route.fulfill({ json: { ...first, request: 'Поздний ответ первой карточки' } })
  })
  await page.goto('/')
  await enter(page, 'demo')
  await waitForListResults(page)
  await page.getByRole('button', { name: `Открыть карточку: ${first.name}`, exact: true }).click()
  await expect.poll(() => started).toBe(true)
  await page.getByRole('button', { name: `Открыть карточку: ${second.name}`, exact: true }).click()
  release()
  const detail = page.getByRole('region', { name: 'Карточка заявки', exact: true })
  await expect(detail.getByRole('heading', { name: second.name, exact: true })).toBeVisible()
  await expect(detail.getByText('Поздний ответ первой карточки', { exact: true })).toHaveCount(0)
})

test('redesign: draft survives resizing and a created lead hidden by filter has a persistent explanation', async ({ page, context }) => {
  const server = await mockAccess(context)
  server.leads = [incomingLead(1)]
  await page.goto('/')
  await enter(page, 'demo')
  await waitForListResults(page)
  await page.getByRole('combobox', { name: 'Направление' }).click()
  await page.getByText('Сайт', { exact: true }).last().click()
  await page.getByRole('button', { name: 'Добавить заявку', exact: true }).click()
  await page.getByLabel('Имя', { exact: true }).fill('Тестовый черновик')
  await page.getByLabel('Контакт 1', { exact: true }).fill('draft@example.com')
  await page.getByLabel('Запрос', { exact: true }).fill('Первая строка\n\nВторая строка')
  for (const width of [375, 768, 1199, 1200, 1440]) {
    await page.setViewportSize({ width, height: 1000 })
    await expect(page.getByLabel('Запрос', { exact: true })).toHaveValue('Первая строка\n\nВторая строка')
    await noOverflow(page)
    await screenshot(page, `redesign-demo-form-${width}`)
  }
  await page.getByRole('button', { name: /^(Сохранить|Создать) заявку$/, exact: true }).click()
  const detail = page.getByRole('region', { name: 'Карточка заявки', exact: true })
  await expect(detail.getByText('Эта заявка не подходит к текущему фильтру', { exact: true })).toBeVisible()
  await expect(detail.getByRole('heading', { name: 'Тестовый черновик', exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'К списку заявок', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Открыть карточку: Тест автообновления 1', exact: true })).toBeFocused()
})

test('redesign: visually handles long demo data at all target widths', async ({ page, context }) => {
  const server = await mockAccess(context)
  const lead = { ...incomingLead(1), name: 'Тестовые данные · Очень длинное имя клиента '.repeat(4).slice(0, 100),
    request: ('Тестовые данные. Запрос с несколькими абзацами и длинным описанием.\n\n').repeat(30).slice(0, 2000),
    contacts: [{ type: 'email', value: 'long-demo-contact-address@example.com' }, { type: 'telegram', value: '@test_contact' }],
    tags: [{ id: 1, name: 'Сайт', is_system: true }, { id: 3, name: 'Ш'.repeat(40), is_system: false }],
  }
  server.leads = [lead, incomingLead(2), incomingLead(3)]
  await page.goto('/')
  await enter(page, 'demo')
  await waitForListResults(page)
  for (const width of [375, 768, 1199, 1200, 1440]) {
    await page.setViewportSize({ width, height: 1000 })
    await noOverflow(page)
    await screenshot(page, `redesign-demo-list-${width}`)
    await page.getByRole('button', { name: `Открыть карточку: ${lead.name}`, exact: true }).click()
    const detail = page.getByRole('region', { name: 'Карточка заявки', exact: true })
    await expect(detail.getByRole('link', { name: 'long-demo-contact-address@example.com', exact: true })).toBeVisible()
    await noOverflow(page)
    expect(await detail.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true)
    await screenshot(page, `redesign-demo-detail-${width}`)
    await page.getByRole('button', { name: 'К списку заявок', exact: true }).click()
  }
})

test('redesign: approved layout preview with explicitly labeled demo data', async ({ page, context }, info) => {
  const server = await mockAccess(context)
  const names = ['Анна', 'Марк', 'София', 'Алексей', 'Ольга']
  const requests = ['Нужен сайт для студии. Хочу обсудить сроки и стоимость.', 'Обновить сайт и фирменный стиль', 'Продвижение нового проекта', 'Автоматизировать приём заявок', 'Обсудить новый проект']
  server.leads = names.map((name, index) => ({ ...incomingLead(index + 1), name: `${name} · тест`,
    request: requests[index], contacts: [{ type: 'telegram', value: `@demo_client_${index + 1}` }],
    source: index % 2 ? 'manual' : 'telegram_bot',
    created_at: new Date(Date.now() - index * 3_600_000).toISOString(),
  }))
  await page.goto('/')
  await enter(page, 'demo')
  await waitForListResults(page)
  await expect(page.getByLabel('Всего заявок: 5', { exact: true })).toBeVisible()
  if (info.project.name === 'desktop') {
    await expect(page.getByRole('columnheader')).toHaveCount(4)
    await page.getByRole('button', { name: 'Открыть карточку: Анна · тест', exact: true }).click()
  }
  await noOverflow(page)
  await screenshot(page, 'crm-preview-demo')
})

test('redesign: returning to a tab shows refresh progress while retaining loaded rows', async ({ page, context }) => {
  const server = await mockAccess(context)
  const lead = incomingLead(1)
  server.leads = [lead]
  await page.goto('/')
  await enter(page, 'demo')
  await waitForListResults(page)
  let release: () => void = () => {}
  const gate = new Promise<void>(resolve => { release = resolve })
  await page.route(/\/api\/leads\/\?/, async route => {
    await gate
    await route.fulfill({ json: { count: 1, results: [lead], next: null, previous: null } })
  })
  try {
    await page.evaluate(() => window.dispatchEvent(new Event('focus')))
    await expect(page.getByRole('status').filter({ hasText: 'Обновляем список…' })).toBeVisible()
    await expect(page.getByRole('button', { name: `Открыть карточку: ${lead.name}`, exact: true })).toBeVisible()
  } finally { release() }
  await expect(page.getByRole('status').filter({ hasText: 'Обновляем список…' })).toHaveCount(0)
})
