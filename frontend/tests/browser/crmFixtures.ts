import path from 'node:path'
import { randomUUID } from 'node:crypto'
import { expect, test } from '@playwright/test'
import type { BrowserContext, Page } from '@playwright/test'

export async function enter(page: Page, password: string) {
  await page.getByLabel('Пароль', { exact: true }).fill(password)
  await page.getByRole('button', { name: 'Войти', exact: true }).click()
}

export async function workspaceLogout(page: Page) {
  if ((page.viewportSize()?.width ?? 1440) < 768) {
    const dialog = page.getByRole('dialog', { name: 'Меню CRM', exact: true })
    if (!await dialog.isVisible()) await page.getByRole('button', { name: 'Меню', exact: true }).click()
  }
  return page.getByRole('button', { name: 'Выйти', exact: true })
}

export async function screenshot(page: Page, name: string, fullPage = true) {
  if (process.env.CRM_TEST_ARTIFACTS) {
    await page.screenshot({ path: path.join(process.env.CRM_TEST_ARTIFACTS, `${test.info().project.name}-${name}.png`), fullPage, animations: 'disabled' })
  }
}

export async function noOverflow(page: Page) {
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
}

export async function waitForListResults(page: Page) {
  await expect(page.getByLabel(/^Всего заявок: \d+$/)).toHaveText(/^\d+$/)
}

export function incomingLead(index: number, tags = [{ id: 1, name: 'Сайт', is_system: true }]) {
  return {
    id: `${String(index).padStart(8, '0')}-0000-0000-0000-000000000000`,
    name: `Тест автообновления ${index}`,
    contacts: [{ type: 'email', value: 'test@example.com' }],
    request: 'Вымышленная заявка для проверки обновления', source: 'telegram_bot', status: 'new', tags,
    created_at: new Date(Date.UTC(2026, 9, 5, 12, 0, index)).toISOString(),
  }
}

export async function mockAccess(context: BrowserContext, lifetime = 48 * 60 * 60 * 1000) {
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
    listReads: 0,
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
      server.listReads++
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
