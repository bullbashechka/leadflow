export type RequestOptions = { signal?: AbortSignal; timeoutMs?: number }

export type Session = {
  authenticated: boolean
  expires_at: string | null
  server_time: string
  csrf_token: string
}

export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly fieldErrors: Record<string, string[]>
  readonly retryAfter: number | null

  constructor(status: number, code: string, message: string, fieldErrors: Record<string, string[]> = {}, retryAfter: number | null = null) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.fieldErrors = fieldErrors
    this.retryAfter = retryAfter
  }
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

export async function requestApi(path: string, init: RequestInit = {}, { signal, timeoutMs = 8000 }: RequestOptions = {}): Promise<unknown> {
  const timeout = new AbortController()
  const timer = setTimeout(() => timeout.abort(new DOMException('Request timed out', 'TimeoutError')), timeoutMs)
  const combined = signal ? AbortSignal.any([signal, timeout.signal]) : timeout.signal
  try {
    const response = await fetch(path, { ...init, signal: combined, credentials: 'same-origin', cache: 'no-store' })
    if (response.status === 204 && response.ok) return null
    const data: unknown = await response.json().catch(() => null)
    if (!response.ok) {
      const fields: Record<string, string[]> = {}
      if (isObject(data) && isObject(data.field_errors)) {
        for (const [field, errors] of Object.entries(data.field_errors)) {
          if (Array.isArray(errors) && errors.every((error) => typeof error === 'string')) fields[field] = errors
        }
      }
      const retry = Number(response.headers.get('Retry-After'))
      throw new ApiError(response.status,
        isObject(data) && typeof data.code === 'string' ? data.code : 'service_unavailable',
        isObject(data) && typeof data.message === 'string' ? data.message : 'Не удалось связаться с сервером.',
        fields, Number.isFinite(retry) && retry > 0 ? retry : null)
    }
    if (data === null) throw new Error('Unexpected API response')
    return data
  } finally {
    clearTimeout(timer)
  }
}

function parseSession(data: unknown): Session {
  if (!isObject(data) || typeof data.authenticated !== 'boolean'
    || typeof data.server_time !== 'string' || !Number.isFinite(Date.parse(data.server_time))
    || typeof data.csrf_token !== 'string' || !data.csrf_token
    || (data.authenticated
      ? typeof data.expires_at !== 'string' || !Number.isFinite(Date.parse(data.expires_at))
      : data.expires_at !== null)) throw new Error('Unexpected session response')
  return data as Session
}

export async function getSession(options?: RequestOptions): Promise<Session> {
  return parseSession(await requestApi('/api/auth/session/', {}, options))
}

export async function login(password: string, csrfToken: string): Promise<Session> {
  return parseSession(await requestApi('/api/auth/login/', {
    method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
    body: JSON.stringify({ password }),
  }))
}

export async function logout(csrfToken: string): Promise<void> {
  await requestApi('/api/auth/logout/', {
    method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
    body: '{}',
  })
}

export async function checkHealth({ signal, timeoutMs = 8000 }: RequestOptions = {}): Promise<void> {
  const data = await requestApi('/api/health/', {}, { signal, timeoutMs })
  if (!isObject(data) || data.status !== 'ok') {
    throw new Error('Unexpected API response')
  }
}
