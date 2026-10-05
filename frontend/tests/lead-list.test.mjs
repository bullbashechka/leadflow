import assert from 'node:assert/strict'
import { test } from 'node:test'
import { LeadListController } from '../src/leadList.ts'

const settle = () => new Promise((resolve) => setImmediate(resolve))
const lead = (index, overrides = {}) => ({
  id: String(index).padStart(8, '0') + '-0000-0000-0000-000000000000',
  name: `Заявка ${index}`, contacts: [{ type: 'email', value: 'test@example.com' }],
  request: 'Тестовая заявка', note: '', version: 1, is_demo: false, arrival_sequence: index,
  source: 'telegram_bot', status: 'new', tags: [],
  created_at: new Date(Date.UTC(2026, 9, 5, 12, 0, index)).toISOString(), ...overrides,
})

function fixture(initial = []) {
  const server = { leads: initial, calls: [], available: true, now: 1000, present: true, wait: null, beforeRead: null, visibleSequence: undefined }
  let tick = null
  const controller = new LeadListController({
    read: async (filters, signal) => {
      server.calls.push({ filters, signal })
      await server.beforeRead?.(filters)
      if (server.wait) await server.wait
      if (!server.available) throw new Error('Нет связи')
      const terms = filters.query?.toLocaleLowerCase().split(/\s+/).filter(Boolean) ?? []
      const matching = server.leads.filter((item) => (filters.tagId === undefined
        || item.tags.some((tag) => tag.id === filters.tagId))
        && (filters.status === undefined || item.status === filters.status)
        && terms.every((term) => [item.name, item.request, ...item.contacts.map((contact) => contact.value)]
          .some((value) => value.toLocaleLowerCase().includes(term))))
      const found = filters.beforeSequence !== undefined
        ? matching.findIndex((item) => item.arrival_sequence < filters.beforeSequence)
        : filters.beforeId ? matching.findIndex((item) => item.id === filters.beforeId) + 1 : 0
      const start = found < 0 ? matching.length : found
      const results = matching.slice(start, start + (filters.limit ?? 50))
      return {
        count: matching.length,
        new_count: filters.sinceSequence === undefined ? 0 : matching.filter((item) => item.arrival_sequence > filters.sinceSequence
          && !(filters.excludeIds ?? []).includes(item.id)).length,
        latest_sequence: Math.max(0, ...server.leads.map((item) => item.arrival_sequence)),
        results,
        next: start + results.length < matching.length ? '/next' : null,
        previous: null,
      }
    },
    failureMessage: (error) => error.message,
    canPresent: () => server.present,
    visibleSequence: () => server.visibleSequence,
    now: () => server.now,
    schedule: (callback) => { tick = callback; return () => { tick = null } },
  })
  return { controller, server, tick: async () => { server.now += 5000; tick?.(); await settle() } }
}

test('a visible authenticated workspace discovers new leads on its next five-second tick', async () => {
  const { controller, server, tick } = fixture([lead(1)])
  await controller.setEnabled(true)
  assert.equal(controller.getSnapshot().count, 1)
  server.leads.unshift(lead(2))
  await tick()
  assert.deepEqual(controller.getSnapshot().leads.map((item) => item.id), [lead(2).id, lead(1).id])
  assert.equal(controller.getSnapshot().count, 2)
  assert.equal(server.calls.length, 2)
  await controller.setEnabled(false)
})

test('a filtered lead that stops matching is replaced when the filtered count stays equal', async () => {
  const website = { id: 1, name: 'Сайт', is_system: true }
  const leaving = lead(3, { status: 'new', tags: [website] })
  const remaining = lead(2, { status: 'new', tags: [website] })
  const entering = lead(4, { status: 'closed', tags: [website] })
  const { controller, server } = fixture([entering, leaving, remaining])
  await controller.setEnabled(true)
  await controller.changeFilters({ tagId: 1, status: 'new' })
  assert.deepEqual(controller.getSnapshot().leads.map((item) => item.id), [leaving.id, remaining.id])

  entering.status = 'new'
  leaving.status = 'closed'
  controller.reflectLead(leaving)
  await controller.refresh()

  assert.equal(controller.getSnapshot().count, 2)
  assert.deepEqual(controller.getSnapshot().leads.map((item) => item.id), [entering.id, remaining.id])
  await controller.setEnabled(false)
})

test('deleting a loaded lead fills its visible slot from the current filtered results', async () => {
  const initial = Array.from({ length: 60 }, (_, index) => lead(60 - index))
  const removed = initial[0]
  const { controller, server } = fixture(initial)
  await controller.setEnabled(true)
  server.leads = server.leads.filter((item) => item.id !== removed.id)
  controller.removeLead(removed.id)
  await controller.refresh()

  assert.equal(controller.getSnapshot().count, 59)
  assert.equal(controller.getSnapshot().leads.length, 50)
  assert.equal(controller.getSnapshot().leads.at(-1).id, lead(10).id)
  await controller.setEnabled(false)
})

