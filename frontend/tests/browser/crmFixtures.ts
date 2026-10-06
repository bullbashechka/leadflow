import path from 'node:path'
import { randomUUID } from 'node:crypto'
import { expect, test } from '@playwright/test'
import type { BrowserContext, Page } from '@playwright/test'

export async function enter(page: Page, password: string, username = process.env.CRM_TEST_USERNAME) {
  const passwordField = page.getByLabel('Пароль', { exact: true })
  await expect(passwordField).toBeVisible()
  const usernameField = page.getByLabel('Логин', { exact: true })
  if (await usernameField.isVisible()) await usernameField.fill(username ?? '')
  await passwordField.fill(password)
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
  // React media-query subscribers and control layout settle after viewport resize.
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
    { message: 'The settled page must fit its viewport without horizontal overflow' }).toBe(true)
}

export async function waitForListResults(page: Page) {
  await expect(page.getByLabel(/^Всего заявок: \d+$/)).toHaveText(/^\d+$/)
}

export function incomingLead(index: number, tags = [{ id: 1, name: 'Сайт', is_system: true }]) {
  return {
    id: `${String(index).padStart(8, '0')}-0000-0000-0000-000000000000`,
    arrival_sequence: index,
    name: `Тест автообновления ${index}`,
    contacts: [{ type: 'email', value: 'test@example.com' }],
    request: 'Вымышленная заявка для проверки обновления', note: '', source: 'telegram_bot', status: 'new',
    version: 1, is_demo: false, tags,
    created_at: new Date(Date.UTC(2026, 9, 5, 12, 0, index)).toISOString(),
  }
}

