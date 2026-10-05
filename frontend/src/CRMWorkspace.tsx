import { useCallback, useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore } from 'react'
import { Alert, Button, Card, Empty, Flex, Select, Skeleton, Typography } from 'antd'
import { getLead, getLeads, getTags } from './api'
import type { Lead, Tag } from './api'
import { useCRMAccess } from './AuthBoundary'
import { LeadListController } from './leadList'
import { LeadListView } from './LeadListView'
import { LeadDetails } from './LeadDetails'
import { ManualLeadForm } from './ManualLeadForm'
import { getFailureMessage } from './crmErrors'
import { formatExactDate, timezoneLabel } from './leadPresentation'

const BOT_URL = import.meta.env.VITE_TELEGRAM_BOT_URL
type CRMMode = { kind: 'list' } | { kind: 'create' } | { kind: 'detail'; leadId: string }
type ScrollAnchor = { leadId: string | null; top: number; scrollY: number }

function useMediaQuery(query: string) {
  const subscribe = useCallback((listener: () => void) => {
    const media = window.matchMedia(query)
    media.addEventListener('change', listener)
    return () => media.removeEventListener('change', listener)
  }, [query])
  return useSyncExternalStore(subscribe, () => window.matchMedia(query).matches)
}

function telegramBotUrl() {
  if (typeof BOT_URL !== 'string' || !BOT_URL) return null
  try {
    const url = new URL(BOT_URL)
    if (url.protocol !== 'https:' || url.hostname !== 't.me' || url.search || url.hash
      || !/^\/[A-Za-z][A-Za-z0-9_]{4,31}\/?$/.test(url.pathname)) return null
    return url.href
  } catch { return null }
}

function visibleRows() {
  return [...document.querySelectorAll<HTMLElement>('[data-lead-id]')]
    .filter(element => element.getClientRects().length > 0)
}

function inViewport(element: HTMLElement) {
  const rect = element.getBoundingClientRect()
  return rect.bottom > 0 && rect.top < window.innerHeight
}

