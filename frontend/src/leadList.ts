import type { Lead, LeadListOptions, LeadPage } from './api.ts'

export type LeadListState = {
  leads: Lead[]
  count: number
  newCount: number
  loading: boolean
  moreLoading: boolean
  hasMore: boolean
  error: string | null
  moreError: string | null
  lastUpdated: number | null
}

type Options = {
  read: (filters: LeadListOptions, signal: AbortSignal) => Promise<LeadPage>
  failureMessage: (error: unknown) => string
  canPresent?: () => boolean
  beforeUpdate?: () => void
  now?: () => number
  schedule?: (callback: () => void) => () => void
}

export class LeadListController {
  private state: LeadListState = {
    leads: [], count: 0, newCount: 0, loading: true, moreLoading: false,
    hasMore: false, error: null, moreError: null, lastUpdated: null,
  }
  private listeners = new Set<() => void>()
  private options: Options
  private cancelTick: (() => void) | null = null
  private enabled = false
  private active: { kind: 'head' | 'more'; request: AbortController; promise: Promise<void> } | null = null
  private pendingRefresh = false
  private pendingMore = false
  private loaded = false
  private acceptedCount = 0
  private displayedCount = 0
  private tagId: number | undefined
  private ownIds = new Set<string>()

  constructor(options: Options) { this.options = options }

  getSnapshot = () => this.state
  subscribe = (listener: () => void) => {
    this.listeners.add(listener)
    return () => { this.listeners.delete(listener) }
  }

  async setEnabled(enabled: boolean, unavailableMessage?: string) {
    if (!enabled && unavailableMessage && unavailableMessage !== this.state.error) this.update({ error: unavailableMessage })
    if (this.enabled === enabled) return
    this.enabled = enabled
    if (!enabled) {
      this.cancelTick?.()
      this.cancelTick = null
      this.cancelRead()
      this.update({ loading: false, moreLoading: false })
      return
    }
    if (!this.cancelTick) {
      const schedule = this.options.schedule ?? ((callback: () => void) => {
        const timer = setInterval(callback, 5000)
        return () => clearInterval(timer)
      })
      this.cancelTick = schedule(() => { void this.refresh() })
    }
    await this.refresh()
  }

  refresh = (queue = true): Promise<void> => {
    if (!this.enabled) return Promise.resolve()
    if (this.active) {
      if (queue) this.pendingRefresh = true
      return this.active.promise
    }
    return this.run('head', (request) => this.fetchHead(request))
  }

  private run(kind: 'head' | 'more', operation: (request: AbortController) => Promise<void>) {
    const task = { kind, request: new AbortController(), promise: Promise.resolve() }
    this.active = task
    task.promise = operation(task.request).finally(() => {
      if (this.active !== task) return
      this.active = null
      this.update(kind === 'head' ? { loading: false } : { moreLoading: false })
      if (this.pendingMore) {
        this.pendingMore = false
        void this.loadMore()
      } else if (this.pendingRefresh) {
        this.pendingRefresh = false
        void this.refresh()
      }
    })
    return task.promise
  }

