import { ApiError, getSession, login, logout } from './api.ts'
import type { Session } from './api.ts'

export const PENDING_LOGOUT_KEY = 'leadflow.logout-pending'
export const AUTH_EVENT_KEY = 'leadflow.auth-event'
export const AUTH_LOCK_NAME = 'leadflow.auth'

export type AuthEvent = { type: 'logout-requested' | 'logged-out' | 'logged-in'; id: string }
export type AuthState = {
  kind: 'checking' | 'anonymous' | 'authenticated' | 'locked' | 'logout-pending'
  hasOpened: boolean
  offline: boolean
  busy: boolean
  error: string | null
  expiresAt: string | null
}

type AuthApi = {
  getSession: () => Promise<Session>
  login: (password: string, token: string) => Promise<Session>
  logout: (token: string) => Promise<void>
}
type AuthOptions = {
  api?: AuthApi
  storage: Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>
  publish: (event: AuthEvent) => void
  exclusive: <T>(task: () => Promise<T>) => Promise<T>
  now?: () => number
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    return error.retryAfter ? `${error.message} Повторите через ${error.retryAfter} сек.` : error.message
  }
  return 'Не удалось связаться с сервером. Проверьте соединение и повторите.'
}

export class AccessInterruptedError extends Error {
  constructor() {
    super('CRM access interrupted; an already sent operation may have completed')
    this.name = 'AccessInterruptedError'
  }
}

export class AuthController {
  state: AuthState = { kind: 'checking', hasOpened: false, offline: false, busy: false, error: null, expiresAt: null }
  private readonly api: AuthApi
  private readonly options: AuthOptions
  private readonly now: () => number
  private readonly listeners = new Set<() => void>()
  private deadline = 0
  private csrfToken = ''
  private generation = 0
  private pendingInMemory: string | null = null
  private refreshRequest: Promise<void> | null = null
  private expiryTimer: ReturnType<typeof setTimeout> | null = null
  private disposed = false

  constructor(options: AuthOptions) {
    this.options = options
    this.api = options.api ?? { getSession, login, logout }
    this.now = options.now ?? (() => performance.now())
    if (this.pendingLogout()) this.state = { ...this.state, kind: 'logout-pending' }
  }