export function CRMWorkspace() {
  const { controller, state: accessState } = useCRMAccess()
  const wide = useMediaQuery('(min-width: 1200px)')
  const mobile = useMediaQuery('(max-width: 767px)')
  const [mode, setMode] = useState<CRMMode>({ kind: 'list' })
  const [tags, setTags] = useState<Tag[]>([])
  const [tagsLoading, setTagsLoading] = useState(true)
  const [tagsError, setTagsError] = useState<string | null>(null)
  const [selectedTag, setSelectedTag] = useState<number | undefined>()
  const [expandedContacts, setExpandedContacts] = useState<Set<string>>(() => new Set())
  const [detail, setDetail] = useState<Lead | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailError, setDetailError] = useState<string | null>(null)
  const [hiddenByFilter, setHiddenByFilter] = useState(false)
  const [newSubmission, setNewSubmission] = useState(0)
  const tagsController = useRef<AbortController | null>(null)
  const detailController = useRef<AbortController | null>(null)
  const pageAnchor = useRef<ScrollAnchor>({ leadId: null, top: 0, scrollY: 0 })
  const viewHeading = useRef<HTMLHeadingElement>(null)
  const detailHeading = useRef<HTMLHeadingElement>(null)
  const listStart = useRef<HTMLDivElement>(null)
  const restoreAnchor = useRef(false)
  const focusAfterReturn = useRef<string | true | null>(null)
  const atTopRef = useRef(true)
  const listVisible = mode.kind === 'list' || (wide && mode.kind === 'detail')
  const listVisibleRef = useRef(listVisible)
  listVisibleRef.current = listVisible

  const captureScrollAnchor = useCallback(() => {
    const row = visibleRows().find(inViewport)
    pageAnchor.current = { leadId: row?.dataset.leadId ?? null,
      top: row?.getBoundingClientRect().top ?? 0, scrollY: window.scrollY }
  }, [])

  const restoreScrollAnchor = useCallback(() => {
    const anchor = pageAnchor.current
    const target = visibleRows().find(row => row.dataset.leadId === anchor.leadId)
    if (target) window.scrollBy({ top: target.getBoundingClientRect().top - anchor.top })
    else window.scrollTo({ top: anchor.scrollY })
  }, [])

  const [list] = useState(() => new LeadListController({
    read: (filters, signal) => controller.runWithAccess(() => getLeads(filters, { signal })),
    failureMessage: getFailureMessage,
    canPresent: () => listVisibleRef.current && atTopRef.current,
    beforeUpdate: () => {
      if (listVisibleRef.current && !atTopRef.current && !restoreAnchor.current) {
        captureScrollAnchor()
        restoreAnchor.current = true
      }
    },
  }))
  const listState = useSyncExternalStore(list.subscribe, list.getSnapshot)
  const { leads, count, newCount, loading: listLoading, moreLoading, hasMore,
    error: listError, moreError, lastUpdated } = listState
  const botUrl = telegramBotUrl()

  const loadTags = useCallback(async () => {
    tagsController.current?.abort()
    const request = new AbortController()
    tagsController.current = request
    setTagsLoading(true)
    try {
      const result = await controller.runWithAccess(() => getTags({ signal: request.signal }))
      if (!request.signal.aborted) { setTags(result); setTagsError(null) }
    } catch (error) {
      if (!request.signal.aborted) setTagsError(getFailureMessage(error))
    } finally {
      if (!request.signal.aborted) setTagsLoading(false)
    }
  }, [controller])

  useEffect(() => {
    const available = accessState.kind === 'authenticated' && !accessState.offline
    void list.setEnabled(available && document.visibilityState === 'visible',
      accessState.offline ? 'Нет связи с сервером. Проверьте соединение и повторите.' : undefined)
    if (available) void loadTags()
  }, [accessState.kind, accessState.offline, list, loadTags])

  useEffect(() => {
    const refreshOnReturn = () => {
      const visible = document.visibilityState === 'visible'
      const available = controller.state.kind === 'authenticated' && !controller.state.offline
      void list.setEnabled(visible && available)
      if (visible && available) { void list.refresh(false); void loadTags() }
    }
    document.addEventListener('visibilitychange', refreshOnReturn)
    window.addEventListener('focus', refreshOnReturn)
    window.addEventListener('online', refreshOnReturn)
    window.addEventListener('pageshow', refreshOnReturn)
    return () => {
      document.removeEventListener('visibilitychange', refreshOnReturn)
      window.removeEventListener('focus', refreshOnReturn)
      window.removeEventListener('online', refreshOnReturn)
      window.removeEventListener('pageshow', refreshOnReturn)
      void list.setEnabled(false)
      tagsController.current?.abort()
      detailController.current?.abort()
    }
  }, [controller, list, loadTags])

  // Keep the latest reading position, including scrolls made beside the detail panel.
  useEffect(() => {
    if (!listVisible) return
    const updatePosition = () => {
      if (restoreAnchor.current) return
      const atTop = listStart.current ? listStart.current.getBoundingClientRect().top >= -1 : window.scrollY <= 16
      const returnedToTop = atTop && !atTopRef.current
      atTopRef.current = atTop
      captureScrollAnchor()
      if (returnedToTop) void list.acceptNew()
    }
    updatePosition()
    const onResize = () => {
      // Width changes can reflow rows without crossing a responsive breakpoint.
      if (listVisibleRef.current && !atTopRef.current) restoreScrollAnchor()
    }
    window.addEventListener('scroll', updatePosition, { passive: true })
    window.addEventListener('resize', onResize)
    return () => {
      window.removeEventListener('scroll', updatePosition)
      window.removeEventListener('resize', onResize)
    }
  }, [list, listVisible, captureScrollAnchor, restoreScrollAnchor])

  const previousLayout = useRef({ wide, mobile, listVisible })
  useLayoutEffect(() => {
    const previous = previousLayout.current
    const changed = previous.wide !== wide || previous.mobile !== mobile || previous.listVisible !== listVisible
    previousLayout.current = { wide, mobile, listVisible }
    if (!listVisible) {
      if (changed) window.scrollTo({ top: 0 })
      return
    }
    if (restoreAnchor.current || changed) {
      restoreAnchor.current = false
      restoreScrollAnchor()
    }
    if (focusAfterReturn.current) {
      const rows = visibleRows().filter(row => {
        const opener = row.querySelector<HTMLButtonElement>('button[aria-label^="Открыть карточку:"]')
        return opener && inViewport(opener)
      })
      const selected = rows.find(row => row.dataset.leadId === focusAfterReturn.current)
      const opener = (selected ?? rows[0])?.querySelector<HTMLButtonElement>('button[aria-label^="Открыть карточку:"]')
      ;(opener ?? viewHeading.current)?.focus({ preventScroll: true })
      focusAfterReturn.current = null
    }
  }, [listState, listVisible, wide, mobile, mode, restoreScrollAnchor])

  const changeFilter = (value: number | undefined) => {
    detailController.current?.abort()
    restoreAnchor.current = false
    atTopRef.current = true
    pageAnchor.current = { leadId: null, top: 0, scrollY: 0 }
    setMode({ kind: 'list' })
    setSelectedTag(value)
    setExpandedContacts(new Set())
    void list.changeFilter(value)
    window.scrollTo({ top: 0 })
  }

  const showDetail = (lead: Lead, wasHidden = false) => {
    setDetail(lead)
    setDetailError(null)
    setHiddenByFilter(wasHidden)
    setMode({ kind: 'detail', leadId: lead.id })
  }

  const openDetail = (lead: Lead) => {
    captureScrollAnchor()
    restoreAnchor.current = wide
    showDetail(lead)
  }

  useEffect(() => {
    if (mode.kind !== 'detail') return
    detailHeading.current?.focus({ preventScroll: true })
    detailController.current?.abort()
    const request = new AbortController()
    detailController.current = request
    setDetailLoading(true)
    void controller.runWithAccess(() => getLead(mode.leadId, { signal: request.signal })).then(result => {
      if (!request.signal.aborted) { setDetail(result); setDetailError(null) }
    }).catch((error: unknown) => {
      if (!request.signal.aborted) setDetailError(getFailureMessage(error))
    }).finally(() => {
      if (!request.signal.aborted) setDetailLoading(false)
    })
    return () => request.abort()
  }, [controller, mode])

  const returnToList = () => {
    detailController.current?.abort()
    if (listVisible) captureScrollAnchor()
    restoreAnchor.current = true
    focusAfterReturn.current = mode.kind === 'detail' ? mode.leadId : true
    setMode({ kind: 'list' })
    void list.refresh(false)
  }

  const startCreate = () => {
    captureScrollAnchor()
    setMode({ kind: 'create' })
  }

  const finishCreate = (created: Lead) => {
    const hidden = selectedTag !== undefined && !created.tags.some(tag => tag.id === selectedTag)
    list.ownCreated(created)
    setNewSubmission(value => value + 1)
    showDetail(created, hidden)
  }

  const toggleContacts = (id: string) => setExpandedContacts(current => {
    const next = new Set(current)
    if (next.has(id)) next.delete(id)
    else next.add(id)
    return next
  })

  if (mode.kind === 'create') return <ManualLeadForm key={newSubmission} tags={tags} tagsLoading={tagsLoading}
    tagsError={tagsError} offline={accessState.offline} onRetryTags={() => void loadTags()} onExit={returnToList} onCreated={finishCreate} />

  const detailView = mode.kind === 'detail' ? <LeadDetails lead={detail} loading={detailLoading} error={detailError}
    hiddenByFilter={hiddenByFilter} panel={wide} headingRef={detailHeading} onClose={returnToList}
    onRetry={() => setMode({ kind: 'detail', leadId: mode.leadId })} /> : null

  return <Flex vertical gap={24} className="crm-workspace">
    {listVisible && <>
      <Flex wrap align="center" justify="space-between" gap={16} className="crm-titlebar">
        <div>
          <Flex gap={14} align="baseline">
            <Typography.Title level={1} ref={viewHeading} tabIndex={-1} className="crm-title" style={{ margin: 0 }}>Заявки</Typography.Title>
            <Typography.Text type="secondary" className="crm-count" aria-label={lastUpdated === null ? 'Количество заявок пока неизвестно' : `Всего заявок: ${count}`}>
              {lastUpdated === null ? (listLoading ? '…' : '—') : count}
            </Typography.Text>
          </Flex>
          <Typography.Text type="secondary" className="crm-subtitle">Из Telegram и вручную</Typography.Text>
        </div>
        <Button type="primary" size="large" className="crm-add-button" aria-label="Добавить заявку" onClick={startCreate} icon={<span aria-hidden="true">＋</span>}>
          <span className="crm-add-full">Добавить заявку</span><span className="crm-add-short">Добавить</span>
        </Button>
      </Flex>
      <Flex wrap align="center" justify="space-between" gap={16}>
        <Flex align="center" wrap gap={12}>
          <Select<number | undefined> aria-label="Направление" allowClear value={selectedTag}
            placeholder="Направление: все" loading={tagsLoading} prefix={selectedTag === undefined ? undefined : 'Направление:'}
            options={tags.map(tag => ({ value: tag.id, label: tag.name }))} onChange={changeFilter}
            className="crm-direction-filter" />
          {selectedTag !== undefined && <Button type="text" onClick={() => changeFilter(undefined)}>Сбросить фильтр</Button>}
        </Flex>
        {botUrl && <Button type="link" style={{ fontWeight: 400 }} href={botUrl} target="_blank" rel="noreferrer">Открыть Telegram-бота ↗</Button>}
      </Flex>
    </>}
    <div className={`crm-content-grid${wide && detailView ? ' crm-content-split' : ''}`}>
      {listVisible && <Flex vertical gap={16} className="crm-list-column">
        {tagsError && <Alert type="warning" showIcon title="Не удалось загрузить направления" description={tagsError}
          action={<Button size="small" onClick={() => void loadTags()} disabled={tagsLoading}>Повторить</Button>} />}
        {listLoading && !leads.length && <Card><Skeleton active paragraph={{ rows: 4 }} /></Card>}
        {listError && <Alert type={leads.length ? 'warning' : 'error'} showIcon role="alert"
          title={leads.length ? 'Не удалось обновить список' : 'Не удалось загрузить заявки'}
          description={<Flex vertical gap={8}><Typography.Text>{listError}</Typography.Text>
            {!!leads.length && lastUpdated !== null && <Typography.Text type="secondary">Последнее обновление: {formatExactDate(new Date(lastUpdated).toISOString())}</Typography.Text>}
          </Flex>} action={<Button onClick={() => void list.refresh(false)} disabled={listLoading}>Повторить</Button>} />}
        {!listLoading && !listError && count === 0 && <Card>
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={selectedTag === undefined ? 'Пока нет заявок' : 'По этому направлению заявок нет'}>
            {selectedTag === undefined ? <Typography.Text type="secondary">Добавьте первую заявку вручную{botUrl ? ' или через Telegram-бота.' : '.'}</Typography.Text>
              : <Button onClick={() => changeFilter(undefined)}>Сбросить фильтр</Button>}
          </Empty>
        </Card>}
        {leads.length > 0 && <>
          <div ref={listStart}>
            <LeadListView leads={leads} selectedId={mode.kind === 'detail' ? mode.leadId : undefined}
              expanded={expandedContacts} mobile={mobile} onExpand={toggleContacts} onOpen={openDetail} />
          </div>
          <Flex wrap justify="space-between" gap={8} style={{ minHeight: 22 }}>
            <Typography.Text type="secondary" style={{ fontSize: 14 }}>Время: {timezoneLabel()}</Typography.Text>
            {listLoading && <Typography.Text type="secondary" role="status" style={{ fontSize: 14 }}>Обновляем список…</Typography.Text>}
          </Flex>
          {moreError && <Alert type="warning" showIcon title="Не удалось загрузить следующие заявки" description={moreError}
            action={<Button onClick={() => void list.loadMore()} disabled={moreLoading}>Повторить загрузку</Button>} />}
          {hasMore && <Button onClick={() => void list.loadMore()} loading={moreLoading} block>Показать ещё</Button>}
        </>}
      </Flex>}
      {detailView}
    </div>
    {mode.kind === 'list' && newCount > 0 && <div className="crm-new-leads">
      <Button type="primary" onClick={() => {
        atTopRef.current = true
        window.scrollTo({ top: 0 })
        void list.acceptNew().then(() => viewHeading.current?.focus({ preventScroll: true }))
      }}>Новые заявки: {newCount} ↑</Button>
    </div>}
  </Flex>
}
