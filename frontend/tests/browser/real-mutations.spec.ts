import { randomUUID } from 'node:crypto'
import { expect, test } from '@playwright/test'
import { enter, noOverflow } from './crmFixtures'

test('real API: editing, status, first deletion and retries preserve one operation', async ({ page }) => {
  const password = process.env.CRM_TEST_PASSWORD
  if (!password) throw new Error('Run scripts/test_auth_browser.py for isolated credentials')
  await page.goto('/')
  await enter(page, password)
  await expect(page.getByRole('heading', { name: 'Заявки', exact: true })).toBeVisible()
  const submission = {
    submission_id: randomUUID(), name: `Проверка удаления ${randomUUID().slice(0, 8)}`,
    contacts: ['synthetic@example.com'], request: 'Синтетическая проверка сохранения', tag_ids: [],
  }
  const created = await page.evaluate(async (body) => {
    const session = await (await fetch('/api/auth/session/')).json()
    const response = await fetch('/api/leads/', {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRFToken': session.csrf_token },
      body: JSON.stringify(body),
    })
    return { status: response.status, body: await response.json() }
  }, submission)
  expect(created.status).toBe(201)
  const leadId = created.body.id as string
  await page.reload()
  await page.getByRole('button', { name: `Открыть карточку: ${submission.name}`, exact: true }).click()
  await page.getByRole('button', { name: 'Редактировать', exact: true }).click()
  const editedName = `${submission.name} — сохранено`
  await page.getByLabel('Имя', { exact: true }).fill(editedName)
  const editResponse = page.waitForResponse(response =>
    response.request().method() === 'PUT' && response.url().endsWith(`/api/leads/${leadId}/`))
  await page.getByRole('button', { name: 'Сохранить изменения', exact: true }).click()
  expect((await editResponse).status()).toBe(200)
  await expect(page.getByRole('heading', { name: editedName, exact: true })).toBeVisible()

  const statusSelect = page.getByLabel('Новый статус', { exact: true })
  await statusSelect.click()
  await statusSelect.press('ArrowDown')
  await statusSelect.press('Enter')
  const statusResponse = page.waitForResponse(response =>
    response.request().method() === 'PATCH' && response.url().endsWith(`/api/leads/${leadId}/status/`))
  await page.getByRole('button', { name: 'Сохранить статус', exact: true }).click()
  const changed = await statusResponse
  expect(changed.status()).toBe(200)
  expect((await changed.json()).lead.status).toBe('in_progress')
  await noOverflow(page)

  await page.getByRole('button', { name: 'Удалить заявку', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: 'Удалить заявку?', exact: true })
  await expect(dialog).toContainText(editedName)
  const deletionRequest = page.waitForRequest(request =>
    request.method() === 'DELETE' && request.url().endsWith(`/api/leads/${leadId}/`))
  const deletionResponse = page.waitForResponse(response =>
    response.request().method() === 'DELETE' && response.url().endsWith(`/api/leads/${leadId}/`))
  await dialog.getByRole('button', { name: 'Удалить заявку', exact: true }).click()
  const request = await deletionRequest
  const deleted = await deletionResponse
  expect(deleted.status()).toBe(200)
  expect(await deleted.json()).toEqual({ lead_id: leadId, deleted: true, replayed: false })
  await expect(dialog).toBeHidden()
  await expect(page.getByRole('heading', { name: editedName, exact: true })).toHaveCount(0)
  await expect(page.getByRole('heading', { name: 'Заявки', exact: true })).toBeVisible()

  const retries = await page.evaluate(async ({ id, operation, original }) => {
    const session = await (await fetch('/api/auth/session/')).json()
    const headers = { 'Content-Type': 'application/json', 'X-CSRFToken': session.csrf_token }
    const replay = await fetch(`/api/leads/${id}/`, {
      method: 'DELETE', headers, body: JSON.stringify(operation),
    })
    const resurrect = await fetch('/api/leads/', {
      method: 'POST', headers, body: JSON.stringify(original),
    })
    const missing = await fetch(`/api/leads/${id}/`)
    return { replayStatus: replay.status, replayBody: await replay.json(),
      resurrectStatus: resurrect.status, resurrectBody: await resurrect.json(), missingStatus: missing.status }
  }, { id: leadId, operation: request.postDataJSON(), original: submission })
  expect(retries.replayStatus).toBe(200)
  expect(retries.replayBody).toEqual({ lead_id: leadId, deleted: true, replayed: true })
  expect(retries.resurrectStatus).toBe(410)
  expect(retries.resurrectBody.code).toBe('submission_deleted')
  expect(retries.missingStatus).toBe(404)
})
