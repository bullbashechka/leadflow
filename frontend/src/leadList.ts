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
  visibleSequence?: () => number | undefined
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
  private loadedRows = 0
  private needsReconciliation = false
  private acceptedCount = 0
  private displayedCount = 0
  private tagId: number | undefined
  private query = ''
  private status: LeadListOptions['status']
  private acceptedSequence: number | undefined
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
      const excludedIds = [...this.ownIds].slice(0, 100)
      const page = await this.options.read({
        limit: this.loaded && !this.needsReconciliation ? 100 : 50,
        tagId: this.tagId,
        query: this.query,
        status: this.status,
        sinceSequence: this.acceptedSequence,
        excludeIds: excludedIds,
      }, request.signal)
      if (request.signal.aborted) return
      const update: Partial<LeadListState> = {
        count: page.count, error: null, lastUpdated: (this.options.now ?? Date.now)(),
        newCount: page.new_count ?? Math.max(0, page.count - this.acceptedCount - this.ownIds.size),
      }
      if (this.loaded && !this.needsReconciliation) {
        const upper = this.options.visibleSequence?.()
        const windows: Array<{ page: LeadPage; upper: number }> = [{ page, upper: Infinity }]
        if (upper !== undefined && page.next && upper <= (page.results.at(-1)?.arrival_sequence ?? Infinity)) {
          const visible = await this.options.read({ limit: 100, tagId: this.tagId, query: this.query,
            status: this.status, beforeSequence: upper + 1 }, request.signal)
          if (request.signal.aborted) return
          windows.push({ page: visible, upper: upper + 1 })
        }
        const present = this.options.canPresent?.() ?? true
        const existing = new Set(this.state.leads.map(lead => lead.id))
        let rows = this.state.leads
        for (const window of windows) {
          const lower = window.page.next ? window.page.results.at(-1)?.arrival_sequence ?? window.upper : 0
          rows = rows.filter(lead => !(lead.arrival_sequence < window.upper && lead.arrival_sequence >= lower))
          rows = [...rows, ...window.page.results.filter(lead => present
            || this.acceptedSequence === undefined || lead.arrival_sequence <= this.acceptedSequence || existing.has(lead.id))]
        }
        const merged = [...new Map(rows.map(lead => [lead.id, lead])).values()]
          .sort((first, second) => second.arrival_sequence - first.arrival_sequence)
        const arrivals = merged.filter(lead => !existing.has(lead.id)
          && lead.arrival_sequence > (this.acceptedSequence ?? Infinity)).length
        update.leads = merged.slice(0, this.loadedRows + (present ? arrivals : 0))
        update.hasMore = update.leads.length < page.count
        this.loadedRows = update.leads.length
        const refreshed = new Map(windows.flatMap(window => window.page.results).map(lead => [lead.id, lead]))
        const foreignArrivals = [...refreshed.values()].filter(lead =>
          lead.arrival_sequence > (this.acceptedSequence ?? Infinity) && !excludedIds.includes(lead.id)).length
        if (present && foreignArrivals >= (page.new_count ?? 0)) {
          this.displayedCount = page.count
          this.acceptedCount = page.count
          this.acceptedSequence = page.latest_sequence ?? this.acceptedSequence
          this.ownIds.clear()
          update.newCount = 0
        }
        this.update(update)
        return
      }
      const target = this.loaded
        ? Math.min(page.count, this.loadedRows + Math.max(0, page.count - this.displayedCount, page.new_count ?? 0))
        : 50
      // Read the loaded prefix again: equal counts do not imply equal IDs or versions.
      const rows = new Map(page.results.map(lead => [lead.id, lead]))
      let tail = page
      while (rows.size < target && tail.next) {
        const last = [...rows.values()].at(-1)
        tail = await this.options.read({ limit: 50, tagId: this.tagId, query: this.query,
          status: this.status, beforeSequence: last?.arrival_sequence,
          beforeId: last?.arrival_sequence === undefined ? last?.id : undefined }, request.signal)
        if (request.signal.aborted) return
        const previousSize = rows.size
        for (const lead of tail.results) rows.set(lead.id, lead)
        if (rows.size === previousSize) throw new Error('Список не удалось загрузить полностью.')
      }
      // Scrolling can change while the rest of the prefix is loading.
      const present = !this.loaded || (this.options.canPresent?.() ?? true)
      if (present) {
        update.leads = [...rows.values()]
        update.hasMore = tail.next !== null
        this.loadedRows = rows.size
        this.displayedCount = page.count
        this.acceptedCount = page.count
        this.acceptedSequence = page.latest_sequence ?? this.acceptedSequence
        this.ownIds.clear()
        update.newCount = 0
      } else {
        // Keep incoming rows behind the explicit acceptance action while refreshing old rows.
        update.leads = [...rows.values()].filter(lead =>
          this.acceptedSequence === undefined || lead.arrival_sequence <= this.acceptedSequence
          || this.state.leads.some(existing => existing.id === lead.id)).slice(0, this.loadedRows)
      }
      this.needsReconciliation = false
      this.loaded = true
      this.update(update)
    } catch (error) {
      if (!request.signal.aborted) this.update({ error: this.options.failureMessage(error) })
    }
  }

  changeFilter = async (tagId: number | undefined) => {
    await this.changeFilters({ tagId })
  }

  changeFilters = async (filters: {
    tagId?: number | undefined
    query?: string
    status?: LeadListOptions['status']
  }) => {
    this.cancelRead()
    if ('tagId' in filters) this.tagId = filters.tagId
    if ('query' in filters) this.query = filters.query?.trim() ?? ''
    if ('status' in filters) this.status = filters.status
    this.loaded = false
    this.loadedRows = 0
    this.needsReconciliation = false
    this.acceptedCount = 0
    this.displayedCount = 0
    this.acceptedSequence = undefined
    this.ownIds.clear()
    this.update({ leads: [], count: 0, newCount: 0, hasMore: false, lastUpdated: null,
      loading: this.enabled, moreLoading: false,
      error: this.enabled ? null : 'Нет связи с сервером. Повторите загрузку.', moreError: null })
    await this.refresh()
  }

  acceptNew = () => { this.needsReconciliation = true; return this.refresh() }

  reconcile = () => {
    this.needsReconciliation = true
    return this.refresh()
  }

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
        const last = this.state.leads.at(-1)
        const page = await this.options.read({
          limit: 50,
          tagId: this.tagId,
          query: this.query,
          status: this.status,
          beforeSequence: last?.arrival_sequence,
          beforeId: last?.arrival_sequence === undefined ? last?.id : undefined,
        }, request.signal)
        if (request.signal.aborted) return
        const rows = new Map(this.state.leads.map((lead) => [lead.id, lead]))
        for (const lead of page.results) rows.set(lead.id, lead)
        const leads = [...rows.values()]
        this.loadedRows = leads.length
        this.update({ leads, hasMore: page.next !== null, moreError: null })
      } catch (error) {
        if (!request.signal.aborted) this.update({ moreError: this.options.failureMessage(error) })
      }
    })
  }

  reflectLead = (lead: Lead) => {
    const matches = (this.tagId === undefined || lead.tags.some((tag) => tag.id === this.tagId))
      && (this.status === undefined || lead.status === this.status)
      && this.query.split(/\s+/).filter(Boolean).every((term) => {
        const normalized = term.toLocaleLowerCase()
        const digits = term.replace(/\D/g, '')
        return [lead.name, lead.request, ...lead.contacts.map(({ value }) => value)]
          .some((value) => value.toLocaleLowerCase().includes(normalized)
            || (digits.length > 0 && value.replace(/\D/g, '').includes(digits)))
      })
    const current = this.state.leads.find(row => row.id === lead.id)
    if (current && current.version > lead.version) return
    const existing = Boolean(current)
    if (!existing && !matches) return
    this.cancelRead()
    if (existing !== matches) this.needsReconciliation = true
    const rows = new Map(this.state.leads.map((row) => [row.id, row]))
    if (matches) rows.set(lead.id, lead)
    else rows.delete(lead.id)
    this.update({ leads: [...rows.values()].sort((first, second) => second.arrival_sequence - first.arrival_sequence) })
  }

  removeLead = (leadId: string, matchesCurrentFilter = this.state.leads.some((lead) => lead.id === leadId)) => {
    this.cancelRead()
    this.needsReconciliation = true
    const leads = this.state.leads.filter((lead) => lead.id !== leadId)
    if (matchesCurrentFilter) {
      this.displayedCount = Math.max(0, this.displayedCount - 1)
      this.acceptedCount = Math.max(0, this.acceptedCount - 1)
    }
    this.update({ leads, count: matchesCurrentFilter ? Math.max(0, this.state.count - 1) : this.state.count })
    void this.refresh()
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
