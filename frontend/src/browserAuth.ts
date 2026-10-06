import { ApiError } from './api.ts'
import { AUTH_EVENT_KEY, AUTH_LOCK_NAME, AuthController, PENDING_LOGOUT_KEY } from './auth.ts'
import type { AuthEvent } from './auth.ts'

function authEvent(value: unknown): value is AuthEvent {
  return typeof value === 'object' && value !== null && 'id' in value && typeof value.id === 'string'
    && 'type' in value && ['logged-in', 'logged-out', 'logout-requested'].includes(String(value.type))
}

export function createBrowserAuth() {
  let channel: BroadcastChannel | null = null
  const controller = new AuthController({
    storage: {
      getItem: (key) => window.localStorage.getItem(key),
      setItem: (key, value) => window.localStorage.setItem(key, value),
      removeItem: (key) => window.localStorage.removeItem(key),
    },
    fallbackStorage: {
      getItem: (key) => {
        const prefix = `${encodeURIComponent(key)}=`
        const value = document.cookie.split('; ').find(cookie => cookie.startsWith(prefix))
        return value ? decodeURIComponent(value.slice(prefix.length)) : null
      },
      setItem: (key, value) => {
        document.cookie = `${encodeURIComponent(key)}=${encodeURIComponent(value)}; Path=/; Max-Age=172800; SameSite=Lax${location.protocol === 'https:' ? '; Secure' : ''}`
      },
      removeItem: (key) => {
        document.cookie = `${encodeURIComponent(key)}=; Path=/; Max-Age=0; SameSite=Lax${location.protocol === 'https:' ? '; Secure' : ''}`
      },
    },
    publish: (event) => {
      channel?.postMessage(event)
      try { window.localStorage.setItem(AUTH_EVENT_KEY, JSON.stringify(event)) } catch { /* BroadcastChannel still works. */ }
    },
    exclusive: (task) => {
      if (!navigator.locks) return Promise.reject(new ApiError(503, 'unsupported_browser', 'Откройте CRM в актуальной версии браузера.'))
      return navigator.locks.request(AUTH_LOCK_NAME, task)
    },
  })

  const connect = () => {
    controller.activate()
    if (typeof BroadcastChannel !== 'undefined') {
      channel = new BroadcastChannel('leadflow.auth')
      channel.onmessage = (event: MessageEvent<unknown>) => {
        if (authEvent(event.data)) void controller.handleEvent(event.data)
      }
    }
    const storage = (event: StorageEvent) => {
      if (event.key === PENDING_LOGOUT_KEY && event.newValue) {
        void controller.handleEvent({ type: 'logout-requested', id: event.newValue })
      } else if (event.key === AUTH_EVENT_KEY && event.newValue && !channel) {
        try {
          const value: unknown = JSON.parse(event.newValue)
          if (authEvent(value)) void controller.handleEvent(value)
        } catch { /* Ignore unrelated or malformed local events. */ }
      }
    }
    const refresh = () => {
      controller.checkExpiry()
      if (document.visibilityState === 'visible') void controller.refresh()
    }
    const poll = setInterval(() => {
      const kind = controller.state.kind
      if (kind === 'authenticated' || kind === 'logout-pending' || kind === 'checking') refresh()
    }, 5000)
    window.addEventListener('storage', storage)
    window.addEventListener('focus', refresh)
    window.addEventListener('online', refresh)
    window.addEventListener('pageshow', refresh)
    document.addEventListener('visibilitychange', refresh)
    void controller.refresh()
    return () => {
      window.removeEventListener('storage', storage)
      window.removeEventListener('focus', refresh)
      window.removeEventListener('online', refresh)
      window.removeEventListener('pageshow', refresh)
      document.removeEventListener('visibilitychange', refresh)
      clearInterval(poll)
      channel?.close()
      channel = null
      controller.dispose()
    }
  }
  return { controller, connect }
}
