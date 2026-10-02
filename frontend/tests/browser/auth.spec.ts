import path from 'node:path'
import { randomUUID } from 'node:crypto'
import { expect, test } from '@playwright/test'
import type { BrowserContext, Page, Route } from '@playwright/test'

async function enter(page: Page, password: string) {
  await page.getByLabel('Пароль', { exact: true }).fill(password)
  await page.getByRole('button', { name: 'Войти', exact: true }).click()
}

async function screenshot(page: Page, name: string) {
  if (process.env.CRM_TEST_ARTIFACTS) {
    await page.screenshot({ path: path.join(process.env.CRM_TEST_ARTIFACTS, `${test.info().project.name}-${name}.png`), fullPage: true, animations: 'disabled' })
  }
}

async function noOverflow(page: Page) {
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
}

async function waitForListResults(page: Page) {
  await expect(page.getByText(/^Заявок: \d+$/).or(page.getByText('Пока нет заявок', { exact: true }))).toBeVisible()
}

async function mockAccess(context: BrowserContext, lifetime = 48 * 60 * 60 * 1000) {
  const crmTags = [
    { id: 1, name: 'Сайт', is_system: true },
    { id: 2, name: 'Реклама', is_system: true },
    { id: 3, name: 'Автоматизация', is_system: true },
    { id: 4, name: 'Другое', is_system: true },
  ]
  const server = {
    authenticated: false,
    available: true,
    logins: 0,
    saves: [] as unknown[],
    leads: [] as Array<Record<string, unknown>>,
    submissions: new Map<string, Record<string, unknown>>(),
    dropNextCreateResponse: false,
    rejectNextCreate: false,
    conflictNextCreate: false,
    expiry: Date.now() + lifetime,
  }
  await context.route('**/api/**', async (route) => {
    if (!server.available) return route.abort('internetdisconnected')
    const endpoint = new URL(route.request().url()).pathname
    if (endpoint.endsWith('/login/')) {
      if (route.request().postDataJSON().password !== 'demo') {
        return route.fulfill({ status: 401, json: { code: 'invalid_password', message: 'Неверный пароль.', field_errors: {} } })
      }
      server.logins++
      server.authenticated = true
      server.expiry = Date.now() + lifetime
    } else if (endpoint.endsWith('/logout/')) {
      server.authenticated = false
      return route.fulfill({ status: 204 })
    } else if (endpoint.endsWith('/test-save/')) {
      server.saves.push(route.request().postDataJSON())
      return route.fulfill({ json: { confirmed: true } })
    } else if (endpoint === '/api/tags/') {
      return route.fulfill({ json: { results: crmTags } })
    } else if (endpoint === '/api/leads/' && route.request().method() === 'POST') {
      if (server.rejectNextCreate) {
        server.rejectNextCreate = false
        return route.fulfill({ status: 400, json: {
          code: 'validation_error', message: 'Исправьте поле запроса.',
          field_errors: { request: ['Опишите запрос иначе.'] },
        } })
      }
      if (server.conflictNextCreate) {
        server.conflictNextCreate = false
        return route.fulfill({ status: 409, json: {
          code: 'submission_conflict', message: 'Идентификатор уже использован для других данных.',
          field_errors: {},
        } })
      }
      const payload = route.request().postDataJSON() as Record<string, unknown>
      const submissionId = String(payload.submission_id)
      let result = server.submissions.get(submissionId)
      const replayed = Boolean(result)
      if (!result) {
        const tagIds = Array.isArray(payload.tag_ids) ? payload.tag_ids : []
        result = {
          id: randomUUID(), name: payload.name, contacts: (payload.contacts as string[]).map((value) => ({
            type: value.startsWith('+') ? 'phone' : value.startsWith('@') ? 'telegram' : 'email', value,
          })),
          request: payload.request, source: 'manual', status: 'new', created_at: new Date().toISOString(),
          tags: crmTags.filter((tag) => tagIds.includes(tag.id)),
        }
        server.submissions.set(submissionId, result)
        server.leads.unshift(result)
        server.saves.push(payload)
      }
      if (server.dropNextCreateResponse) {
        server.dropNextCreateResponse = false
        return route.abort('internetdisconnected')
      }
      return route.fulfill({ status: replayed ? 200 : 201, json: result })
    } else if (endpoint.startsWith('/api/leads/')) {
      const id = endpoint.split('/').filter(Boolean)[2]
      if (id) {
        const result = server.leads.find((lead) => lead.id === id)
        return result
          ? route.fulfill({ json: result })
          : route.fulfill({ status: 404, json: { code: 'not_found', message: 'Заявка не найдена.', field_errors: {} } })
      }
      const url = new URL(route.request().url())
      const tagId = url.searchParams.get('tag_id')
      let results = server.leads.filter((lead) => tagId === null
        || (lead.tags as Array<{ id: number }>).some((tag) => String(tag.id) === tagId))
      const anchor = url.searchParams.get('before_id')
      if (anchor) {
        const index = results.findIndex((lead) => lead.id === anchor)
        results = index < 0 ? [] : results.slice(index + 1)
      } else {
        results = results.slice(Number(url.searchParams.get('offset') ?? 0))
      }
      const count = server.leads.filter((lead) => tagId === null
        || (lead.tags as Array<{ id: number }>).some((tag) => String(tag.id) === tagId)).length
      const limit = Number(url.searchParams.get('limit') ?? 50)
      const page = results.slice(0, limit)
      const more = results.length > limit
      return route.fulfill({ json: {
        count,
        next: more ? `/api/leads/?limit=${limit}&before_id=${page.at(-1)?.id}` : null,
        previous: null,
        results: page,
      } })
    } else if (endpoint.endsWith('/health/')) {
      return route.fulfill({ json: { status: 'ok' } })
    }
    return route.fulfill({ json: {
      authenticated: server.authenticated,
      expires_at: server.authenticated ? new Date(server.expiry).toISOString() : null,
      server_time: new Date().toISOString(), csrf_token: 'fixture-token',
    } })
  })
  return server
}

