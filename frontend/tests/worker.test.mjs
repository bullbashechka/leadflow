import assert from 'node:assert/strict'
import { test } from 'node:test'
import { createWorker } from '../worker/index.js'

const env = {
  API_ORIGIN: 'https://api.example.test',
  INGRESS_SHARED_SECRET: 'test-only-ingress-secret-with-32-characters',
  ASSETS: { fetch: async () => new Response('<html>SPA</html>') },
}

test('uncached requests use options accepted by the Workers runtime', async () => {
  const worker = createWorker({ fetchUpstream: async (request, options) => {
    if (request.cache === 'no-store' && options?.cf?.cacheTtl !== undefined) {
      throw new TypeError('CacheTtl is not compatible with cache: no-store')
    }
    return Response.json({ status: 'ok' })
  } })
  const response = await worker.fetch(incoming('/api/health/'), env)
  assert.equal(response.status, 200)
  assert.deepEqual(await response.json(), { status: 'ok' })
  assert.equal(response.headers.get('cache-control'), 'no-store')
})

test('oversized incoming bodies are rejected before upstream with security headers', async () => {
  let calls = 0
  const worker = createWorker({ fetchUpstream: async () => { calls++; return new Response('{}') } })
  const response = await worker.fetch(incoming('/api/leads/', {
    method: 'POST', body: 'x'.repeat(128 * 1024 + 1),
  }), env)
  assert.equal(response.status, 413)
  assert.equal((await response.json()).code, 'request_too_large')
  assert.equal(calls, 0)
  assert.equal(response.headers.get('x-content-type-options'), 'nosniff')
  assert.equal(response.headers.get('x-frame-options'), 'DENY')
  assert.equal(response.headers.get('cache-control'), 'no-store')
})

test('own Worker errors carry the static security policy', async () => {
  const worker = createWorker({ fetchUpstream: async () => { throw new Error('Unavailable') } })
  for (const settings of [env, { ...env, API_ORIGIN: 'invalid' }]) {
    const response = await worker.fetch(incoming(), settings)
    assert.equal(response.headers.get('x-content-type-options'), 'nosniff')
    assert.equal(response.headers.get('x-frame-options'), 'DENY')
    assert.ok(response.headers.get('content-security-policy'))
    assert.ok(response.headers.get('strict-transport-security'))
  }
})

function incoming(path = '/api/leads/', options = {}) {
  const request = new Request(`https://crm.example.test${path}`, {
    ...options,
    headers: { 'CF-Connecting-IP': '198.51.100.20', ...options.headers },
  })
  Object.defineProperty(request, 'cf', { value: { country: 'GB' } })
  return request
}

test('API GET uses the fixed upstream and preserves query and session/CSRF headers', async () => {
  let captured
  const worker = createWorker({ fetchUpstream: async (request, options) => {
    captured = { request, options }
    return new Response('{"leads":[]}', { status: 200 })
  } })
  const response = await worker.fetch(incoming('/api/leads/?q=hello&upstream=https%3A%2F%2Fevil.test', {
    headers: {
      Cookie: 'sessionid=test-only-session',
      Origin: 'https://crm.example.test',
      Referer: 'https://crm.example.test/leads',
      'X-CSRFToken': 'test-only-csrf',
      Accept: 'application/json',
    },
  }), env)
  assert.equal(response.status, 200)
  assert.ok(captured, 'API must contact the upstream instead of static assets')
  assert.equal(captured.request.url, 'https://api.example.test/api/leads/?q=hello&upstream=https%3A%2F%2Fevil.test')
  assert.equal(captured.request.method, 'GET')
  assert.equal(captured.request.headers.get('cookie'), 'sessionid=test-only-session')
  assert.equal(captured.request.headers.get('origin'), 'https://crm.example.test')
  assert.equal(captured.request.headers.get('referer'), 'https://crm.example.test/leads')
  assert.equal(captured.request.headers.get('x-csrftoken'), 'test-only-csrf')
  assert.equal(captured.request.headers.get('accept-encoding'), 'identity')
  assert.equal(captured.request.cache, 'no-store')
  assert.equal(captured.request.redirect, 'manual')
  assert.deepEqual(captured.options.cf, { cacheEverything: false })
})

