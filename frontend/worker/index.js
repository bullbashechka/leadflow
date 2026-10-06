const FORWARDED_HEADERS = [
  'accept',
  'accept-language',
  'content-type',
  'cookie',
  'origin',
  'referer',
  'x-csrftoken',
]

const MAX_UPSTREAM_RESPONSE_BYTES = 8 * 1024 * 1024
const MAX_REQUEST_BYTES = 128 * 1024
const SECURITY_HEADERS = {
  'Strict-Transport-Security': 'max-age=31536000; includeSubDomains',
  'X-Content-Type-Options': 'nosniff',
  'X-Frame-Options': 'DENY',
  'Referrer-Policy': 'strict-origin-when-cross-origin',
  'Permissions-Policy': 'camera=(), microphone=(), geolocation=()',
  'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; connect-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; form-action 'self'",
}

class BodyTooLarge extends Error {}

function configuration(env) {
  if (typeof env.API_ORIGIN !== 'string'
    || typeof env.INGRESS_SHARED_SECRET !== 'string'
    || !/^[\x21-\x7e]{32,}$/.test(env.INGRESS_SHARED_SECRET)) return null
  try {
    const origin = new URL(env.API_ORIGIN)
    if (origin.protocol !== 'https:' || origin.username || origin.password
      || ![origin.origin, `${origin.origin}/`].includes(env.API_ORIGIN)) return null
    return origin.origin
  } catch {
    return null
  }
}

function cloudflareClientIp(request) {
  // Cloudflare supplies cf and overwrites CF-Connecting-IP at external ingress.
  // This handler must run at that boundary, not behind an arbitrary HTTP proxy.
  if (!request.cf || typeof request.cf !== 'object') return null
  const address = request.headers.get('cf-connecting-ip')
  if (!address) return null
  if (/^(?:0|[1-9]\d{0,2})(?:\.(?:0|[1-9]\d{0,2})){3}$/.test(address)) {
    return address.split('.').every((part) => Number(part) <= 255) ? address : null
  }
  if (address.includes(':') && /^[\da-f:.]+$/i.test(address)) {
    try {
      new URL(`http://[${address}]/`)
      return address
    } catch {
      return null
    }
  }
  return null
}

