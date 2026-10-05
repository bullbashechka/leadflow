import { expect, test } from '@playwright/test'
import { enter, incomingLead, mockAccess } from './crmFixtures'

test('locking access hides tag portals and preserves the tag draft', async ({ page, context }) => {
  const server = await mockAccess(context)
  server.leads.push(incomingLead(1))
  await page.goto('/')
  await enter(page, 'demo')
  await page.getByRole('button', { name: 'Открыть карточку: Тест автообновления 1' }).click()
  await page.getByRole('region', { name: 'Карточка заявки' }).getByRole('button', { name: 'Управление тегами' }).click()
  const manager = page.getByRole('dialog', { name: 'Управление тегами' })
  await manager.getByLabel('Название нового тега').fill('Private draft')
  await page.evaluate(() => localStorage.setItem('leadflow.logout-pending', 'test-lock'))
  await page.evaluate(() => window.dispatchEvent(new Event('focus')))
  await expect(manager).toBeHidden()
  await expect(page.getByRole('dialog', { name: 'Вход в CRM' })).toBeVisible()
  await enter(page, 'demo')
  await expect(manager).toBeVisible()
  await expect(manager.getByLabel('Название нового тега')).toHaveValue('Private draft')
})

test('locking access dismisses lead deletion confirmations with private contacts', async ({ page, context }) => {
  const server = await mockAccess(context)
  server.leads.push(incomingLead(2))
  await page.goto('/')
  await enter(page, 'demo')
  await page.getByRole('button', { name: 'Открыть карточку: Тест автообновления 2' }).click()
  await page.getByRole('button', { name: 'Удалить заявку', exact: true }).click()
  const confirmation = page.getByRole('dialog', { name: 'Удалить заявку?' })
  await expect(confirmation).toBeVisible()
  server.authenticated = false
  await page.evaluate(() => window.dispatchEvent(new Event('focus')))
  await expect(page.getByRole('dialog', { name: 'Вход в CRM' })).toBeVisible()
  await expect(confirmation).toBeHidden()
  expect(server.leads).toHaveLength(1)
})

for (const result of ['success', 'conflict', 'delete'] as const) {
  test(`late ${result} response cannot replace the newly selected lead`, async ({ page, context }, info) => {
    test.skip(info.project.name === 'phone', 'Selecting another row while the card is open requires the wide layout')
    const server = await mockAccess(context)
    const first = incomingLead(3)
    const second = incomingLead(4)
    server.leads.push(first, second)
    let release!: () => void
    let started!: () => void
    const pending = new Promise<void>(resolve => { started = resolve })
    const wait = new Promise<void>(resolve => { release = resolve })
    await context.route(`**/api/leads/${first.id}/${result === 'delete' ? '' : 'status/'}`, async route => {
      if (route.request().method() === 'GET') return route.fallback()
      started()
      const payload = route.request().postDataJSON()
      await wait
      if (result === 'delete') return route.fulfill({ json: { lead_id: first.id, deleted: true, replayed: false } })
      const lead = { ...first, status: 'in_progress', version: 2 }
      if (result === 'conflict') return route.fulfill({ status: 409, json: {
        code: 'version_conflict', message: 'Changed', current_lead: lead,
      } })
      return route.fulfill({ json: { operation_id: payload.operation_id, replayed: false, applied_version: 2, lead } })
    })
    await page.goto('/')
    await enter(page, 'demo')
    await page.getByRole('button', { name: `Открыть карточку: ${first.name}` }).click()
    if (result === 'delete') {
      await page.getByRole('button', { name: 'Удалить заявку', exact: true }).click()
      await page.getByRole('dialog', { name: 'Удалить заявку?' }).getByRole('button', { name: 'Удалить заявку' }).click()
      await page.getByRole('dialog', { name: 'Удалить заявку?' }).getByRole('button', { name: 'Оставить заявку' }).click()
    } else {
      const status = page.getByLabel('Новый статус')
      await status.click()
      await status.press('ArrowDown')
      await status.press('Enter')
      await page.getByRole('button', { name: 'Сохранить статус' }).click()
    }
    await pending
    await page.getByRole('button', { name: `Открыть карточку: ${second.name}` }).click()
    await expect(page.getByRole('heading', { name: second.name, exact: true })).toBeVisible()
    const response = page.waitForResponse(response => response.url().includes(first.id)
      && response.request().method() === (result === 'delete' ? 'DELETE' : 'PATCH'))
    release()
    await (await response).finished()
    await page.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))))
    await expect(page.getByRole('heading', { name: second.name, exact: true })).toBeVisible()
  })
}

test('editing warns before unload while save is pending and after its result becomes unknown', async ({ page, context }) => {
  const server = await mockAccess(context)
  const lead = incomingLead(5)
  server.leads.push(lead)
  let release!: () => void
  const wait = new Promise<void>(resolve => { release = resolve })
  await context.route(`**/api/leads/${lead.id}/`, async route => {
    if (route.request().method() !== 'PUT') return route.fallback()
    await wait
    await route.abort('internetdisconnected')
  })
  await page.goto('/')
  await enter(page, 'demo')
  await page.getByRole('button', { name: `Открыть карточку: ${lead.name}` }).click()
  await page.getByRole('button', { name: 'Редактировать' }).click()
  await page.getByLabel('Заметка', { exact: true }).fill('Unsaved note')
  await page.getByRole('button', { name: 'Сохранить изменения' }).click()
  const warns = () => page.evaluate(() => {
    const event = new Event('beforeunload', { cancelable: true })
    window.dispatchEvent(event)
    return event.defaultPrevented
  })
  expect(await warns()).toBe(true)
  release()
  await expect(page.getByText('Результат сохранения неизвестен', { exact: true })).toBeVisible()
  expect(await warns()).toBe(true)
})

test('individual access requires a username and sends both credentials without storing them', async ({ page, context }) => {
  const server = await mockAccess(context)
  const credentials: unknown[] = []
  await context.route('**/api/auth/**', async route => {
    const endpoint = new URL(route.request().url()).pathname
    if (endpoint.endsWith('/login/')) {
      const body = route.request().postDataJSON()
      credentials.push(body)
      if (body.username !== 'test-operator' || body.password !== 'demo') return route.fulfill({
        status: 401, json: { code: 'invalid_credentials', message: 'Неверный логин или пароль.' },
      })
      server.authenticated = true
    } else if (!endpoint.endsWith('/session/')) return route.fallback()
    return route.fulfill({ json: {
      authenticated: server.authenticated, auth_mode: 'individual',
      expires_at: server.authenticated ? new Date(server.expiry).toISOString() : null,
      server_time: new Date().toISOString(), csrf_token: 'test-token',
    } })
  })
  await page.goto('/')
  await expect(page.getByLabel('Логин', { exact: true })).toBeFocused()
  await page.getByLabel('Пароль', { exact: true }).fill('demo')
  await page.getByRole('button', { name: 'Войти', exact: true }).click()
  await expect(page.getByText('Введите логин.', { exact: true })).toBeVisible()
  expect(credentials).toHaveLength(0)
  await enter(page, 'demo', 'test-operator')
  await expect(page.getByRole('heading', { name: 'Заявки', exact: true })).toBeVisible()
  expect(credentials).toEqual([{ username: 'test-operator', password: 'demo' }])
  const stored = await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }))
  expect(stored).not.toContain('test-operator')
  expect(stored).not.toContain('demo')
})