test('API POST preserves bytes and strips forged forwarding and ingress headers', async () => {
  const body = '{"name":"Тест","operation_id":"test-only"}'
  let captured
  const worker = createWorker({ fetchUpstream: async (request) => {
    captured = { request, body: await request.text() }
    return new Response('{"saved":true}', { status: 201 })
  } })
  const response = await worker.fetch(incoming('/api/leads/', {
    method: 'POST',
    body,
    headers: {
      'Content-Type': 'application/json',
      Host: 'evil.test',
      Forwarded: 'for=127.0.0.1;host=evil.test;proto=https',
      'X-Forwarded-For': '127.0.0.1',
      'X-Forwarded-Proto': 'https',
      'X-Forwarded-Host': 'evil.test',
      'X-Real-IP': '127.0.0.1',
      'X-Leadflow-Ingress-Secret': 'forged',
      'X-Leadflow-Client-IP': '127.0.0.1',
      'X-Leadflow-Other': 'forged',
      'CF-Connecting-IPv6': '::1',
      'True-Client-IP': '127.0.0.1',
    },
  }), env)
  assert.equal(response.status, 201)
  assert.equal(captured.body, body)
  assert.equal(captured.request.method, 'POST')
  assert.equal(captured.request.headers.get('content-type'), 'application/json')
  assert.equal(captured.request.headers.get('x-leadflow-ingress-secret'), env.INGRESS_SHARED_SECRET)
  assert.equal(captured.request.headers.get('x-leadflow-client-ip'), '198.51.100.20')
  for (const name of ['host', 'forwarded', 'x-forwarded-for', 'x-forwarded-proto', 'x-forwarded-host', 'x-real-ip', 'cf-connecting-ip', 'cf-connecting-ipv6', 'true-client-ip', 'x-leadflow-other']) {
    assert.equal(captured.request.headers.get(name), null, name)
  }
})

test('API responses preserve status/body and each Set-Cookie and prohibit CDN caching', async () => {
  const cookies = ['sessionid=test-only; Expires=Fri, 09 Oct 2026 00:00:00 GMT; HttpOnly; Secure; SameSite=Lax', 'csrftoken=test-only; HttpOnly; Secure; SameSite=Lax']
  const worker = createWorker({ fetchUpstream: async () => new Response('{"code":"version_conflict"}', {
    status: 409,
    headers: [
      ['Content-Type', 'application/json'],
      ['Set-Cookie', cookies[0]],
      ['Set-Cookie', cookies[1]],
      ['Cache-Control', 'public, max-age=3600'],
      ['CDN-Cache-Control', 'public, max-age=3600'],
      ['Cloudflare-CDN-Cache-Control', 'public, max-age=3600'],
      ['Surrogate-Control', 'max-age=3600'],
      ['Content-Length', '1000'],
      ['Content-Encoding', 'gzip'],
    ],
  }) })
  const response = await worker.fetch(incoming(), env)
  assert.equal(response.status, 409)
  assert.equal(await response.text(), '{"code":"version_conflict"}')
  assert.deepEqual(response.headers.getSetCookie(), cookies)
  assert.equal(response.headers.get('cache-control'), 'no-store')
  for (const name of ['cdn-cache-control', 'cloudflare-cdn-cache-control', 'surrogate-control', 'content-length', 'content-encoding']) {
    assert.equal(response.headers.get(name), null)
  }
})

test('upstream redirects are never followed and fixed-origin Location remains on public origin', async () => {
  let calls = 0
  const worker = createWorker({ fetchUpstream: async (request) => {
    calls += 1
    assert.equal(request.redirect, 'manual')
    return new Response(null, { status: 307, headers: { Location: 'https://api.example.test/api/session/?next=1' } })
  } })
  const response = await worker.fetch(incoming('/api/leads/', { method: 'POST', body: '{"name":"Тест"}' }), env)
  assert.equal(response.status, 307)
  assert.equal(response.headers.get('location'), 'https://crm.example.test/api/session/?next=1')
  assert.equal(calls, 1)
})

