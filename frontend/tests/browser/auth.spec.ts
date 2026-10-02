import path from 'node:path'
import { expect, test } from '@playwright/test'
import type { BrowserContext, Page } from '@playwright/test'

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

async function mockAccess(context: BrowserContext, lifetime = 48 * 60 * 60 * 1000) {
  const server = { authenticated: false, available: true, logins: 0, saves: [] as unknown[], expiry: Date.now() + lifetime }
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
  await expect(page.getByRole('heading', { name: 'CRM', exact: true })).toBeVisible()
  await expect(page.getByText('Соединение установлено', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Проверить ещё раз' })).toBeEnabled()
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