test('reading below the beginning keeps rendered rows and counts incoming leads', async () => {
  const { controller, server, tick } = fixture([lead(1)])
  await controller.setEnabled(true)
  server.present = false
  server.leads.unshift(lead(2), lead(3))
  await tick()
  assert.deepEqual(controller.getSnapshot().leads.map((item) => item.id), [lead(1).id])
  assert.equal(controller.getSnapshot().newCount, 2)
  assert.equal(controller.getSnapshot().count, 3)
  await controller.setEnabled(false)
})

test('a failed background read keeps data, the last success time and an error until recovery', async () => {
  const { controller, server } = fixture([lead(1)])
  await controller.setEnabled(true)
  server.available = false
  server.now = 2000
  await assert.doesNotReject(controller.refresh())
  assert.equal(controller.getSnapshot().leads[0].id, lead(1).id)
  assert.equal(controller.getSnapshot().lastUpdated, 1000)
  assert.equal(controller.getSnapshot().error, 'Нет связи')
  server.available = true
  server.now = 3000
  await controller.refresh()
  assert.equal(controller.getSnapshot().error, null)
  assert.equal(controller.getSnapshot().lastUpdated, 3000)
  await controller.setEnabled(false)
})

test('periodic checks coalesce while a read is pending', async () => {
  const { controller, server, tick } = fixture([lead(1)])
  await controller.setEnabled(true)
  let release
  server.wait = new Promise((resolve) => { release = resolve })
  await tick()
  await tick()
  assert.equal(server.calls.length, 2)
  server.wait = null
  release()
  await settle()
  assert.equal(server.calls.length, 3)
  await controller.setEnabled(false)
})

test('accepting more than a page of arrivals preserves all previously loaded leads', async () => {
  const old = Array.from({ length: 150 }, (_, index) => lead(150 - index))
  const { controller, server, tick } = fixture(old)
  await controller.setEnabled(true)
  await controller.loadMore()
  await controller.loadMore()
  assert.equal(controller.getSnapshot().leads.length, 150)
  server.present = false
  const added = Array.from({ length: 80 }, (_, index) => lead(230 - index))
  server.leads = [...added, ...old]
  await tick()
  assert.equal(controller.getSnapshot().newCount, 80)
  assert.equal(controller.getSnapshot().leads.length, 150)
  const beforeAccept = server.calls.length
  server.present = true
  await controller.acceptNew()
  assert.deepEqual(controller.getSnapshot().leads.map((item) => item.id), server.leads.map((item) => item.id))
  assert.equal(controller.getSnapshot().newCount, 0)
  assert.equal(controller.getSnapshot().hasMore, false)
  assert.equal(server.calls.length - beforeAccept, Math.ceil(server.leads.length / 50))
  await controller.setEnabled(false)
})

test('scrolling away while the new prefix loads keeps the original rows and counter', async () => {
  const old = Array.from({ length: 50 }, (_, index) => lead(50 - index))
  const { controller, server, tick } = fixture(old)
  await controller.setEnabled(true)
  server.leads = [...Array.from({ length: 80 }, (_, index) => lead(130 - index)), ...old]
  server.beforeRead = () => { server.present = false }
  await tick()
  assert.deepEqual(controller.getSnapshot().leads.map((item) => item.id), old.map((item) => item.id))
  assert.equal(controller.getSnapshot().newCount, 80)
  await controller.setEnabled(false)
})

test('an unchanged background head does not discard loaded pages', async () => {
  const { controller, tick } = fixture(Array.from({ length: 80 }, (_, index) => lead(80 - index)))
  await controller.setEnabled(true)
  await controller.loadMore()
  await tick()
  assert.equal(controller.getSnapshot().leads.length, 80)
  await controller.setEnabled(false)
})

test('a filter keeps only matching arrivals and a late response cannot overwrite the next filter', async () => {
  const tags = [{ id: 1, name: 'Сайт', is_system: true }, { id: 2, name: 'Реклама', is_system: true }]
  const { controller, server, tick } = fixture([lead(1, { tags: [tags[0]] }), lead(2, { tags: [tags[1]] })])
  await controller.setEnabled(true)
  await controller.changeFilter(1)
  server.present = false
  server.leads.unshift(lead(3, { tags: [tags[1]] }))
  await tick()
  assert.equal(controller.getSnapshot().newCount, 0)
  let release
  server.wait = new Promise((resolve) => { release = resolve })
  await tick()
  const oldRequest = server.calls.at(-1)
  server.wait = null
  await controller.changeFilter(2)
  release()
  await settle()
  assert.equal(oldRequest.signal.aborted, true)
  assert.deepEqual(controller.getSnapshot().leads.map((item) => item.id), [lead(3).id, lead(2).id])
  assert.equal(controller.getSnapshot().newCount, 0)
  await controller.setEnabled(false)
})