  subscribe = (listener: () => void) => {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  getSnapshot = () => this.state

  private update(changes: Partial<AuthState>) {
    if (this.disposed) return
    this.state = { ...this.state, ...changes }
    for (const listener of this.listeners) listener()
  }

  private pendingLogout(): string | null {
    try {
      return this.options.storage.getItem(PENDING_LOGOUT_KEY) ?? this.pendingInMemory
    } catch {
      return this.pendingInMemory
    }
  }

  private broadcast(type: AuthEvent['type']) {
    this.options.publish({ type, id: crypto.randomUUID() })
  }

  private lock(message: string | null = null) {
    this.generation++
    this.deadline = 0
    this.csrfToken = ''
    if (this.expiryTimer) clearTimeout(this.expiryTimer)
    this.update({ kind: this.state.hasOpened ? 'locked' : 'anonymous', error: message, expiresAt: null })
  }

  private applySession(session: Session, started: number) {
    this.csrfToken = session.csrf_token
    if (!session.authenticated || !session.expires_at) {
      this.lock(this.state.hasOpened ? 'Вход завершён. Введите пароль для продолжения.' : null)
      this.csrfToken = session.csrf_token
      this.update({ offline: false })
      return
    }
    const remaining = Date.parse(session.expires_at) - Date.parse(session.server_time) - (this.now() - started)
    if (remaining <= 0) {
      this.lock('Срок входа истёк. Введите пароль для продолжения.')
      return
    }
    this.deadline = this.now() + remaining
    if (this.expiryTimer) clearTimeout(this.expiryTimer)
    this.expiryTimer = setTimeout(() => this.checkExpiry(), remaining)
    this.update({ kind: 'authenticated', hasOpened: true, offline: false, error: null, expiresAt: session.expires_at })
  }

  checkExpiry() {
    if (this.state.kind === 'authenticated' && this.now() >= this.deadline) {
      this.lock('Срок входа истёк. Введите пароль для продолжения.')
    }
  }

  private async completeLogout() {
    const marker = this.pendingLogout()
    if (!marker) return
    this.update({ kind: 'logout-pending', busy: true })
    try {
      const session = await this.api.getSession()
      if (session.authenticated) await this.api.logout(session.csrf_token)
      if (this.pendingLogout() !== marker) return
      try { this.options.storage.removeItem(PENDING_LOGOUT_KEY) } catch { /* Keep the in-memory block until confirmation. */ }
      this.pendingInMemory = null
      if (this.pendingLogout()) throw new Error('Cannot clear pending logout')
      this.lock()
      this.update({ kind: 'anonymous', offline: false, error: null })
      this.broadcast('logged-out')
    } catch (error) {
      this.update({ kind: 'logout-pending', offline: true, error: errorMessage(error) })
    } finally {
      this.update({ busy: false })
    }
  }

  refresh(): Promise<void> {
    this.checkExpiry()
    if (this.pendingLogout() && this.state.kind !== 'logout-pending') {
      this.lock()
      this.update({ kind: 'logout-pending' })
    }
    if (this.refreshRequest) return this.refreshRequest
    const generation = this.generation
    const request = this.options.exclusive(async () => {
      if (this.pendingLogout()) {
        await this.completeLogout()
        return
      }
      if (generation !== this.generation || this.disposed) return
      const started = this.now()
      try {
        const session = await this.api.getSession()
        if (generation !== this.generation || this.pendingLogout()) return
        this.applySession(session, started)
      } catch (error) {
        if (generation !== this.generation || this.pendingLogout()) return
        this.checkExpiry()
        this.update({ offline: true, error: errorMessage(error) })
      }
    }).catch((error) => {
      this.update({ error: errorMessage(error) })
    }).finally(() => { if (this.refreshRequest === request) this.refreshRequest = null })
    this.refreshRequest = request
    return request
  }

  async logIn(password: string): Promise<void> {
    if (this.state.busy || this.pendingLogout()) return
    this.update({ busy: true, error: null })
    const generation = this.generation
    try {
      await this.options.exclusive(async () => {
        if (this.pendingLogout() || generation !== this.generation) return
        let started = this.now()
        const current = await this.api.getSession()
        if (this.pendingLogout() || generation !== this.generation) return
        let session = current
        if (!current.authenticated) {
          started = this.now()
          session = await this.api.login(password, current.csrf_token)
        }
        if (this.pendingLogout() || generation !== this.generation) return
        this.applySession(session, started)
        if (this.state.kind === 'authenticated') this.broadcast('logged-in')
      })
    } catch (error) {
      if (generation === this.generation && !this.pendingLogout()) {
        this.update({ error: errorMessage(error), offline: !(error instanceof ApiError) })
      }
    } finally {
      this.update({ busy: false })
    }
  }

  logOut(): Promise<void> {
    const marker = this.pendingLogout() ?? crypto.randomUUID()
    this.pendingInMemory = marker
    try { this.options.storage.setItem(PENDING_LOGOUT_KEY, marker) } catch { /* Still hide immediately. */ }
    this.lock()
    this.update({ kind: 'logout-pending', error: null })
    this.broadcast('logout-requested')
    return this.options.exclusive(() => this.completeLogout()).catch((error) => {
      this.update({ kind: 'logout-pending', error: errorMessage(error) })
    })
  }

  async handleEvent(event: AuthEvent): Promise<void> {
    if (event.type === 'logout-requested') {
      // Use the shared marker when available; retain the request locally otherwise.
      if (!this.pendingLogout()) this.pendingInMemory = event.id
      this.lock()
      this.update({ kind: 'logout-pending' })
      return
    }
    if (event.type === 'logged-out') {
      this.pendingInMemory = null
      this.lock()
      this.update({ kind: 'anonymous' })
      return
    }
    await this.refresh()
  }

  async runWithAccess<T>(operation: (csrfToken: string) => Promise<T>): Promise<T> {
    this.checkExpiry()
    if (this.state.kind !== 'authenticated' || this.state.offline || this.pendingLogout()) throw new AccessInterruptedError()
    const generation = this.generation
    try {
      const result = await operation(this.csrfToken)
      this.checkExpiry()
      if (generation !== this.generation || this.pendingLogout()) throw new AccessInterruptedError()
      return result
    } catch (error) {
      if (generation === this.generation && error instanceof ApiError) {
        if (error.status === 401) this.lock('Войдите для продолжения. Введённые данные сохранены в открытой форме.')
        else if (error.code === 'csrf_failed') await this.refresh()
      }
      throw error
    }
  }

  dispose() {
    this.disposed = true
    this.generation++
    this.refreshRequest = null
    if (this.expiryTimer) clearTimeout(this.expiryTimer)
  }

  activate() {
    this.disposed = false
    this.checkExpiry()
    if (this.state.kind === 'authenticated') {
      this.expiryTimer = setTimeout(() => this.checkExpiry(), Math.max(0, this.deadline - this.now()))
    }
  }
}