export async function mockAccess(context: BrowserContext, lifetime = 48 * 60 * 60 * 1000) {
  const crmTags = [
    { id: 1, name: 'Сайт', is_system: true, lead_count: 0 },
    { id: 2, name: 'Реклама', is_system: true, lead_count: 0 },
    { id: 3, name: 'Автоматизация', is_system: true, lead_count: 0 },
    { id: 4, name: 'Другое', is_system: true, lead_count: 0 },
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
    conflictNextUpdate: false,
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
    } else if (endpoint === '/api/tags/' && route.request().method() === 'GET') {
      return route.fulfill({ json: { results: crmTags.map((tag) => ({
        ...tag,
        lead_count: server.leads.filter((lead) => (lead.tags as Array<{ id: number }>).some((item) => item.id === tag.id)).length,
      })) } })
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
          request: payload.request, note: '', source: 'manual', status: 'new', version: 1, is_demo: false,
          arrival_sequence: Math.max(0, ...server.leads.map((lead) => Number(lead.arrival_sequence))) + 1,
          created_at: new Date().toISOString(),
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
    } else if (endpoint.startsWith('/api/tags/') && route.request().method() === 'DELETE') {
      const id = Number(endpoint.split('/').filter(Boolean)[2])
      const tag = crmTags.find((item) => item.id === id)
      if (tag?.is_system) return route.fulfill({ status: 409, json: { code: 'protected_tag', message: 'Нельзя удалить.', field_errors: {} } })
      const affected = server.leads.filter((lead) => (lead.tags as Array<{ id: number }>).some((item) => item.id === id)).length
      if (tag) {
        for (const lead of server.leads) {
          if ((lead.tags as Array<{ id: number }>).some((item) => item.id === id)) {
            lead.tags = (lead.tags as Array<{ id: number }>).filter((item) => item.id !== id)
            lead.version = Number(lead.version) + 1
          }
        }
        crmTags.splice(crmTags.indexOf(tag), 1)
      }
      return route.fulfill({ json: { tag_id: id, deleted: true, replayed: false, affected_leads: affected } })
    } else if (endpoint === '/api/tags/' && route.request().method() === 'POST') {
      const payload = route.request().postDataJSON() as { name: string }
      const existing = crmTags.find((tag) => tag.name.toLocaleLowerCase() === payload.name.trim().toLocaleLowerCase())
      const tag = existing ?? { id: Math.max(0, ...crmTags.map((item) => item.id)) + 1, name: payload.name.trim(), is_system: false, lead_count: 0 }
      if (!existing) crmTags.push(tag)
      return route.fulfill({ status: existing ? 200 : 201, json: { tag, created: !existing, replayed: false } })
    } else if (endpoint.startsWith('/api/leads/') && route.request().method() === 'DELETE') {
      const id = endpoint.split('/').filter(Boolean)[2]
      const index = server.leads.findIndex((lead) => lead.id === id)
      if (index < 0) return route.fulfill({ status: 404, json: { code: 'not_found', message: 'Не найдено.', field_errors: {} } })
      server.leads.splice(index, 1)
      return route.fulfill({ json: { lead_id: id, deleted: true, replayed: false } })
    } else if (endpoint.startsWith('/api/leads/') && route.request().method() === 'PUT') {
      const id = endpoint.split('/').filter(Boolean)[2]
      const index = server.leads.findIndex((lead) => lead.id === id)
      if (index < 0) return route.fulfill({ status: 404, json: { code: 'not_found', message: 'Не найдено.', field_errors: {} } })
      const payload = route.request().postDataJSON() as Record<string, unknown>
      let current = server.leads[index]
      if (server.conflictNextUpdate) {
        server.conflictNextUpdate = false
        current = { ...current, name: 'Актуальное имя', note: 'Актуальная заметка', version: Number(current.version) + 1 }
        server.leads[index] = current
        return route.fulfill({ status: 409, json: {
          code: 'version_conflict', message: 'Заявка изменилась в другой вкладке.', field_errors: {}, current_lead: current,
        } })
      }
      const tags = crmTags.filter((tag) => (payload.tag_ids as number[]).includes(tag.id))
      const result = {
        ...current,
        name: payload.name,
        contacts: (payload.contacts as string[]).map((value) => ({ type: value.startsWith('+') ? 'phone' : value.startsWith('@') ? 'telegram' : 'email', value })),
        request: payload.request,
        note: payload.note,
        tags,
        version: Number(current.version) + 1,
      }
      server.leads[index] = result
      return route.fulfill({ json: { operation_id: payload.operation_id, replayed: false, applied_version: result.version, lead: result } })
    } else if (endpoint.startsWith('/api/leads/') && route.request().method() === 'PATCH') {
      const id = endpoint.split('/').filter(Boolean)[2]
      const index = server.leads.findIndex((lead) => lead.id === id)
      const payload = route.request().postDataJSON() as Record<string, unknown>
      if (index < 0) return route.fulfill({ status: 404, json: { code: 'not_found', message: 'Не найдено.', field_errors: {} } })
      const result = { ...server.leads[index], status: payload.status, version: Number(server.leads[index].version) + 1 }
      server.leads[index] = result
      return route.fulfill({ json: { operation_id: payload.operation_id, replayed: false, applied_version: result.version, lead: result } })
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
      const terms = (url.searchParams.get('q') ?? '').toLocaleLowerCase().trim().split(/\s+/).filter(Boolean)
      const status = url.searchParams.get('status')
      results = results.filter((lead) => (status === null || lead.status === status)
        && terms.every((term) => [String(lead.name), String(lead.request), ...(lead.contacts as Array<{ value: string }>).map((contact) => contact.value)]
          .some((value) => value.toLocaleLowerCase().includes(term)
            || (term.replace(/\D/g, '').length > 0 && value.replace(/\D/g, '').includes(term.replace(/\D/g, ''))))))
      const cursor = url.searchParams.get('before_sequence')
      const anchor = url.searchParams.get('before_id')
      if (cursor !== null) {
        results = results.filter((lead) => Number(lead.arrival_sequence) < Number(cursor))
      } else if (anchor) {
        const index = results.findIndex((lead) => lead.id === anchor)
        results = index < 0 ? [] : results.slice(index + 1)
      } else {
        results = results.slice(Number(url.searchParams.get('offset') ?? 0))
      }
      const count = server.leads.filter((lead) => (tagId === null
        || (lead.tags as Array<{ id: number }>).some((tag) => String(tag.id) === tagId))
        && (status === null || lead.status === status)
        && terms.every((term) => [String(lead.name), String(lead.request), ...(lead.contacts as Array<{ value: string }>).map((contact) => contact.value)]
          .some((value) => value.toLocaleLowerCase().includes(term)
            || (term.replace(/\D/g, '').length > 0 && value.replace(/\D/g, '').includes(term.replace(/\D/g, '')))))).length
      const limit = Number(url.searchParams.get('limit') ?? 50)
      const page = results.slice(0, limit)
      const more = results.length > limit
      return route.fulfill({ json: {
        count,
        next: more ? `/api/leads/?limit=${limit}&before_sequence=${page.at(-1)?.arrival_sequence}` : null,
        previous: null,
        latest_sequence: Math.max(0, ...server.leads.map((lead) => Number(lead.arrival_sequence))),
        new_count: url.searchParams.get('since_sequence') === null ? 0 : server.leads.filter((lead) => Number(lead.arrival_sequence) > Number(url.searchParams.get('since_sequence'))
          && !url.searchParams.getAll('exclude_id').includes(String(lead.id))
          && (tagId === null || (lead.tags as Array<{ id: number }>).some((tag) => String(tag.id) === tagId))
          && (status === null || lead.status === status)
          && terms.every((term) => [String(lead.name), String(lead.request), ...(lead.contacts as Array<{ value: string }>).map((contact) => contact.value)]
            .some((value) => value.toLocaleLowerCase().includes(term)))).length,
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