test('a failed filter change cannot leave old rows under the new filter', async () => {
  const { controller, server } = fixture([lead(1)])
  await controller.setEnabled(true)
  server.available = false
  await controller.changeFilter(1)
  assert.deepEqual(controller.getSnapshot().leads, [])
  assert.equal(controller.getSnapshot().error, 'Нет связи')
  assert.equal(controller.getSnapshot().lastUpdated, null)
  assert.equal(server.calls.at(-1).filters.tagId, 1)
  await controller.setEnabled(false)
})

test('own creation excludes only its receipt even when an earlier check already counted it', async () => {
  const { controller, server, tick } = fixture([lead(1)])
  await controller.setEnabled(true)
  server.present = false
  const own = lead(3, { source: 'manual' })
  server.leads.unshift(own, lead(2))
  await tick()
  assert.equal(controller.getSnapshot().newCount, 2)
  controller.ownCreated(own)
  await settle()
  assert.equal(controller.getSnapshot().newCount, 1)
  controller.ownCreated(own)
  await settle()
  assert.equal(controller.getSnapshot().newCount, 1)
  server.leads.unshift(lead(4))
  await tick()
  assert.equal(controller.getSnapshot().newCount, 2)
  await controller.setEnabled(false)
})

test('locking or hiding a workspace aborts reads and ignores their late results', async () => {
  const { controller, server, tick } = fixture([lead(1)])
  await controller.setEnabled(true)
  let release
  server.wait = new Promise((resolve) => { release = resolve })
  server.leads.unshift(lead(2))
  await tick()
  await controller.setEnabled(false)
  assert.equal(server.calls.at(-1).signal.aborted, true)
  release()
  await settle()
  assert.equal(controller.getSnapshot().count, 1)
  const calls = server.calls.length
  await tick()
  assert.equal(server.calls.length, calls)
  server.wait = null
  await controller.setEnabled(true)
  assert.equal(controller.getSnapshot().count, 2)
  await controller.setEnabled(false)
})

test('loading an older page does not clear a failed freshness check', async () => {
  const { controller, server } = fixture(Array.from({ length: 80 }, (_, index) => lead(80 - index)))
  await controller.setEnabled(true)
  server.beforeRead = (filters) => { if (!filters.beforeSequence) throw new Error('Проверка обновления не удалась') }
  await controller.refresh()
  assert.equal(controller.getSnapshot().error, 'Проверка обновления не удалась')
  await controller.loadMore()
  assert.equal(controller.getSnapshot().leads.length, 80)
  assert.equal(controller.getSnapshot().error, 'Проверка обновления не удалась')
  await controller.setEnabled(false)
})

test('the server order retains timestamp precision and separate identical submissions', async () => {
  const sameFields = { name: 'Одинаковое имя', request: 'Одинаковый запрос' }
  const older = lead(2, { ...sameFields, created_at: '2026-10-05T12:00:00.123001Z' })
  const newer = lead(1, { ...sameFields, arrival_sequence: 3, created_at: '2026-10-05T12:00:00.123999Z' })
  const { controller, server, tick } = fixture([older])
  await controller.setEnabled(true)
  server.leads.unshift(newer)
  await tick()
  assert.deepEqual(controller.getSnapshot().leads.map((item) => item.id), [newer.id, older.id])
  await controller.setEnabled(false)
})

test('an own receipt cancels an older page without leaving its loading indicator stuck', async () => {
  const { controller, server } = fixture(Array.from({ length: 80 }, (_, index) => lead(80 - index)))
  await controller.setEnabled(true)
  let release
  server.wait = new Promise((resolve) => { release = resolve })
  const more = controller.loadMore()
  await settle()
  server.present = false
  server.wait = null
  const own = lead(81, { source: 'manual' })
  server.leads.unshift(own)
  controller.ownCreated(own)
  await settle()
  release()
  await more
  assert.equal(controller.getSnapshot().moreLoading, false)
  await controller.loadMore()
  assert.equal(controller.getSnapshot().leads.length, 80)
  await controller.setEnabled(false)
})

test('creating before the initial list response excludes the own receipt but counts a foreign arrival', async () => {
  let release
  let first = true
  let present = true
  const rows = [lead(1)]
  const controller = new LeadListController({
    read: () => {
      if (first) {
        first = false
        return new Promise((resolve) => { release = () => resolve({ count: 1, results: [lead(1)], next: null }) })
      }
      return Promise.resolve({ count: rows.length, results: [...rows], next: null })
    },
    failureMessage: (error) => error.message,
    canPresent: () => present,
    schedule: () => () => {},
  })
  const initial = controller.setEnabled(true)
  present = false
  const own = lead(2, { source: 'manual' })
  rows.unshift(own)
  controller.ownCreated(own)
  await settle()
  release()
  await initial
  rows.unshift(lead(3))
  await controller.refresh()
  assert.equal(controller.getSnapshot().newCount, 1)
  await controller.setEnabled(false)
})

