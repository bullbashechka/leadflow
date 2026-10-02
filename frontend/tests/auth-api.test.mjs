import assert from 'node:assert/strict'
import { test } from 'node:test'
import * as api from '../src/api.ts'

const session = {
  authenticated: true,
  expires_at: '2026-10-04T12:00:00+00:00',
  server_time: '2026-10-02T12:00:00+00:00',
  csrf_token: 'test-token',
}

test('session discovery includes cookies and never caches access', async (t) => {
  let request
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    request = { url, options }
    return Response.json(session)
  })
  assert.equal(typeof api.getSession, 'function', 'session discovery must be available')
  assert.deepEqual(await api.getSession(), session)
  assert.equal(request.url, '/api/auth/session/')
  assert.equal(request.options.credentials, 'same-origin')
  assert.equal(request.options.cache, 'no-store')
})

test('login and logout send explicit CSRF and JSON without automatic retries', async (t) => {
  const requests = []
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    requests.push({ url, options })
    return url.endsWith('/logout/') ? new Response(null, { status: 204 }) : Response.json(session)
  })
  assert.equal(typeof api.login, 'function', 'login must be available')
  assert.deepEqual(await api.login('password', 'csrf'), session)
  await api.logout('csrf')
  assert.equal(requests.length, 2)
  assert.equal(requests[0].options.headers['X-CSRFToken'], 'csrf')
  assert.deepEqual(JSON.parse(requests[0].options.body), { password: 'password' })
  assert.deepEqual(JSON.parse(requests[1].options.body), {})
})

test('API errors retain their code, fields and retry delay', async (t) => {
  t.mock.method(globalThis, 'fetch', async () => Response.json({
    code: 'rate_limited', message: 'Подождите.', field_errors: {},
  }, { status: 429, headers: { 'Retry-After': '60' } }))
  assert.equal(typeof api.login, 'function', 'login must be available')
  await assert.rejects(api.login('password', 'csrf'), (error) => {
    assert.equal(error.status, 429)
    assert.equal(error.code, 'rate_limited')
    assert.equal(error.retryAfter, 60)
    return true
  })
})

test('HTML and malformed session responses cannot grant access', async (t) => {
  assert.equal(typeof api.getSession, 'function', 'session discovery must be available')
  t.mock.method(globalThis, 'fetch', async () => new Response('<html>unavailable</html>'))
  await assert.rejects(api.getSession())
  globalThis.fetch = async () => Response.json({ ...session, expires_at: null })
  await assert.rejects(api.getSession())
})
