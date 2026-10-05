import assert from 'node:assert/strict'
import { test } from 'node:test'
import { AuthController } from '../src/auth.ts'
import { ApiError } from '../src/api.ts'

const duration = 48 * 60 * 60 * 1000
const pendingKey = 'leadflow.logout-pending'

function environment(t) {
  let now = 0
  let authenticated = true
  let unavailable = false
  let expiry = duration
  let queue = Promise.resolve()
  const storageData = new Map()
  const storage = {
    getItem: (key) => storageData.get(key) ?? null,
    setItem: (key, value) => storageData.set(key, value),
    removeItem: (key) => storageData.delete(key),
  }
  const calls = []
  const events = []
  const snapshot = () => ({
    authenticated, expires_at: authenticated ? new Date(expiry).toISOString() : null,
    server_time: new Date(now).toISOString(), csrf_token: 'current-token',
  })
  const api = {
    getSession: async () => {
      calls.push('get')
      if (unavailable) throw new TypeError('network unavailable')
      return snapshot()
    },
    login: async () => {
      calls.push('login')
      authenticated = true
      expiry = now + duration
      return snapshot()
    },
    logout: async () => { calls.push('logout'); authenticated = false },
  }
  const exclusive = (task) => {
    const result = queue.then(task)
    queue = result.catch(() => {})
    return result
  }
  const create = () => {
    const controller = new AuthController({ api, storage, publish: (event) => events.push(event), exclusive, now: () => now })
    t.after(() => controller.dispose())
    return controller
  }
  return {
    create, api, storage, calls, events, snapshot,
    advance: (ms) => { now += ms },
    network: (works) => { unavailable = !works },
    access: (value) => { authenticated = value },
  }
}

test('discovery opens valid access and does not retain passwords in storage', async (t) => {
  const env = environment(t)
  const controller = env.create()
  await controller.refresh()
  assert.equal(controller.state.kind, 'authenticated')
  assert.equal(controller.state.hasOpened, true)
  assert.equal(env.storage.getItem(pendingKey), null)
})

test('offline access stays visible until the known absolute expiry', async (t) => {
  const env = environment(t)
  const controller = env.create()
  await controller.refresh()
  env.network(false)
  env.advance(60_000)
  await controller.refresh()
  assert.equal(controller.state.kind, 'authenticated')
  assert.equal(controller.state.offline, true)
  env.advance(duration)
  controller.checkExpiry()
  assert.equal(controller.state.kind, 'locked')
  await assert.rejects(controller.runWithAccess(async () => 'saved'))
})

test('first visit without a server response never opens protected content', async (t) => {
  const env = environment(t)
  env.network(false)
  const controller = env.create()
  await controller.refresh()
  assert.equal(controller.state.kind, 'checking')
  assert.equal(controller.state.hasOpened, false)
  assert.ok(controller.state.error)
})

test('offline logout hides immediately and remains pending after reload', async (t) => {
  const env = environment(t)
  const controller = env.create()
  await controller.refresh()
  env.network(false)
  const result = controller.logOut()
  assert.equal(controller.state.kind, 'logout-pending')
  assert.ok(env.storage.getItem(pendingKey))
  await result
  const reloaded = env.create()
  await reloaded.refresh()
  assert.equal(reloaded.state.kind, 'logout-pending')
  await reloaded.logIn('password')
  assert.equal(env.calls.filter((value) => value === 'login').length, 0)
  env.network(true)
  await reloaded.refresh()
  assert.equal(reloaded.state.kind, 'anonymous')
  assert.equal(env.storage.getItem(pendingKey), null)
  assert.equal(env.calls.filter((value) => value === 'logout').length, 1)
})

test('lost logout response is checked rather than announced as success', async (t) => {
  const env = environment(t)
  const controller = env.create()
  await controller.refresh()
  env.api.logout = async () => { env.access(false); throw new TypeError('response lost') }
  await controller.logOut()
  assert.equal(controller.state.kind, 'logout-pending')
  assert.ok(env.storage.getItem(pendingKey))
  await controller.refresh()
  assert.equal(controller.state.kind, 'anonymous')
  assert.equal(env.storage.getItem(pendingKey), null)
})