test('cross-origin redirects do not cause a browser to forward mutation bodies elsewhere', async () => {
  for (const location of ['https://evil.test/collect', '//evil.test/collect', 'javascript:alert(1)']) {
    let calls = 0
    const worker = createWorker({ fetchUpstream: async () => {
      calls += 1
      return new Response(null, { status: 307, headers: { Location: location } })
    } })
    const response = await worker.fetch(incoming(), env)
    assert.equal(response.status, 502)
    assert.equal((await response.json()).code, 'upstream_unavailable')
    assert.equal(response.headers.get('location'), null)
    assert.equal(calls, 1)
  }
})

test('a fixed-origin redirect with a double-slash path keeps the public origin', async () => {
  const worker = createWorker({ fetchUpstream: async () => new Response(null, {
    status: 307,
    headers: { Location: 'https://api.example.test//evil.test/collect?q=1' },
  }) })
  const response = await worker.fetch(incoming(), env)
  assert.equal(response.status, 307)
  assert.equal(response.headers.get('location'), 'https://crm.example.test//evil.test/collect?q=1')
})

test('relative redirect Location resolves against the requested API path', async () => {
  const worker = createWorker({ fetchUpstream: async () => new Response(null, {
    status: 302,
    headers: { Location: '../session/?next=1' },
  }) })
  const response = await worker.fetch(incoming('/api/leads/'), env)
  assert.equal(response.status, 302)
  assert.equal(response.headers.get('location'), 'https://crm.example.test/api/session/?next=1')
})

test('bad API origin or ingress secret fails closed without contacting an upstream', async () => {
  let calls = 0
  const worker = createWorker({ fetchUpstream: async () => { calls += 1; return new Response('{}') } })
  for (const API_ORIGIN of [undefined, 'http://api.example.test', 'https://api.example.test/path', 'https://api.example.test/?q=1', 'https://user:pass@api.example.test', 'https://api.example.test/#fragment', ' https://api.example.test']) {
    const response = await worker.fetch(incoming(), { ...env, API_ORIGIN })
    assert.equal(response.status, 503)
    assert.equal((await response.json()).code, 'configuration_error')
    assert.equal(response.headers.get('cache-control'), 'no-store')
  }
  for (const INGRESS_SHARED_SECRET of [undefined, '', 'short', 'secret with spaces that is definitely long enough']) {
    const response = await worker.fetch(incoming(), { ...env, INGRESS_SHARED_SECRET })
    assert.equal(response.status, 503)
  }
  assert.equal(calls, 0)
})

test('trusted client IP requires Cloudflare runtime metadata and one valid address', async () => {
  let calls = 0
  const worker = createWorker({ fetchUpstream: async () => { calls += 1; return new Response('{}') } })
  const noRuntime = new Request('https://crm.example.test/api/', { headers: { 'CF-Connecting-IP': '127.0.0.1' } })
  assert.equal((await worker.fetch(noRuntime, env)).status, 503)
  for (const address of ['', 'not-an-ip', '198.51.100.20, 127.0.0.1', '999.0.0.1']) {
    assert.equal((await worker.fetch(incoming('/api/', { headers: { 'CF-Connecting-IP': address } }), env)).status, 503)
  }
  assert.equal(calls, 0)
})

test('valid IPv6 Cloudflare ingress is passed as the verified client address', async () => {
  let captured
  const worker = createWorker({ fetchUpstream: async (request) => {
    captured = request
    assert.equal(request.headers.get('x-leadflow-client-ip'), '2001:db8::20')
    return new Response('{}')
  } })
  const response = await worker.fetch(incoming('/api/', { headers: { 'CF-Connecting-IP': '2001:db8::20' } }), env)
  assert.equal(response.status, 200)
  assert.ok(captured, 'API must contact the upstream instead of static assets')
})

