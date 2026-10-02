import assert from 'node:assert/strict'
import { test } from 'node:test'
import { createLead, getLead, getLeads, getTags } from '../src/api.ts'

const lead = {
  id: '7312e490-ae04-4f19-9f18-41d7d125db38',
  name: 'Тестовый клиент',
  contacts: [{ type: 'email', value: 'client@example.com' }],
  request: 'Нужен сайт агентства',
  source: 'manual',
  status: 'new',
  created_at: '2026-10-01T12:00:00Z',
  tags: [{ id: 1, name: 'Сайт', is_system: true }],
}

test('tag, list, and card API readers use protected no-store endpoints and parse records', async (t) => {
  const requests = []
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    requests.push({ url, options })
    if (url === '/api/tags/') return Response.json({ results: lead.tags })
    if (url.startsWith('/api/leads/?')) return Response.json({ count: 1, next: null, previous: null, results: [lead] })
    return Response.json(lead)
  })

  assert.deepEqual(await getTags(), lead.tags)
  assert.deepEqual(await getLeads({ tagId: 1 }), { count: 1, next: null, previous: null, results: [lead] })
  assert.deepEqual(await getLead(lead.id), lead)
  assert.deepEqual(requests.map(({ url }) => url), [
    '/api/tags/', '/api/leads/?tag_id=1&limit=50', `/api/leads/${lead.id}/`,
  ])
  for (const { options } of requests) {
    assert.equal(options.credentials, 'same-origin')
    assert.equal(options.cache, 'no-store')
  }
})

test('lead list reader requests later rows from a stable UUID anchor', async (t) => {
  let url
  t.mock.method(globalThis, 'fetch', async (requestUrl) => {
    url = requestUrl
    return Response.json({ count: 75, next: null, previous: null, results: [lead] })
  })
  await getLeads({ tagId: 3, beforeId: lead.id, limit: 25 })
  assert.equal(url, `/api/leads/?tag_id=3&limit=25&before_id=${lead.id}`)
})

test('manual create sends its unchanged operation, form values, and CSRF token once', async (t) => {
  let request
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    request = { url, options }
    return Response.json(lead, { status: 201 })
  })
  const submission = {
    submission_id: '513c476d-64c4-4879-b964-fc4d1a41d65e',
    name: 'Тестовый клиент',
    contacts: ['client@example.com'],
    request: 'Нужен сайт агентства',
    tag_ids: [1],
  }

  assert.deepEqual(await createLead(submission, 'csrf-token'), lead)
  assert.equal(request.url, '/api/leads/')
  assert.equal(request.options.headers['X-CSRFToken'], 'csrf-token')
  assert.deepEqual(JSON.parse(request.options.body), submission)
})

test('malformed lead data cannot be rendered as a valid CRM record', async (t) => {
  t.mock.method(globalThis, 'fetch', async () => Response.json({
    ...lead,
    contacts: [{ type: 'phone', value: 13 }],
  }))
  await assert.rejects(getLead(lead.id), /Unexpected contact response/)
})