function errorResponse(status, code, message) {
  return new Response(JSON.stringify({ code, message, field_errors: {} }), {
    status,
    headers: { ...SECURITY_HEADERS, 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' },
  })
}

function responseHeaders(upstream, upstreamUrl, publicUrl) {
  const headers = new Headers(upstream.headers)
  // getSetCookie preserves individual values, including commas in Expires.
  headers.delete('set-cookie')
  for (const cookie of upstream.headers.getSetCookie()) headers.append('set-cookie', cookie)
  headers.set('cache-control', 'no-store')
  // Fetch exposes decoded bytes; forwarding transport encoding/length would lie.
  for (const name of ['cdn-cache-control', 'cloudflare-cdn-cache-control', 'surrogate-control', 'content-encoding', 'content-length']) {
    headers.delete(name)
  }
  if (upstream.status >= 300 && upstream.status < 400 && headers.has('location')) {
    const target = new URL(headers.get('location'), upstreamUrl)
    // Do not let the browser replay a POST body against a different origin.
    if (target.origin !== upstreamUrl.origin) throw new Error('Unsafe upstream redirect')
    const redirected = new URL(publicUrl.origin)
    redirected.pathname = target.pathname
    redirected.search = target.search
    redirected.hash = target.hash
    headers.set('location', redirected.href)
  }
  return headers
}

async function boundedBody(upstream, maxBytes, signal) {
  signal.throwIfAborted()
  if (Number(upstream.headers.get('content-length')) > maxBytes) {
    await upstream.body?.cancel()
    throw new BodyTooLarge('Body too large')
  }
  if (!upstream.body) return null
  const reader = upstream.body.getReader()
  const chunks = []
  let length = 0
  const cancel = () => { void reader.cancel(signal.reason).catch(() => {}) }
  signal.addEventListener('abort', cancel, { once: true })
  try {
    while (true) {
      const { done, value } = await reader.read()
      signal.throwIfAborted()
      if (done) break
      length += value.byteLength
      if (length > maxBytes) throw new BodyTooLarge('Body too large')
      chunks.push(value)
    }
    const body = new Uint8Array(length)
    let offset = 0
    for (const chunk of chunks) {
      body.set(chunk, offset)
      offset += chunk.byteLength
    }
    return body
  } catch (error) {
    await reader.cancel().catch(() => {})
    throw error
  } finally {
    signal.removeEventListener('abort', cancel)
    reader.releaseLock()
  }
}

export function createWorker({
  fetchUpstream = globalThis.fetch,
  timeoutMs = 15000,
  maxResponseBytes = MAX_UPSTREAM_RESPONSE_BYTES,
} = {}) {
  return {
    async fetch(request, env) {
      const publicUrl = new URL(request.url)
      if (publicUrl.pathname !== '/api' && !publicUrl.pathname.startsWith('/api/')) {
        return env.ASSETS.fetch(request)
      }
      const origin = configuration(env)
      const clientIp = cloudflareClientIp(request)
      if (!origin || !clientIp) {
        return errorResponse(503, 'configuration_error', 'Сервис временно недоступен.')
      }

      const headers = new Headers()
      // An allowlist excludes every client-supplied routing and ingress header.
      for (const name of FORWARDED_HEADERS) {
        if (request.headers.has(name)) headers.set(name, request.headers.get(name))
      }
      headers.set('accept-encoding', 'identity')
      headers.set('x-leadflow-ingress-secret', env.INGRESS_SHARED_SECRET)
      headers.set('x-leadflow-client-ip', clientIp)
      const controller = new AbortController()
      const signal = AbortSignal.any([request.signal, controller.signal])
      let timer
      let forwarded = false
      try {
        // Bound request upload and upstream response under the same deadline.
        const timeout = new Promise((_resolve, reject) => {
          timer = setTimeout(() => {
            const error = new Error('Upstream timeout')
            controller.abort(error)
            reject(error)
          }, timeoutMs)
        })
        const proxied = (async () => {
          const upstreamUrl = new URL(origin)
          // Assign components instead of resolving a path beginning with //.
          upstreamUrl.pathname = publicUrl.pathname
          upstreamUrl.search = publicUrl.search
          const hasBody = request.method !== 'GET' && request.method !== 'HEAD'
          const requestBody = hasBody ? await boundedBody(request, MAX_REQUEST_BYTES, signal) : null
          const upstreamRequest = new Request(upstreamUrl, {
            method: request.method,
            headers,
            body: requestBody,
            ...(hasBody ? { duplex: 'half' } : {}),
            redirect: 'manual',
            cache: 'no-store',
            signal,
          })
          forwarded = true
          const upstream = await fetchUpstream(upstreamRequest, {
            cf: { cacheEverything: false, cacheTtl: 0 },
          })
          const outgoingHeaders = responseHeaders(upstream, upstreamUrl, publicUrl)
          const hasResponseBody = request.method !== 'HEAD' && ![204, 205, 304].includes(upstream.status)
          // Finish reading under the deadline so a broken body returns JSON 502.
          const body = hasResponseBody ? await boundedBody(upstream, maxResponseBytes, signal) : null
          return new Response(body, { status: upstream.status, statusText: upstream.statusText, headers: outgoingHeaders })
        })()
        return await Promise.race([proxied, timeout])
      } catch (error) {
        controller.abort()
        if (!forwarded && error instanceof BodyTooLarge) {
          return errorResponse(413, 'request_too_large', 'Запрос слишком большой.')
        }
        const mutation = !['GET', 'HEAD', 'OPTIONS'].includes(request.method)
        return errorResponse(502, 'upstream_unavailable', mutation
          ? 'Сервис временно недоступен. Результат операции неизвестен. Повторите ту же операцию.'
          : 'Сервис временно недоступен. Повторите запрос позже.')
      } finally {
        clearTimeout(timer)
      }
    },
  }
}

export default createWorker()
