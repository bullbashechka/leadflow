import assert from 'node:assert/strict'
import { test } from 'node:test'
import { changeLeadStatus, createLead, createTag, deleteLead, deleteTag, getLead, getLeads, getTags, updateLead } from '../src/api.ts'

const lead = {
  id: '7312e490-ae04-4f19-9f18-41d7d125db38',
  name: 'Тестовый клиент',
  contacts: [{ type: 'email', value: 'client@example.com' }],
  request: 'Нужен сайт агентства',
  note: '',
  source: 'manual',
  status: 'new',
  version: 1,
  is_demo: false,
  arrival_sequence: 1,
  created_at: '2026-10-01T12:00:00Z',
  tags: [{ id: 1, name: 'Сайт', is_system: true }],
}

test('tag, list, and card API readers use protected no-store endpoints and parse records', async (t) => {
  const requests = []
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    requests.push({ url, options })
    if (url === '/api/tags/') return Response.json({ results: lead.tags.map((tag) => ({ ...tag, lead_count: 1 })) })
    if (url.startsWith('/api/leads/?')) return Response.json({ count: 1, new_count: 0, latest_sequence: 1, next: null, previous: null, results: [lead] })
    return Response.json(lead)
  })

  assert.deepEqual(await getTags(), lead.tags.map((tag) => ({ ...tag, lead_count: 1 })))
  assert.deepEqual(await getLeads({ tagId: 1 }), { count: 1, new_count: 0, latest_sequence: 1, next: null, previous: null, results: [lead] })
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

test('lead list sends combined filters, stable cursors and independent arrival baseline', async (t) => {
  let url
  t.mock.method(globalThis, 'fetch', async (requestUrl) => {
    url = requestUrl
    return Response.json({ count: 0, new_count: 0, latest_sequence: 13, next: null, previous: null, results: [] })
  })
  await getLeads({ tagId: 3, status: 'closed', query: '  Анна   сайт ', beforeSequence: 12, sinceSequence: 9, excludeIds: [lead.id] })
  const parsed = new URL(url, 'https://crm.example')
  assert.equal(parsed.pathname, '/api/leads/')
  assert.equal(parsed.searchParams.get('tag_id'), '3')
  assert.equal(parsed.searchParams.get('before_sequence'), '12')
  assert.equal(parsed.searchParams.get('q'), 'Анна сайт')
  assert.equal(parsed.searchParams.get('status'), 'closed')
  assert.equal(parsed.searchParams.get('since_sequence'), '9')
  assert.deepEqual(parsed.searchParams.getAll('exclude_id'), [lead.id])
})

test('tag and lead mutations use CSRF protected idempotent operations', async (t) => {
  const calls = []
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    calls.push({ url, options })
    if (url === '/api/tags/') return Response.json({ tag: { id: 8, name: 'Сайт', is_system: false }, created: false, replayed: false })
    if (url === '/api/tags/8/') return Response.json({ tag_id: 8, deleted: true, replayed: true, affected_leads: 4 })
    return Response.json({ lead_id: lead.id, deleted: true, replayed: false })
  })
  const operation = '513c476d-64c4-4879-b964-fc4d1a41d65e'
  assert.equal((await createTag('Сайт', operation, 'tag-csrf')).tag.id, 8)
  assert.equal((await deleteTag(8, operation, 'tag-delete-csrf')).affected_leads, 4)
  assert.equal((await deleteLead(lead.id, operation, 2, 'lead-csrf')).deleted, true)
  assert.deepEqual(calls.map(({ url }) => url), ['/api/tags/', '/api/tags/8/', `/api/leads/${lead.id}/`])
  assert.deepEqual(calls.map(({ options }) => options.method), ['POST', 'DELETE', 'DELETE'])
  assert.deepEqual(calls.map(({ options }) => options.headers['X-CSRFToken']), ['tag-csrf', 'tag-delete-csrf', 'lead-csrf'])
  assert.deepEqual(calls.map(({ options }) => JSON.parse(options.body)), [
    { operation_id: operation, name: 'Сайт' },
    { operation_id: operation },
    { operation_id: operation, expected_version: 2 },
  ])
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

test('lead edit and status mutations reuse stable operation IDs and parse versioned results', async (t) => {
  const requests = []
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    requests.push({ url, options })
    const payload = JSON.parse(options.body)
    return Response.json({
      operation_id: payload.operation_id,
      replayed: false,
      applied_version: 2,
      lead: { ...lead, name: payload.name ?? lead.name, status: payload.status ?? lead.status, version: 2 },
    })
  })
  const edit = {
    operation_id: '513c476d-64c4-4879-b964-fc4d1a41d65e',
    expected_version: 1,
    name: 'Обновлённый клиент',
    contacts: ['client@example.com'],
    request: 'Новый запрос',
    note: 'Перезвонить',
    tag_ids: [1],
  }
  const status = {
    operation_id: '6c6fe269-8d43-49f6-9f3f-8f21502cf9f5',
    expected_version: 2,
    status: 'in_progress',
  }

  assert.equal((await updateLead(lead.id, edit, 'edit-csrf')).lead.version, 2)
  assert.equal((await changeLeadStatus(lead.id, status, 'status-csrf')).lead.status, 'in_progress')
  assert.deepEqual(requests.map(({ url }) => url), [
    `/api/leads/${lead.id}/`, `/api/leads/${lead.id}/status/`,
  ])
  assert.deepEqual(requests.map(({ options }) => options.method), ['PUT', 'PATCH'])
  assert.deepEqual(requests.map(({ options }) => options.headers['X-CSRFToken']), ['edit-csrf', 'status-csrf'])
  assert.deepEqual(requests.map(({ options }) => JSON.parse(options.body)), [edit, status])
})

test('a version conflict exposes the current card without discarding the update payload', async (t) => {
  let body
  t.mock.method(globalThis, 'fetch', async (_url, options) => {
    body = JSON.parse(options.body)
    return Response.json({
      code: 'version_conflict',
      message: 'Заявка изменилась.',
      field_errors: {},
      current_lead: { ...lead, name: 'Актуальное имя', version: 2 },
    }, { status: 409 })
  })

  await assert.rejects(
    updateLead(lead.id, {
      operation_id: '513c476d-64c4-4879-b964-fc4d1a41d65e',
      expected_version: 1,
      name: 'Мой вариант',
      contacts: ['client@example.com'],
      request: 'Новый запрос',
      note: '',
      tag_ids: [1],
    }, 'csrf-token'),
    (error) => {
      assert.equal(error.code, 'version_conflict')
      assert.equal(error.currentLead.name, 'Актуальное имя')
      return true
    },
  )
  assert.equal(body.name, 'Мой вариант')
})

test('malformed lead data cannot be rendered as a valid CRM record', async (t) => {
  t.mock.method(globalThis, 'fetch', async () => Response.json({
    ...lead,
    contacts: [{ type: 'phone', value: 13 }],
  }))
  await assert.rejects(getLead(lead.id), /Unexpected contact response/)
})