  private async fetchHead(request: AbortController) {
    this.update({ loading: true })
    try {
      const page = await this.options.read({ limit: 50, tagId: this.tagId }, request.signal)
      if (request.signal.aborted) return
      const present = !this.loaded || (this.options.canPresent?.() ?? true)
      const update: Partial<LeadListState> = {
        count: page.count, error: null, lastUpdated: (this.options.now ?? Date.now)(),
        newCount: Math.max(0, page.count - this.acceptedCount - this.ownIds.size),
      }
      if (present) {
        let rowsToPresent: Lead[] | undefined
        let hasMore = this.state.hasMore
        if (!this.loaded || page.count !== this.displayedCount) {
          const target = this.loaded
            ? Math.min(page.count, this.state.leads.length + Math.max(0, page.count - this.displayedCount)) : 50
          const lastId = this.state.leads.at(-1)?.id
          const existingIds = new Set(this.state.leads.map((lead) => lead.id))
          const rows = new Map(page.results.map((lead) => [lead.id, lead]))
          const canJoin = () => this.loaded && page.count >= this.displayedCount
            && [...rows.keys()].filter((id) => !existingIds.has(id)).length === page.count - this.displayedCount
          let tail = page
          while (!canJoin() && tail.next && (rows.size < target || (lastId !== undefined && !rows.has(lastId)))) {
            const beforeId = [...rows.keys()].at(-1)
            tail = await this.options.read({ limit: 50, tagId: this.tagId, beforeId }, request.signal)
            if (request.signal.aborted) return
            const previousSize = rows.size
            for (const lead of tail.results) rows.set(lead.id, lead)
            if (rows.size === previousSize) throw new Error('Список не удалось загрузить полностью.')
          }
          if (canJoin()) {
            for (const lead of this.state.leads) if (!rows.has(lead.id)) rows.set(lead.id, lead)
            hasMore = rows.size < page.count
          } else hasMore = tail.next !== null
          rowsToPresent = [...rows.values()]
        }
        // A user can start scrolling while the missing prefix is loading.
        if (!this.loaded || (this.options.canPresent?.() ?? true)) {
          if (rowsToPresent) update.leads = rowsToPresent
          update.hasMore = hasMore
          this.displayedCount = page.count
          this.acceptedCount = page.count
          this.ownIds.clear()
          update.newCount = 0
        }
      }
      this.loaded = true
      this.update(update)
    } catch (error) {
      if (!request.signal.aborted) this.update({ error: this.options.failureMessage(error) })
    }
  }

  changeFilter = async (tagId: number | undefined) => {
    this.cancelRead()
    this.tagId = tagId
    this.loaded = false
    this.acceptedCount = 0
    this.displayedCount = 0
    this.ownIds.clear()
    this.update({ leads: [], count: 0, newCount: 0, hasMore: false, lastUpdated: null,
      loading: this.enabled, moreLoading: false,
      error: this.enabled ? null : 'Нет связи с сервером. Повторите загрузку.', moreError: null })
    await this.refresh()
  }

  acceptNew = () => this.refresh()

  ownCreated = (lead: Lead) => {
    if (!this.state.leads.some((row) => row.id === lead.id)
      && (this.tagId === undefined || lead.tags.some((tag) => tag.id === this.tagId))) {
      this.ownIds.add(lead.id)
      this.cancelRead()
      void this.refresh()
    }
  }

  loadMore = (): Promise<void> => {
    if (!this.enabled || !this.state.hasMore || !this.state.leads.length) return Promise.resolve()
    if (this.active) {
      if (this.active.kind === 'more') return this.active.promise
      this.pendingMore = true
      const task = this.active
      return task.promise.then(() => task.request.signal.aborted ? undefined : this.active?.promise)
    }
    return this.run('more', async (request) => {
      this.update({ moreLoading: true })
      try {
        const page = await this.options.read({ limit: 50, tagId: this.tagId,
          beforeId: this.state.leads.at(-1)?.id }, request.signal)
        if (request.signal.aborted) return
        const rows = new Map(this.state.leads.map((lead) => [lead.id, lead]))
        for (const lead of page.results) rows.set(lead.id, lead)
        this.update({ leads: [...rows.values()], hasMore: page.next !== null, moreError: null })
      } catch (error) {
        if (!request.signal.aborted) this.update({ moreError: this.options.failureMessage(error) })
      }
    })
  }

  private cancelRead() {
    const kind = this.active?.kind
    this.active?.request.abort()
    this.active = null
    this.pendingRefresh = false
    this.pendingMore = false
    if (kind) this.update(kind === 'head' ? { loading: false } : { moreLoading: false })
  }

  private update(update: Partial<LeadListState>) {
    this.options.beforeUpdate?.()
    this.state = { ...this.state, ...update }
    for (const listener of this.listeners) listener()
  }
}
