export type RequestOptions = { signal?: AbortSignal; timeoutMs?: number }

export type Session = {
  authenticated: boolean
  expires_at: string | null
  server_time: string
  csrf_token: string
}

export type ContactType = 'phone' | 'email' | 'telegram'
export type LeadSource = 'manual' | 'telegram_bot'
export type LeadStatus = 'new' | 'in_progress' | 'closed'

export type Tag = {
  id: number
  name: string
  is_system: boolean
}

export type Contact = {
  type: ContactType
  value: string
}

export type Lead = {
  id: string
  name: string
  contacts: Contact[]
  request: string
  source: LeadSource
  status: LeadStatus
  created_at: string
  tags: Tag[]
}

export type LeadPage = {
  count: number
  next: string | null
  previous: string | null
  results: Lead[]
}

export type LeadSubmission = {
  submission_id: string
  name: string
  contacts: string[]
  request: string
  tag_ids: number[]
}

export type LeadListOptions = {
  tagId?: number
  beforeId?: string
  limit?: number
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

function parseTag(value: unknown): Tag {
  if (!isObject(value) || !Number.isSafeInteger(value.id) || (value.id as number) < 1
    || typeof value.name !== 'string' || typeof value.is_system !== 'boolean') {
    throw new Error('Unexpected tag response')
  }
  return value as Tag
}

function parseLead(value: unknown): Lead {
  if (!isObject(value) || typeof value.id !== 'string' || !value.id
    || typeof value.name !== 'string' || typeof value.request !== 'string'
    || !['manual', 'telegram_bot'].includes(String(value.source))
    || !['new', 'in_progress', 'closed'].includes(String(value.status))
    || typeof value.created_at !== 'string' || !Number.isFinite(Date.parse(value.created_at))
    || !Array.isArray(value.contacts) || !Array.isArray(value.tags)) {
    throw new Error('Unexpected lead response')
  }
  const contacts = value.contacts.map((item) => {
    if (!isObject(item) || !['phone', 'email', 'telegram'].includes(String(item.type))
      || typeof item.value !== 'string') throw new Error('Unexpected contact response')
    return item as Contact
  })
  return {
    ...value,
    contacts,
    tags: value.tags.map(parseTag),
  } as Lead
}

function parseLeadPage(value: unknown): LeadPage {
  if (!isObject(value) || !Number.isSafeInteger(value.count) || (value.count as number) < 0
    || !(value.next === null || typeof value.next === 'string')
    || !(value.previous === null || typeof value.previous === 'string')
    || !Array.isArray(value.results)) throw new Error('Unexpected lead page response')
  return { ...value, results: value.results.map(parseLead) } as LeadPage
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

export async function getTags(options?: RequestOptions): Promise<Tag[]> {
  const data = await requestApi('/api/tags/', {}, options)
  if (!isObject(data) || !Array.isArray(data.results)) throw new Error('Unexpected tags response')
  return data.results.map(parseTag)
}

export async function getLeads(filters: LeadListOptions = {}, options?: RequestOptions): Promise<LeadPage> {
  const query = new URLSearchParams()
  if (filters.tagId !== undefined) query.set('tag_id', String(filters.tagId))
  query.set('limit', String(filters.limit ?? 50))
  if (filters.beforeId) query.set('before_id', filters.beforeId)
  const suffix = query.size ? `?${query}` : ''
  return parseLeadPage(await requestApi(`/api/leads/${suffix}`, {}, options))
}

export async function getLead(id: string, options?: RequestOptions): Promise<Lead> {
  return parseLead(await requestApi(`/api/leads/${encodeURIComponent(id)}/`, {}, options))
}

export async function createLead(
  submission: LeadSubmission,
  csrfToken: string,
  options?: RequestOptions,
): Promise<Lead> {
  return parseLead(await requestApi('/api/leads/', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
    body: JSON.stringify(submission),
  }, options))
}
