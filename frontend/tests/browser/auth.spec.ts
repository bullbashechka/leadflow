import { randomUUID } from 'node:crypto'
import { expect, test } from '@playwright/test'
import type { Route } from '@playwright/test'
import { enter, screenshot, noOverflow, waitForListResults, mockAccess, workspaceLogout, incomingLead } from './crmFixtures'

test('real API: wrong password, persistent 48h cookie, tab logout and independent browser', async ({ page, context, browser }) => {
  const password = process.env.CRM_TEST_PASSWORD
  if (!password) throw new Error('Run scripts/test_auth_browser.py to supply temporary credentials')
  await page.goto('/')
  await expect(page.getByLabel(process.env.CRM_TEST_USERNAME ? 'Логин' : 'Пароль', { exact: true })).toBeFocused()
  await noOverflow(page)
  await screenshot(page, 'login')
  await enter(page, 'incorrect-test-password')
  await expect(page.getByRole('alert')).toContainText(process.env.CRM_TEST_USERNAME ? 'Неверное имя пользователя или пароль' : 'Неверный пароль')
  await enter(page, password)
  await expect(page.getByRole('heading', { name: 'Заявки', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Добавить заявку' }).first()).toBeVisible()
  await workspaceLogout(page)
  const botLink = page.getByRole('link', { name: 'Открыть Telegram-бота' }).first()
  if (process.env.CRM_TEST_BOT_URL) {
    await expect(botLink).toHaveAttribute('href', process.env.CRM_TEST_BOT_URL)
  } else {
    await expect(botLink).toHaveCount(0)
  }
  if ((page.viewportSize()?.width ?? 1440) < 768) await page.keyboard.press('Escape')
  await waitForListResults(page)
  await noOverflow(page)
  await screenshot(page, 'workspace')
  const expiry = await page.evaluate(async () => (await (await fetch('/api/auth/session/')).json()).expires_at)
  await page.reload()
  await expect(await workspaceLogout(page)).toBeVisible()
  expect(await page.evaluate(async () => (await (await fetch('/api/auth/session/')).json()).expires_at)).toBe(expiry)
  const restored = await browser.newContext({ storageState: await context.storageState() })
  const restoredPage = await restored.newPage()
  await restoredPage.goto(process.env.CRM_TEST_BASE_URL!)
  await expect(await workspaceLogout(restoredPage)).toBeVisible()
  expect(await restoredPage.evaluate(async () => (await (await fetch('/api/auth/session/')).json()).expires_at)).toBe(expiry)
  await restored.close()
  const other = await browser.newContext()
  const otherPage = await other.newPage()
  await otherPage.goto(process.env.CRM_TEST_BASE_URL!)
  await enter(otherPage, password)
  await expect(await workspaceLogout(otherPage)).toBeVisible()
  const tab = await context.newPage()
  await tab.goto('/')
  await expect(await workspaceLogout(tab)).toBeVisible()
  await (await workspaceLogout(page)).click()
  await expect(page.getByRole('button', { name: 'Войти', exact: true })).toBeVisible()
  await expect(tab.getByRole('button', { name: 'Войти', exact: true })).toBeVisible()
  await expect(tab.getByRole('button', { name: 'Выйти', exact: true })).toHaveCount(0)
  await expect(await workspaceLogout(otherPage)).toBeVisible()
  await other.close()
})

test('real API: create, review all contacts, preserve filter, and exit an unfinished form', async ({ page }) => {
  const password = process.env.CRM_TEST_PASSWORD
  if (!password) throw new Error('Run scripts/test_auth_browser.py to supply temporary credentials')
  await page.goto('/')
  await enter(page, password)
  await expect(page.getByRole('heading', { name: 'Заявки', exact: true })).toBeVisible()

  await page.getByRole('button', { name: 'Добавить заявку' }).first().click()
  await noOverflow(page)
  await screenshot(page, 'crm-form')
  const discardedName = `Черновик ${randomUUID().slice(0, 8)}`
  await page.getByLabel('Имя', { exact: true }).fill(discardedName)
  await page.getByRole('button', { name: 'К списку заявок' }).click()
  await expect(page.getByRole('dialog', { name: 'Выйти без сохранения?' })).toBeVisible()
  await page.getByRole('button', { name: 'Остаться' }).click()
  await expect(page.getByLabel('Имя', { exact: true })).toHaveValue(discardedName)
  await page.getByRole('button', { name: 'К списку заявок' }).click()
  await page.getByRole('button', { name: 'Выйти без сохранения' }).click()
  await waitForListResults(page)
  await expect(page.getByText(discardedName, { exact: true })).toHaveCount(0)

  const leadName = `Сайт ${randomUUID().slice(0, 8)}`
  const email = 'foo?subject=evil&body=body%20example@example.com'
  await page.getByRole('button', { name: 'Добавить заявку' }).first().click()
  await page.getByLabel('Имя', { exact: true }).fill(leadName)
  await page.getByLabel('Контакт 1').fill('+7 701 123-45-67')
  await page.getByRole('button', { name: 'Добавить контакт' }).click()
  await page.getByLabel('Контакт 2', { exact: true }).fill(email)
  await page.getByRole('button', { name: 'Добавить контакт' }).click()
  await page.getByLabel('Контакт 3', { exact: true }).fill('@alexander')
  await page.getByRole('button', { name: 'Добавить контакт' }).click()
  await page.getByLabel('Запрос', { exact: true }).fill('Нужен сайт агентства')
  const tagPicker = page.getByLabel('Направления', { exact: true })
  await tagPicker.click()
  await page.keyboard.press('ArrowDown')
  await page.keyboard.press('Enter')
  await page.getByRole('button', { name: /^(Сохранить|Создать) заявку$/ }).click()
  await expect(page.getByRole('heading', { name: leadName })).toBeVisible()
  await expect(page.getByRole('region', { name: 'Карточка заявки', exact: true }).getByText('Реклама', { exact: true })).toBeVisible()
  await expect(page.getByRole('link', { name: '+7 701 123-45-67' })).toHaveAttribute('href', 'tel:+77011234567')
  await expect(page.getByRole('link', { name: email })).toHaveAttribute(
    'href', 'mailto:foo%3Fsubject%3Devil%26body%3Dbody%2520example@example.com',
  )
  await expect(page.getByRole('link', { name: '@alexander' })).toHaveAttribute('href', 'https://t.me/alexander')
  await noOverflow(page)
  await screenshot(page, 'crm-detail')

  await page.getByRole('button', { name: 'К списку заявок' }).click()
  await expect(page.getByRole('button', { name: `Открыть карточку: ${leadName}` })).toBeVisible()
  const leadRow = test.info().project.name === 'phone'
    ? page.locator('.lead-mobile-card').filter({ hasText: leadName })
    : page.getByRole('row').filter({ hasText: leadName })
  await expect(leadRow.getByRole('button', { name: 'Ещё 1' })).toBeVisible()
  await leadRow.getByRole('button', { name: 'Ещё 1' }).click()
  await expect(leadRow.getByText('@alexander', { exact: true })).toBeVisible()

  await page.getByRole('combobox', { name: 'Направление' }).click()
  await page.keyboard.press('Home')
  // Other real-API journeys can create leads under «Сайт» in this shared test database.
  await page.keyboard.press('ArrowDown')
  await page.keyboard.press('ArrowDown')
  await page.keyboard.press('Enter')
  await page.keyboard.press('Escape')
  await expect(page.getByRole('status', { name: 'Нет заявок', exact: true })).toBeVisible()
  const untaggedName = `Без тега ${randomUUID().slice(0, 8)}`
  await page.getByRole('button', { name: 'Добавить заявку' }).first().click()
  await page.getByLabel('Имя', { exact: true }).fill(untaggedName)
  await page.getByLabel('Контакт 1').fill('other@example.com')
  await page.getByLabel('Запрос', { exact: true }).fill('Проверка заявки без тега')
  await page.getByRole('button', { name: /^(Сохранить|Создать) заявку$/ }).click()
  await expect(page.getByRole('heading', { name: untaggedName })).toBeVisible()
  await expect(page.getByRole('alert').filter({ hasText: 'Эта заявка не подходит к текущему фильтру' })).toBeVisible()
  await page.getByRole('button', { name: 'К списку заявок' }).click()
  await expect(page.getByRole('status', { name: 'Нет заявок', exact: true })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Заявки', exact: true })).toBeFocused()
  await noOverflow(page)

  await page.getByRole('button', { name: 'Добавить заявку' }).first().click()
  await page.getByLabel('Имя', { exact: true }).fill(`Перед уходом ${randomUUID().slice(0, 8)}`)
  const beforeUnloadPrevented = await page.evaluate(() => {
    const event = new Event('beforeunload', { cancelable: true })
    window.dispatchEvent(event)
    return event.defaultPrevented
  })
  expect(beforeUnloadPrevented).toBe(true)
})

test('CRM retries a failed list request and shows the empty state after recovery', async ({ page, context }) => {
  await mockAccess(context)
  const unavailable = (route: Route) => route.fulfill({ status: 503, json: {
    code: 'service_unavailable', message: 'Список временно недоступен.', field_errors: {},
  } })
  await page.route('**/api/leads/**', unavailable)
  await page.goto('/')
  await enter(page, 'demo')
  await expect(page.getByRole('alert')).toContainText('Не удалось загрузить заявки')
  await page.unroute('**/api/leads/**', unavailable)
  await page.getByRole('button', { name: 'Повторить', exact: true }).click()
  await expect(page.getByRole('status', { name: 'Нет заявок', exact: true })).toBeVisible()
  await noOverflow(page)
})

test('server field errors keep form values editable for a corrected retry', async ({ page, context }) => {
  const server = await mockAccess(context)
  server.rejectNextCreate = true
  await page.goto('/')
  await enter(page, 'demo')
  await page.getByRole('button', { name: 'Добавить заявку' }).first().click()
  const name = `Проверка ${randomUUID().slice(0, 8)}`
  await page.getByLabel('Имя', { exact: true }).fill(name)
  await page.getByLabel('Контакт 1', { exact: true }).fill('person@example.com')
  await page.getByLabel('Запрос', { exact: true }).fill('Первый вариант')
  await page.getByRole('button', { name: /^(Сохранить|Создать) заявку$/ }).click()
  await expect(page.getByRole('alert').filter({ hasText: 'Исправьте поле запроса.' })).toBeVisible()
  await expect(page.getByLabel('Имя', { exact: true })).toHaveValue(name)
  await expect(page.getByLabel('Контакт 1', { exact: true })).toHaveValue('person@example.com')
  await expect(page.getByLabel('Запрос', { exact: true })).toHaveValue('Первый вариант')
  await expect(page.getByText('Опишите запрос иначе.', { exact: true })).toBeVisible()
  await page.getByLabel('Запрос', { exact: true }).fill('Исправленный вариант')
  await page.getByRole('button', { name: /^(Сохранить|Создать) заявку$/ }).click()
  await expect(page.getByRole('heading', { name })).toBeVisible()
  expect(server.saves).toHaveLength(1)
})

test('a committed submission conflict keeps the snapshot and allows confirmed exit', async ({ page, context }) => {
  const server = await mockAccess(context)
  server.conflictNextCreate = true
  await page.goto('/')
  await enter(page, 'demo')
  await page.getByRole('button', { name: 'Добавить заявку' }).first().click()
  await page.getByLabel('Имя', { exact: true }).fill('Конфликт операции')
  await page.getByLabel('Контакт 1').fill('person@example.com')
  await page.getByLabel('Запрос', { exact: true }).fill('Заявка для конфликта')
  await page.getByRole('button', { name: /^(Сохранить|Создать) заявку$/ }).click()

  await expect(page.getByRole('alert')).toContainText('Эта операция уже связана с другими данными')
  await expect(page.getByLabel('Имя', { exact: true })).toHaveValue('Конфликт операции')
  await expect(page.getByLabel('Имя', { exact: true })).toBeDisabled()
  await expect(page.getByRole('button', { name: /^(Сохранить|Создать) заявку$/ })).toBeDisabled()
  const exit = page.getByRole('button', { name: 'К списку заявок' })
  await expect(exit).toBeEnabled()
  await exit.click()
  await expect(page.getByRole('dialog', { name: 'Выйти без сохранения?' })).toBeVisible()
  await page.getByRole('button', { name: 'Остаться' }).click()
  await expect(page.getByLabel('Имя', { exact: true })).toHaveValue('Конфликт операции')
  await exit.click()
  await page.getByRole('button', { name: 'Выйти без сохранения' }).click()
  await expect(page.getByRole('heading', { name: 'Заявки', exact: true })).toBeVisible()
})

test('lead cards display the saved status', async ({ page, context }) => {
  const server = await mockAccess(context)
  const statuses = [
    { status: 'new', label: 'Новый' },
    { status: 'in_progress', label: 'В работе' },
    { status: 'closed', label: 'Закрыт' },
  ]
  server.leads = statuses.map(({ status }, index) => ({
    ...incomingLead(3 - index),
    id: randomUUID(),
    name: `Статус ${index + 1}`,
    contacts: [{ type: 'email', value: `status${index + 1}@example.com` }],
    request: 'Проверка статуса',
    source: 'manual',
    status,
    created_at: new Date(Date.now() - index * 60_000).toISOString(),
    tags: [],
  }))
  await page.goto('/')
  await enter(page, 'demo')

  for (const [index, { label }] of statuses.entries()) {
    const lead = server.leads[index]
    if (!lead) throw new Error('Status fixture is missing a lead')
    await page.getByRole('button', { name: `Открыть карточку: ${lead.name}` }).click()
    await expect(page.getByRole('region', { name: 'Карточка заявки' }).getByText(label, { exact: true }).first()).toBeVisible()
    await page.getByRole('button', { name: 'К списку заявок' }).click()
  }
})

test('creating a lead from a long list preserves the list position after returning from its card', async ({ page, context }) => {
  const server = await mockAccess(context)
  server.leads = Array.from({ length: 80 }, (_, index) => ({
    ...incomingLead(80 - index),
    id: randomUUID(),
    name: `Длинный список ${index + 1}`,
    contacts: [{ type: 'email', value: `lead${index + 1}@example.com` }],
    request: 'Проверка позиции списка',
    source: 'telegram_bot',
    status: 'new',
    created_at: new Date(Date.now() - index * 60_000).toISOString(),
    tags: [],
  }))
  await page.goto('/')
  await enter(page, 'demo')
  await expect(page.getByLabel('Всего заявок: 80', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Показать ещё', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Открыть карточку: Длинный список 51' })).toBeVisible()
  await expect(page.locator('[data-lead-id]:visible')).toHaveCount(80)
  const row = test.info().project.name === 'phone'
    ? page.locator('.lead-mobile-card').filter({ hasText: 'Длинный список 65' })
    : page.getByRole('row').filter({ hasText: 'Длинный список 65' })
  await row.scrollIntoViewIfNeeded()
  const initialTop = await row.evaluate((element) => element.getBoundingClientRect().top)

  await page.getByRole('button', { name: 'Добавить заявку' }).first().evaluate((element) => (element as HTMLButtonElement).click())
  await page.getByLabel('Имя', { exact: true }).fill('Новый лид из длинного списка')
  await page.getByLabel('Контакт 1').fill('new@example.com')
  await page.getByLabel('Запрос', { exact: true }).fill('Проверка возврата к списку')
  await page.getByRole('button', { name: /^(Сохранить|Создать) заявку$/ }).click()
  await expect(page.getByRole('heading', { name: 'Новый лид из длинного списка' })).toBeVisible()
  await page.getByRole('button', { name: 'К списку заявок' }).click()

  await expect.poll(async () => {
    const restoredRow = test.info().project.name === 'phone'
      ? page.locator('.lead-mobile-card').filter({ hasText: 'Длинный список 65' })
      : page.getByRole('row').filter({ hasText: 'Длинный список 65' })
    const box = await restoredRow.boundingBox()
    return box ? Math.abs(box.y - initialTop) : Number.POSITIVE_INFINITY
  }).toBeLessThan(4)
})

test('unknown create outcome retries the same operation without creating a duplicate', async ({ page, context }) => {
  const server = await mockAccess(context)
  server.dropNextCreateResponse = true
  await page.goto('/')
  await enter(page, 'demo')
  await page.getByRole('button', { name: 'Добавить заявку' }).first().click()
  await page.getByLabel('Имя', { exact: true }).fill('Проверка повтора')
  await page.getByLabel('Контакт 1').fill('+7 701 123-45-67')
  await page.getByLabel('Запрос', { exact: true }).fill('Проверка потери ответа')
  await page.getByRole('button', { name: /^(Сохранить|Создать) заявку$/ }).click()
  await expect(page.getByRole('alert')).toContainText('Результат сохранения неизвестен')
  await expect(page.getByLabel('Имя', { exact: true })).toBeDisabled()
  await page.getByRole('button', { name: 'Проверить и повторить' }).click()
  await expect(page.getByRole('heading', { name: 'Проверка повтора' })).toBeVisible()
  expect(server.saves).toHaveLength(1)
  expect(server.leads).toHaveLength(1)
  expect(server.saves[0]).toMatchObject({ name: 'Проверка повтора' })
})

test('API unavailable: initial access stays closed and retry recovers', async ({ page, context }) => {
  const server = await mockAccess(context)
  server.available = false
  await page.goto('/')
  await expect(page.getByText('Не удалось проверить доступ', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Выйти', exact: true })).toHaveCount(0)
  await screenshot(page, 'unavailable')
  server.available = true
  await page.getByRole('button', { name: 'Повторить', exact: true }).click()
  await expect(page.getByLabel('Пароль', { exact: true })).toBeVisible()
})

test('reauthentication keeps form and operation, hides content and never autosaves', async ({ page, context }) => {
  const server = await mockAccess(context)
  await page.goto('/tests/fixtures/reauth.html')
  await enter(page, 'demo')
  const draft = page.getByLabel('Текст черновика', { exact: true })
  await draft.fill('Несохранённый тестовый текст')
  const operation = await page.getByLabel('Идентификатор операции').textContent()
  server.authenticated = false
  await page.evaluate(() => window.dispatchEvent(new Event('focus')))
  await expect(page.getByRole('dialog')).toBeVisible()
  await expect(draft).toBeHidden()
  await expect(page.getByRole('textbox', { name: 'Текст черновика' })).toHaveCount(0)
  await page.keyboard.press('Escape')
  await expect(page.getByRole('dialog')).toBeVisible()
  await page.getByLabel('Пароль', { exact: true }).click()
  for (let index = 0; index < 6; index++) {
    await page.keyboard.press('Tab')
    expect(await page.getByRole('dialog').evaluate((dialog) => dialog.contains(document.activeElement))).toBe(true)
  }
  await noOverflow(page)
  await screenshot(page, 'reauth')
  await enter(page, 'demo')
  await expect(draft).toHaveValue('Несохранённый тестовый текст')
  await expect(draft).toBeVisible()
  await expect(draft).toBeFocused()
  expect(await page.getByLabel('Идентификатор операции').textContent()).toBe(operation)
  expect(server.saves).toHaveLength(0)
  await page.getByRole('button', { name: 'Сохранить (тест)' }).click()
  await expect(page.getByRole('status')).toContainText('Подтверждено')
  expect(server.saves).toHaveLength(1)
})

test('offline logout stays closed after reload and reconnect unlocks both tabs only after login', async ({ page, context }) => {
  const server = await mockAccess(context)
  await page.goto('/')
  await enter(page, 'demo')
  const tab = await context.newPage()
  await tab.goto('/')
  await expect(await workspaceLogout(tab)).toBeVisible()
  server.available = false
  await page.evaluate(() => window.dispatchEvent(new Event('focus')))
  await expect(page.getByText('Нет связи с сервером', { exact: true })).toBeVisible()
  await screenshot(page, 'offline-workspace')
  await (await workspaceLogout(page)).click()
  await expect(page.getByText('Данные скрыты', { exact: true })).toBeVisible()
  await expect(tab.getByText('Данные скрыты', { exact: true })).toBeVisible()
  await page.reload()
  await expect(page.getByText('Данные скрыты', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Войти', exact: true })).toHaveCount(0)
  await screenshot(page, 'pending-logout')
  server.available = true
  await page.evaluate(() => window.dispatchEvent(new Event('online')))
  await expect(page.getByRole('button', { name: 'Войти', exact: true })).toBeVisible()
  await expect(tab.getByRole('button', { name: 'Войти', exact: true })).toBeVisible()
  expect(server.authenticated).toBe(false)
  await enter(page, 'demo')
  await expect(await workspaceLogout(tab)).toBeVisible()
  expect(server.logins).toBe(2)
})

test('storage fallback synchronizes logout and login without BroadcastChannel', async ({ page, context }) => {
  await context.addInitScript(() => { Object.defineProperty(window, 'BroadcastChannel', { value: undefined }) })
  await mockAccess(context)
  await page.goto('/')
  await enter(page, 'demo')
  const tab = await context.newPage()
  await tab.goto('/')
  await expect(await workspaceLogout(tab)).toBeVisible()
  await (await workspaceLogout(page)).click()
  await expect(tab.getByRole('button', { name: 'Войти' })).toBeVisible()
  await enter(page, 'demo')
  await expect(await workspaceLogout(tab)).toBeVisible()
})

test('known expiry locks an offline form while preserving its text', async ({ page, context }) => {
  const server = await mockAccess(context, 2000)
  await page.goto('/tests/fixtures/reauth.html')
  await enter(page, 'demo')
  const draft = page.getByLabel('Текст черновика', { exact: true })
  await draft.fill('Текст до истечения срока')
  server.available = false
  await page.evaluate(() => window.dispatchEvent(new Event('focus')))
  await expect(page.getByRole('dialog')).toBeVisible({ timeout: 5000 })
  await expect(draft).toBeHidden()
  expect(await draft.inputValue()).toBe('Текст до истечения срока')
  expect(server.saves).toHaveLength(0)
})

test('offline form stays editable before expiry; save is unavailable', async ({ page, context }) => {
  const server = await mockAccess(context)
  await page.goto('/tests/fixtures/reauth.html')
  await enter(page, 'demo')
  await expect(page.getByLabel('Текст черновика', { exact: true })).toBeVisible()
  server.available = false
  await page.evaluate(() => window.dispatchEvent(new Event('focus')))
  await expect(page.getByRole('button', { name: 'Сохранить (тест)' })).toBeDisabled()
  await page.getByLabel('Текст черновика', { exact: true }).fill('Текст при обрыве связи')
  await expect(page.getByRole('dialog')).toBeHidden()
  expect(server.saves).toHaveLength(0)
})