test('upstream network failures return a safe JSON error without provider details', async () => {
  const worker = createWorker({ fetchUpstream: async () => { throw new Error('private provider credentials') } })
  const response = await worker.fetch(incoming('/api/leads/', { method: 'POST', body: '{}' }), env)
  assert.equal(response.status, 502)
  assert.equal(response.headers.get('cache-control'), 'no-store')
  const body = await response.json()
  assert.equal(body.code, 'upstream_unavailable')
  assert.deepEqual(body.field_errors, {})
  assert.match(body.message, /неизвестен/)
  assert.doesNotMatch(JSON.stringify(body), /private provider/)
})

test('a stalled upstream is aborted and returns JSON 502', async () => {
  let upstreamSignal
  const worker = createWorker({ timeoutMs: 10, fetchUpstream: async (request) => {
    upstreamSignal = request.signal
    return new Promise((_resolve, reject) => {
      request.signal.addEventListener('abort', () => reject(request.signal.reason), { once: true })
    })
  } })
  const response = await worker.fetch(incoming(), env)
  assert.equal(response.status, 502)
  assert.equal((await response.json()).code, 'upstream_unavailable')
  assert.equal(upstreamSignal.aborted, true)
})

test('upstream response body failures return JSON 502 instead of a malformed success', async () => {
  const worker = createWorker({ fetchUpstream: async () => new Response(new ReadableStream({
    start(controller) { controller.error(new Error('private upstream error')) },
  })) })
  const response = await worker.fetch(incoming(), env)
  assert.equal(response.status, 502)
  assert.equal((await response.json()).code, 'upstream_unavailable')
})

test('an upstream response exceeding the resource limit is cancelled and returns JSON 502', async () => {
  let cancelled = false
  const worker = createWorker({ maxResponseBytes: 8, fetchUpstream: async () => new Response(new ReadableStream({
    start(controller) {
      controller.enqueue(new TextEncoder().encode('12345678'))
      controller.enqueue(new TextEncoder().encode('9'))
    },
    cancel() { cancelled = true },
  })) })
  const response = await worker.fetch(incoming(), env)
  assert.equal(response.status, 502)
  assert.equal((await response.json()).code, 'upstream_unavailable')
  assert.equal(cancelled, true)
})

test('an upstream body exactly at the resource limit is preserved', async () => {
  const worker = createWorker({ maxResponseBytes: 8, fetchUpstream: async () => new Response('12345678') })
  const response = await worker.fetch(incoming(), env)
  assert.equal(response.status, 200)
  assert.equal(await response.text(), '12345678')
})

test('a stalled response body remains under the upstream timeout', async () => {
  let cancelled = false
  const worker = createWorker({ timeoutMs: 10, fetchUpstream: async () => new Response(new ReadableStream({
    start(controller) { controller.enqueue(new TextEncoder().encode('partial')) },
    cancel() { cancelled = true },
  })) })
  const response = await worker.fetch(incoming(), env)
  assert.equal(response.status, 502)
  assert.equal((await response.json()).code, 'upstream_unavailable')
  assert.equal(cancelled, true)
})

test('HEAD and no-content API responses have no response body', async () => {
  const worker = createWorker({ fetchUpstream: async () => new Response(null, { status: 204 }) })
  const response = await worker.fetch(incoming('/api/logout/', { method: 'HEAD' }), env)
  assert.equal(response.status, 204)
  assert.equal(await response.text(), '')
  assert.equal(response.headers.get('cache-control'), 'no-store')
})

test('non-API requests use static assets even when API configuration is absent', async () => {
  let captured
  const worker = createWorker({ fetchUpstream: async () => assert.fail('No upstream request expected') })
  const assets = { fetch: async (request) => { captured = request; return new Response('<html>SPA</html>') } }
  const request = new Request('https://crm.example.test/leads/42')
  const response = await worker.fetch(request, { ASSETS: assets })
  assert.equal(await response.text(), '<html>SPA</html>')
  assert.equal(captured, request)
})

test('the bare /api path cannot fall through to the SPA', async () => {
  const worker = createWorker({ fetchUpstream: async (request) => {
    assert.equal(request.url, 'https://api.example.test/api')
    return new Response('{"code":"not_found"}', { status: 404 })
  } })
  assert.equal((await worker.fetch(incoming('/api'), env)).status, 404)
})