test('real API: wrong password, persistent 48h cookie, tab logout and independent browser', async ({ page, context, browser }) => {
  const password = process.env.CRM_TEST_PASSWORD
  if (!password) throw new Error('Run scripts/test_auth_browser.py to supply temporary credentials')
  await page.goto('/')
  await expect(page.getByLabel('Пароль', { exact: true })).toBeFocused()
  await noOverflow(page)
  await screenshot(page, 'login')
  await enter(page, 'incorrect-test-password')
  await expect(page.getByRole('alert')).toContainText('Неверный пароль')
  await enter(page, password)
  await expect(page.getByRole('heading', { name: 'Заявки', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Добавить лид' }).first()).toBeVisible()
  const botLink = page.getByRole('link', { name: 'Открыть Telegram-бота' }).first()
  if (process.env.CRM_TEST_BOT_URL) {
    await expect(botLink).toHaveAttribute('href', process.env.CRM_TEST_BOT_URL)
  } else {
    await expect(botLink).toHaveCount(0)
  }
  await waitForListResults(page)
  await noOverflow(page)
  await screenshot(page, 'workspace')
  const expiry = await page.evaluate(async () => (await (await fetch('/api/auth/session/')).json()).expires_at)
  await page.reload()
  await expect(page.getByRole('button', { name: 'Выйти', exact: true })).toBeVisible()
  expect(await page.evaluate(async () => (await (await fetch('/api/auth/session/')).json()).expires_at)).toBe(expiry)
  const restored = await browser.newContext({ storageState: await context.storageState() })
  const restoredPage = await restored.newPage()
  await restoredPage.goto(process.env.CRM_TEST_BASE_URL!)
  await expect(restoredPage.getByRole('button', { name: 'Выйти', exact: true })).toBeVisible()
  expect(await restoredPage.evaluate(async () => (await (await fetch('/api/auth/session/')).json()).expires_at)).toBe(expiry)
  await restored.close()
  const other = await browser.newContext()
  const otherPage = await other.newPage()
  await otherPage.goto(process.env.CRM_TEST_BASE_URL!)
  await enter(otherPage, password)
  await expect(otherPage.getByRole('button', { name: 'Выйти', exact: true })).toBeVisible()
  const tab = await context.newPage()
  await tab.goto('/')
  await expect(tab.getByRole('button', { name: 'Выйти', exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Выйти', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Войти', exact: true })).toBeVisible()
  await expect(tab.getByRole('button', { name: 'Войти', exact: true })).toBeVisible()
  await expect(tab.getByRole('button', { name: 'Выйти', exact: true })).toHaveCount(0)
  await expect(otherPage.getByRole('button', { name: 'Выйти', exact: true })).toBeVisible()
  await other.close()
})

test('real API: create, review all contacts, preserve filter, and exit an unfinished form', async ({ page }) => {
  const password = process.env.CRM_TEST_PASSWORD
  if (!password) throw new Error('Run scripts/test_auth_browser.py to supply temporary credentials')
  await page.goto('/')
  await enter(page, password)
  await expect(page.getByRole('heading', { name: 'Заявки', exact: true })).toBeVisible()

  await page.getByRole('button', { name: 'Добавить лид' }).first().click()
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
  await page.getByRole('button', { name: 'Добавить лид' }).first().click()
  await page.getByLabel('Имя', { exact: true }).fill(leadName)
  await page.getByLabel('Контакт 1').fill('+7 701 123-45-67')
  await page.getByRole('button', { name: 'Добавить контакт' }).click()
  await page.getByLabel('Контакт 2', { exact: true }).fill(email)
  await page.getByRole('button', { name: 'Добавить контакт' }).click()
  await page.getByLabel('Контакт 3', { exact: true }).fill('@alexander')
  await page.getByRole('button', { name: 'Добавить контакт' }).click()
  await page.getByLabel('Запрос', { exact: true }).fill('Нужен сайт агентства')
  const tagPicker = page.getByLabel('Теги', { exact: true })
  await tagPicker.click()
  await page.keyboard.press('ArrowDown')
  await page.keyboard.press('Enter')
  await page.getByRole('button', { name: 'Сохранить лид' }).click()
  await expect(page.getByRole('heading', { name: leadName })).toBeVisible()
  await expect(page.getByText('Реклама', { exact: true })).toBeVisible()
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

  await page.getByRole('combobox', { name: 'Фильтр по тегу' }).click()
  await page.keyboard.press('Home')
  await page.keyboard.press('Enter')
  await page.keyboard.press('Escape')
  await expect(page.getByText('По этому тегу заявок нет', { exact: true })).toBeVisible()
  const untaggedName = `Без тега ${randomUUID().slice(0, 8)}`
  await page.getByRole('button', { name: 'Добавить лид' }).first().click()
  await page.getByLabel('Имя', { exact: true }).fill(untaggedName)
  await page.getByLabel('Контакт 1').fill('other@example.com')
  await page.getByLabel('Запрос', { exact: true }).fill('Проверка заявки без тега')
  await page.getByRole('button', { name: 'Сохранить лид' }).click()
  await expect(page.getByRole('heading', { name: untaggedName })).toBeVisible()
  await expect(page.getByRole('alert').filter({ hasText: 'Этот лид не подходит к текущему фильтру' })).toBeVisible()
  await page.getByRole('button', { name: 'К списку заявок' }).click()
  await expect(page.getByText('По этому тегу заявок нет', { exact: true })).toBeVisible()
  await noOverflow(page)

  await page.getByRole('button', { name: 'Добавить лид' }).first().click()
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
  await expect(page.getByText('Пока нет заявок', { exact: true })).toBeVisible()
  await noOverflow(page)
})

test('server field errors keep form values editable for a corrected retry', async ({ page, context }) => {
  const server = await mockAccess(context)
  server.rejectNextCreate = true
  await page.goto('/')
  await enter(page, 'demo')
  await page.getByRole('button', { name: 'Добавить лид' }).first().click()
  const name = `Проверка ${randomUUID().slice(0, 8)}`
  await page.getByLabel('Имя', { exact: true }).fill(name)
  await page.getByLabel('Контакт 1', { exact: true }).fill('person@example.com')
  await page.getByLabel('Запрос', { exact: true }).fill('Первый вариант')
  await page.getByRole('button', { name: 'Сохранить лид' }).click()
  await expect(page.getByRole('alert').filter({ hasText: 'Исправьте поле запроса.' })).toBeVisible()
  await expect(page.getByLabel('Имя', { exact: true })).toHaveValue(name)
  await expect(page.getByLabel('Контакт 1', { exact: true })).toHaveValue('person@example.com')
  await expect(page.getByLabel('Запрос', { exact: true })).toHaveValue('Первый вариант')
  await expect(page.getByText('Опишите запрос иначе.', { exact: true })).toBeVisible()
  await page.getByLabel('Запрос', { exact: true }).fill('Исправленный вариант')
  await page.getByRole('button', { name: 'Сохранить лид' }).click()
  await expect(page.getByRole('heading', { name })).toBeVisible()
  expect(server.saves).toHaveLength(1)
})

test('a committed submission conflict keeps the snapshot and allows confirmed exit', async ({ page, context }) => {
  const server = await mockAccess(context)
  server.conflictNextCreate = true
  await page.goto('/')
  await enter(page, 'demo')
  await page.getByRole('button', { name: 'Добавить лид' }).first().click()
  await page.getByLabel('Имя', { exact: true }).fill('Конфликт операции')
  await page.getByLabel('Контакт 1').fill('person@example.com')
  await page.getByLabel('Запрос', { exact: true }).fill('Заявка для конфликта')
  await page.getByRole('button', { name: 'Сохранить лид' }).click()

  await expect(page.getByRole('alert')).toContainText('Эта операция уже связана с другими данными')
  await expect(page.getByLabel('Имя', { exact: true })).toHaveValue('Конфликт операции')
  await expect(page.getByLabel('Имя', { exact: true })).toBeDisabled()
  await expect(page.getByRole('button', { name: 'Сохранить лид' })).toBeDisabled()
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
    await expect(page.getByText(label, { exact: true })).toBeVisible()
    await page.getByRole('button', { name: 'К списку заявок' }).click()
  }
})

test('creating a lead from a long list preserves the list position after returning from its card', async ({ page, context }) => {
  const server = await mockAccess(context)
  server.leads = Array.from({ length: 80 }, (_, index) => ({
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
  await expect(page.getByText('Заявок: 80', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Показать ещё', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Открыть карточку: Длинный список 51' })).toBeVisible()
  const renderedRows = test.info().project.name === 'phone'
    ? await page.locator('.lead-mobile-card').count()
    : await page.locator('.lead-desktop-table tbody tr.ant-table-row').count()
  expect(renderedRows).toBe(80)
  const row = test.info().project.name === 'phone'
    ? page.locator('.lead-mobile-card').filter({ hasText: 'Длинный список 65' })
    : page.getByRole('row').filter({ hasText: 'Длинный список 65' })
  await row.scrollIntoViewIfNeeded()
  const initialTop = await row.evaluate((element) => element.getBoundingClientRect().top)

  await page.getByRole('button', { name: 'Добавить лид' }).first().evaluate((element) => (element as HTMLButtonElement).click())
  await page.getByLabel('Имя', { exact: true }).fill('Новый лид из длинного списка')
  await page.getByLabel('Контакт 1').fill('new@example.com')
  await page.getByLabel('Запрос', { exact: true }).fill('Проверка возврата к списку')
  await page.getByRole('button', { name: 'Сохранить лид' }).click()
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
  await page.getByRole('button', { name: 'Добавить лид' }).first().click()
  await page.getByLabel('Имя', { exact: true }).fill('Проверка повтора')
  await page.getByLabel('Контакт 1').fill('+7 701 123-45-67')
  await page.getByLabel('Запрос', { exact: true }).fill('Проверка потери ответа')
  await page.getByRole('button', { name: 'Сохранить лид' }).click()
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
  await expect(tab.getByRole('button', { name: 'Выйти' })).toBeVisible()
  server.available = false
  await page.evaluate(() => window.dispatchEvent(new Event('focus')))
  await expect(page.getByText('Нет связи с сервером', { exact: true })).toBeVisible()
  await screenshot(page, 'offline-workspace')
  await page.getByRole('button', { name: 'Выйти' }).click()
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
  await expect(tab.getByRole('button', { name: 'Выйти', exact: true })).toBeVisible()
  expect(server.logins).toBe(2)
})

test('storage fallback synchronizes logout and login without BroadcastChannel', async ({ page, context }) => {
  await context.addInitScript(() => { Object.defineProperty(window, 'BroadcastChannel', { value: undefined }) })
  await mockAccess(context)
  await page.goto('/')
  await enter(page, 'demo')
  const tab = await context.newPage()
  await tab.goto('/')
  await expect(tab.getByRole('button', { name: 'Выйти' })).toBeVisible()
  await page.getByRole('button', { name: 'Выйти' }).click()
  await expect(tab.getByRole('button', { name: 'Войти' })).toBeVisible()
  await enter(page, 'demo')
  await expect(tab.getByRole('button', { name: 'Выйти' })).toBeVisible()
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