test('logout notification hides another tab; login requires its own server check', async (t) => {
  const env = environment(t)
  const first = env.create()
  const second = env.create()
  await first.refresh()
  await second.refresh()
  env.network(false)
  await first.logOut()
  await second.handleEvent(env.events.at(-1))
  assert.equal(second.state.kind, 'logout-pending')
  env.network(true)
  await first.refresh()
  await first.logIn('password')
  await second.handleEvent(env.events.at(-1))
  assert.equal(second.state.kind, 'authenticated')
  assert.equal(env.calls.filter((value) => value === 'login').length, 1)
})

test('another tab completes pending logout when storage is unavailable and the sender closes', async (t) => {
  const env = environment(t)
  for (const method of ['getItem', 'setItem', 'removeItem']) {
    t.mock.method(env.storage, method, () => { throw new Error('Storage unavailable') })
  }
  const first = env.create()
  const second = env.create()
  await first.refresh()
  await second.refresh()
  env.network(false)
  await first.logOut()
  await second.handleEvent(env.events.at(-1))
  first.dispose()
  assert.equal(second.state.kind, 'logout-pending')

  env.network(true)
  const logout = env.api.logout
  env.api.logout = async () => { throw new TypeError('Logout unavailable') }
  await second.refresh()
  assert.equal(second.state.kind, 'logout-pending')
  await assert.rejects(second.runWithAccess(async () => 'saved'))
  await second.logIn('password')
  assert.equal(env.calls.filter((value) => value === 'login').length, 0)

  env.api.logout = logout
  await second.refresh()
  assert.equal(second.state.kind, 'anonymous')
  assert.equal(env.calls.filter((value) => value === 'logout').length, 1)
  await second.logIn('password')
  assert.equal(second.state.kind, 'authenticated')
})

test('authentication failures lock access without replaying mutations', async (t) => {
  const env = environment(t)
  const controller = env.create()
  await controller.refresh()
  let attempts = 0
  await assert.rejects(controller.runWithAccess(async (csrfToken) => {
    assert.equal(csrfToken, 'current-token')
    attempts++
    throw new ApiError(401, 'authentication_required', 'Войдите.')
  }))
  assert.equal(controller.state.kind, 'locked')
  env.access(false)
  await controller.logIn('password')
  assert.equal(controller.state.kind, 'authenticated')
  assert.equal(attempts, 1)
})

test('CSRF rejection refreshes access but does not repeat the operation', async (t) => {
  const env = environment(t)
  const controller = env.create()
  await controller.refresh()
  let attempts = 0
  await assert.rejects(controller.runWithAccess(async () => {
    attempts++
    throw new ApiError(403, 'csrf_failed', 'Обновите доступ.')
  }))
  assert.equal(controller.state.kind, 'authenticated')
  assert.equal(attempts, 1)
  assert.equal(env.calls.filter((value) => value === 'get').length, 2)
})

test('stale data replies after logout cannot appear as success', async (t) => {
  const env = environment(t)
  const controller = env.create()
  await controller.refresh()
  let resolve
  const request = controller.runWithAccess(() => new Promise((done) => { resolve = done }))
  await controller.logOut()
  resolve({ private: true })
  await assert.rejects(request, /access/i)
  assert.notEqual(controller.state.kind, 'authenticated')
})

test('a late session reply cannot unlock a pending logout', async (t) => {
  const env = environment(t)
  const controller = env.create()
  await controller.refresh()
  let resolve
  let started
  const began = new Promise((done) => { started = done })
  env.api.getSession = () => new Promise((done) => { resolve = done; started() })
  const checking = controller.refresh()
  await began
  const exiting = controller.logOut()
  env.api.getSession = async () => env.snapshot()
  resolve(env.snapshot())
  await checking
  assert.notEqual(controller.state.kind, 'authenticated')
  await exiting
  assert.equal(controller.state.kind, 'anonymous')
})