test('leaving the list during acceptance retains unseen arrivals until the list is shown again', async () => {
  const { controller, server } = fixture([lead(1)])
  await controller.setEnabled(true)
  server.leads.unshift(lead(2))
  let release
  server.wait = new Promise((resolve) => { release = resolve })
  const acceptance = controller.acceptNew()
  server.present = false
  server.leads.unshift(lead(3))
  release()
  await acceptance
  assert.equal(controller.getSnapshot().newCount, 2)
  assert.deepEqual(controller.getSnapshot().leads.map((item) => item.id), [lead(1).id])
  await controller.setEnabled(false)
})


test('poll replaces unchanged-count rows and refreshes loaded tail versions', async () => {
  const { controller, server } = fixture(Array.from({ length: 80 }, (_, i) => lead(80 - i)))
  await controller.setEnabled(true)
  await controller.loadMore()
  server.visibleSequence = 10
  server.leads = server.leads.map(item => item.arrival_sequence === 10
    ? { ...item, name: 'Updated tail', version: 2 } : item)
  server.leads = [lead(81), ...server.leads.filter(item => item.arrival_sequence !== 80)]
  await controller.refresh()
  assert.equal(controller.getSnapshot().leads[0].id, lead(81).id)
  assert.equal(controller.getSnapshot().leads.find(item => item.arrival_sequence === 10).name, 'Updated tail')
  await controller.setEnabled(false)
})

test('below-top polling updates existing rows but defers incoming rows', async () => {
  const { controller, server } = fixture([lead(2), lead(1)])
  await controller.setEnabled(true)
  server.present = false
  server.leads = [lead(3), lead(2, { name: 'Updated', version: 2 }), lead(1)]
  await controller.refresh()
  assert.deepEqual(controller.getSnapshot().leads.map(item => item.id), [lead(2).id, lead(1).id])
  assert.equal(controller.getSnapshot().leads[0].name, 'Updated')
  assert.equal(controller.getSnapshot().newCount, 1)
  await controller.setEnabled(false)
})


test('background polling reads at most two windows even after 10000 rows are loaded', async () => {
  const { controller, server } = fixture(Array.from({ length: 10000 }, (_, i) => lead(10000 - i)))
  await controller.setEnabled(true)
  while (controller.getSnapshot().hasMore) await controller.loadMore()
  server.present = false
  server.visibleSequence = 1000
  server.leads = server.leads.map(item => item.arrival_sequence === 990
    ? { ...item, version: 2, note: 'Updated visible note' } : item)
  const before = server.calls.length
  await controller.refresh()
  assert.equal(server.calls.length - before, 2)
  assert.ok(server.calls.slice(before).every(call => call.filters.limit <= 100))
  assert.equal(controller.getSnapshot().leads.find(item => item.arrival_sequence === 990).note, 'Updated visible note')
  assert.equal(controller.getSnapshot().leads.length, 10000)
  await controller.setEnabled(false)
})

test('a confirmed mutation cancels an older list read and ignores an older replay version', async () => {
  const { controller, server } = fixture([lead(1)])
  await controller.setEnabled(true)
  let release
  server.wait = new Promise(resolve => { release = resolve })
  const pending = controller.refresh()
  const read = server.calls.at(-1)
  controller.reflectLead(lead(1, { name: 'Confirmed edit', version: 3 }))
  assert.equal(read.signal.aborted, true)
  controller.reflectLead(lead(1, { name: 'Old replay', version: 2 }))
  release()
  await pending
  assert.equal(controller.getSnapshot().leads[0].name, 'Confirmed edit')
  await controller.setEnabled(false)
})

test('own arrival cannot acknowledge an incomplete foreign burst at the head limit', async () => {
  const { controller, server } = fixture(Array.from({ length: 50 }, (_, i) => lead(50 - i)))
  await controller.setEnabled(true)
  const own = lead(151, { source: 'manual' })
  server.leads = Array.from({ length: 151 }, (_, i) => lead(151 - i))
  server.leads[0] = own
  controller.ownCreated(own)
  await settle()
  assert.equal(controller.getSnapshot().newCount, 100)
  await controller.acceptNew()
  assert.deepEqual(controller.getSnapshot().leads.map(item => item.id), server.leads.map(item => item.id))
  assert.equal(controller.getSnapshot().newCount, 0)
  await controller.setEnabled(false)
})
