import assert from 'node:assert/strict'
import { test } from 'node:test'
import { checkHealth } from '../src/api.ts'

test('health checks the real API with cookie credentials', async (t) => {
  let request
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    request = { url, options }
    return new Response(JSON.stringify({ status: 'ok' }))
  })
  await checkHealth()
  assert.equal(request.url, '/api/health/')
  assert.equal(request.options.credentials, 'same-origin')
  assert.equal(request.options.cache, 'no-store')
})

test('HTTP failures and unexpected payloads cannot appear as success', async (t) => {
  t.mock.method(globalThis, 'fetch', async () => new Response('{}', { status: 503 }))
  await assert.rejects(checkHealth(), { status: 503, code: 'service_unavailable' })
  globalThis.fetch = async () => new Response('{"status":"unavailable"}')
  await assert.rejects(checkHealth(), /Unexpected/)
})

test('a stalled request times out', async (t) => {
  t.mock.method(globalThis, 'fetch', (_url, { signal }) => new Promise((_resolve, reject) => {
    signal.addEventListener('abort', () => reject(signal.reason), { once: true })
  }))
  await assert.rejects(checkHealth({ timeoutMs: 20 }), { name: 'TimeoutError' })
})

test('leaving the screen cancels the request', async (t) => {
  const controller = new AbortController()
  t.mock.method(globalThis, 'fetch', (_url, { signal }) => new Promise((_resolve, reject) => {
    signal.addEventListener('abort', () => reject(signal.reason), { once: true })
  }))
  const request = checkHealth({ signal: controller.signal })
  controller.abort()
  await assert.rejects(request, { name: 'AbortError' })
})
