import { useCallback, useEffect, useRef, useState } from 'react'
import { Alert, App as AntApp, Button, Card, Empty, Flex, Form, Input, Result, Select, Skeleton, Space, Spin, Table, Tag as AntTag, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { ApiError, createLead, getLead, getLeads, getTags } from './api'
import type { Lead, LeadListOptions, LeadPage, Tag } from './api'
import { AccessInterruptedError } from './auth'
import { useCRMAccess } from './AuthBoundary'

const PAGE_SIZE = 50
const BOT_URL = import.meta.env.VITE_TELEGRAM_BOT_URL

type CRMMode = { kind: 'list' } | { kind: 'create' } | { kind: 'detail'; leadId: string }
type FormValues = { name: string; contacts: string[]; request: string; tag_ids: number[] }
type FormFieldError =
  | { name: 'name' | 'request' | 'tag_ids'; errors: string[] }
  | { name: ['contacts', number]; errors: string[] }
type ScrollAnchor = { leadId: string | null; top: number; scrollY: number }

function timezoneLabel() {
  return Intl.DateTimeFormat().resolvedOptions().timeZone || 'часовой пояс устройства'
}

function formatDate(value: string) {
  return new Intl.DateTimeFormat('ru-RU', { dateStyle: 'short', timeStyle: 'short' }).format(new Date(value))
}

function sourceLabel(source: Lead['source']) {
  return source === 'manual' ? 'Вручную' : 'Telegram-бот'
}

function statusLabel(status: Lead['status']) {
  return ({ new: 'Новый', in_progress: 'В работе', closed: 'Закрыт' })[status]
}

function contactHref(type: Lead['contacts'][number]['type'], value: string) {
  if (type === 'phone') return `tel:${value.trim().replace(/[ ()-]/g, '')}`
  if (type === 'email') {
    const address = value.trim().split('@').map(encodeURIComponent).join('@')
    return `mailto:${address}`
  }
  const text = value.trim()
  const username = text.startsWith('@') ? text.slice(1) : new URL(text.startsWith('http') ? text : `https://${text}`).pathname.split('/').filter(Boolean)[0]
  return `https://t.me/${encodeURIComponent(username)}`
}

function telegramBotUrl() {
  if (typeof BOT_URL !== 'string' || !BOT_URL) return null
  try {
    const url = new URL(BOT_URL)
    if (url.protocol !== 'https:' || url.hostname !== 't.me' || url.search || url.hash
      || !/^\/[A-Za-z][A-Za-z0-9_]{4,31}\/?$/.test(url.pathname)) return null
    return url.href
  } catch {
    return null
  }
}

function sortLeads(leads: Lead[]) {
  return [...leads].sort((left, right) => {
    const dateOrder = Date.parse(right.created_at) - Date.parse(left.created_at)
    return dateOrder || right.id.localeCompare(left.id)
  })
}

function mergeLeads(existing: Lead[], incoming: Lead[]) {
  const byId = new Map(existing.map((lead) => [lead.id, lead]))
  for (const lead of incoming) byId.set(lead.id, lead)
  return sortLeads([...byId.values()])
}

function findVisibleLeadElement(id: string) {
  const escapedId = CSS.escape(id)
  return [...document.querySelectorAll<HTMLElement>(`[data-lead-id="${escapedId}"], .crm-lead-${escapedId}`)]
    .find((element) => element.getClientRects().length > 0) ?? null
}

function getFailureMessage(error: unknown) {
  if (error instanceof ApiError) return error.message
  if (error instanceof AccessInterruptedError) return 'Повторите действие после входа в CRM.'
  return 'Не удалось связаться с сервером. Проверьте соединение и повторите.'
}

function leadLink(lead: Lead, onOpen: (lead: Lead) => void) {
  return <Button type="link" className="lead-name-button" onClick={() => onOpen(lead)} aria-label={`Открыть карточку: ${lead.name}`}>
    {lead.name}
  </Button>
}

function ContactText({ lead, expanded, onExpand }: {
  lead: Lead
  expanded: boolean
  onExpand: (id: string) => void
}) {
  const shown = expanded ? lead.contacts : lead.contacts.slice(0, 2)
  return <Space direction="vertical" size={0} className="contact-preview">
    {shown.map((contact, index) => <Typography.Text key={`${contact.type}-${contact.value}-${index}`}>
      {contact.value}
    </Typography.Text>)}
    {!expanded && lead.contacts.length > 2 && <Button type="link" size="small" onClick={() => onExpand(lead.id)}>
      Ещё {lead.contacts.length - 2}
    </Button>}
  </Space>
}

function LeadRowCard({ lead, expanded, onExpand, onOpen }: {
  lead: Lead
  expanded: boolean
  onExpand: (id: string) => void
  onOpen: (lead: Lead) => void
}) {
  return <Card size="small" className="lead-mobile-card" data-lead-id={lead.id}>
    <Flex vertical gap={8}>
      <div><Typography.Text type="secondary">Имя</Typography.Text><div>{leadLink(lead, onOpen)}</div></div>
      <div><Typography.Text type="secondary">Контакты</Typography.Text>
        <div><ContactText lead={lead} expanded={expanded} onExpand={onExpand} /></div>
      </div>
      <Flex wrap gap="small">
        <div><Typography.Text type="secondary">Источник</Typography.Text><div>{sourceLabel(lead.source)}</div></div>
        <div><Typography.Text type="secondary">Статус</Typography.Text><div>{statusLabel(lead.status)}</div></div>
      </Flex>
      <div><Typography.Text type="secondary">Теги</Typography.Text><div>{
        lead.tags.length ? lead.tags.map((tag) => <AntTag key={tag.id}>{tag.name}</AntTag>) : 'Без тегов'
      }</div></div>
      <div><Typography.Text type="secondary">Создан</Typography.Text><div>{formatDate(lead.created_at)}</div></div>
    </Flex>
  </Card>
}

function ContactLinks({ lead }: { lead: Lead }) {
  return <Flex vertical gap={8}>
    {lead.contacts.map((contact, index) => <a
      key={`${contact.type}-${contact.value}-${index}`}
      href={contactHref(contact.type, contact.value)}
      target={contact.type === 'telegram' ? '_blank' : undefined}
      rel={contact.type === 'telegram' ? 'noreferrer' : undefined}
    >{contact.value}</a>)}
  </Flex>
}

function ManualLeadForm({
  tags,
  tagsLoading,
  tagsError,
  offline,
  onRetryTags,
  onExit,
  onCreated,
}: {
  tags: Tag[]
  tagsLoading: boolean
  tagsError: string | null
  offline: boolean
  onRetryTags: () => void
  onExit: () => void
  onCreated: (lead: Lead) => void
}) {
  const { controller } = useCRMAccess()
  const { modal } = AntApp.useApp()
  const [form] = Form.useForm<FormValues>()
  const [operationId] = useState(() => crypto.randomUUID())
  const [saving, setSaving] = useState(false)
  const [unknown, setUnknown] = useState(false)
  const [conflict, setConflict] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)
  const frozenSubmission = useRef<ReturnType<typeof makeSubmission> | null>(null)
  const sent = useRef(false)
  const heading = useRef<HTMLHeadingElement>(null)

  function makeSubmission(values: FormValues) {
    const nonblank = values.contacts.map((contact, index) => ({ contact, index }))
      .filter(({ contact }) => contact.trim().length > 0)
    const fieldIndexes = nonblank.map(({ index }) => index)
    return {
      payload: {
        submission_id: operationId,
        name: values.name,
        contacts: nonblank.map(({ contact }) => contact),
        request: values.request,
        tag_ids: values.tag_ids ?? [],
      },
      fieldIndexes,
    }
  }

  const formIsDirty = useCallback(() => {
    const values = form.getFieldsValue()
    return (values.name ?? '') !== '' || (values.request ?? '') !== ''
      || (values.contacts ?? []).some((value: string) => value !== '')
      || (values.tag_ids ?? []).length > 0
  }, [form])

  useEffect(() => {
    heading.current?.focus()
  }, [])

  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => {
      if (!formIsDirty() && !saving && !unknown) return
      event.preventDefault()
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [formIsDirty, saving, unknown])

  const requestExit = () => {
    if (saving || unknown) return
    if (!formIsDirty()) {
      onExit()
      return
    }
    modal.confirm({
      title: 'Выйти без сохранения?',
      content: 'Введённые данные будут потеряны.',
      okText: 'Выйти без сохранения',
      cancelText: 'Остаться',
      onOk: onExit,
    })
  }

  const submit = async (values: FormValues) => {
    setFormError(null)
    form.setFields([{ name: 'contacts', errors: [] }])
    const prepared = frozenSubmission.current ?? makeSubmission(values)
    if (prepared.payload.contacts.length === 0) {
      form.setFields([{ name: 'contacts', errors: ['Укажите хотя бы один корректный контакт.'] }])
      return
    }
    frozenSubmission.current = prepared
    setSaving(true)
    sent.current = false
    try {
      const created = await controller.runWithAccess((csrfToken) => {
        sent.current = true
        return createLead(prepared.payload, csrfToken)
      })
      frozenSubmission.current = null
      setUnknown(false)
      onCreated(created)
    } catch (error) {
      if (!sent.current || (error instanceof ApiError && error.status >= 400 && error.status < 500)) {
        frozenSubmission.current = null
        setUnknown(false)
        setFormError(getFailureMessage(error))
        if (error instanceof ApiError && error.fieldErrors) {
          const fields = Object.entries(error.fieldErrors).reduce<FormFieldError[]>((updates, [name, errors]) => {
            const match = /^contacts\.(\d+)$/.exec(name)
            if (match) {
              const originalIndex = prepared.fieldIndexes[Number(match[1])]
              if (originalIndex !== undefined) updates.push({ name: ['contacts', originalIndex], errors })
              return updates
            }
            if (name === 'name' || name === 'request' || name === 'tag_ids') updates.push({ name, errors })
            return updates
          }, [])
          form.setFields(fields)
        }
        if (error instanceof ApiError && error.code === 'submission_conflict') {
          frozenSubmission.current = prepared
          setConflict(true)
        }
      } else {
        setUnknown(true)
        setFormError('Не удалось подтвердить сохранение. Проверьте и повторите отправку этой же заявки.')
      }
    } finally {
      setSaving(false)
    }
  }

  const verifyAndRetry = () => {
    const frozen = frozenSubmission.current
    if (!frozen) return
    void submit({
      name: frozen.payload.name,
      contacts: form.getFieldValue('contacts') ?? [],
      request: frozen.payload.request,
      tag_ids: frozen.payload.tag_ids,
    })
  }

  const title = <Typography.Title level={2} tabIndex={-1} ref={heading} style={{ margin: 0 }}>
    Добавить лид
  </Typography.Title>

  return <Flex vertical gap="middle" className="crm-subview">
    <Flex wrap align="center" justify="space-between" gap="middle">
      {title}
      <Button onClick={requestExit} disabled={saving || unknown}>К списку заявок</Button>
    </Flex>
    {offline && <Alert type="warning" showIcon title="Нет связи с сервером" description="Текст формы сохранён в этой вкладке. Дождитесь связи, чтобы отправить заявку." />}
    {tagsError && <Alert type="warning" showIcon title="Не удалось загрузить список тегов" description={tagsError}
      action={<Button size="small" onClick={onRetryTags} disabled={tagsLoading}>Повторить</Button>} />}
    {tagsError && tags.length === 0 && <Typography.Text type="secondary">Можно создать заявку без тегов.</Typography.Text>}
    {unknown && <Alert type="warning" showIcon role="alert" title="Результат сохранения неизвестен"
      description={formError} action={<Button onClick={verifyAndRetry} loading={saving} disabled={offline}>Проверить и повторить</Button>} />}
    {conflict && <Alert type="error" showIcon role="alert" title="Эта операция уже связана с другими данными"
      description="Сервер связал этот идентификатор с другими данными. Не отправляйте эту заявку повторно. Сохраните её текст отдельно, затем выйдите из формы." />}
    {!unknown && !conflict && formError && <Alert type="error" showIcon role="alert" title={formError} />}
    <Card>
      <Form<FormValues>
        form={form}
        layout="vertical"
        requiredMark={false}
        initialValues={{ contacts: [''], tag_ids: [] }}
        onFinish={(values) => void submit(values)}
        onValuesChange={() => setFormError(null)}
      >
        <Form.Item label="Имя" name="name" rules={[
          { required: true, whitespace: true, message: 'Введите имя.' },
          { max: 100, message: 'Не более 100 символов.' },
        ]}>
          <Input autoComplete="name" disabled={saving || unknown || conflict} />
        </Form.Item>
        <Form.Item label="Контакты">
          <Form.List name="contacts" rules={[{ validator: async (_, values: string[]) => {
            if (!values?.some((value) => value.trim().length > 0)) throw new Error('Укажите хотя бы один корректный контакт.')
          } }]}>
            {(fields, { add, remove }, meta) => <Flex vertical gap="small">
              {fields.map((field, index) => <Flex key={field.key} align="start" gap="small">
                <Form.Item name={field.name} style={{ flex: 1, marginBottom: 4 }}>
                  <Input aria-label={`Контакт ${index + 1}`} autoComplete="off" disabled={saving || unknown || conflict} />
                </Form.Item>
                {fields.length > 1 && <Button aria-label={`Удалить контакт ${index + 1}`} onClick={() => remove(field.name)}
                  disabled={saving || unknown || conflict}>Удалить</Button>}
              </Flex>)}
              <Button onClick={() => add('')} disabled={saving || unknown || conflict} block>Добавить контакт</Button>
              <Form.ErrorList errors={meta.errors} />
            </Flex>}
          </Form.List>
          <Typography.Text type="secondary">Телефон с кодом страны, email или Telegram-контакт.</Typography.Text>
        </Form.Item>
        <Form.Item label="Запрос" name="request" rules={[
          { required: true, whitespace: true, message: 'Опишите запрос.' },
          { max: 2000, message: 'Не более 2000 символов.' },
        ]}>
          <Input.TextArea autoSize={{ minRows: 4, maxRows: 10 }} disabled={saving || unknown || conflict} />
        </Form.Item>
        <Form.Item label="Теги" name="tag_ids">
          <Select
            mode="multiple"
            allowClear
            placeholder={tagsLoading ? 'Загружаем теги…' : 'Выберите теги (необязательно)'}
            options={tags.map((tag) => ({ value: tag.id, label: tag.name }))}
            loading={tagsLoading}
            disabled={saving || unknown || conflict || tags.length === 0}
          />
        </Form.Item>
        <Button type="primary" htmlType="submit" loading={saving} disabled={unknown || conflict || offline} block>
          Сохранить лид
        </Button>
      </Form>
    </Card>
  </Flex>
}

export function CRMWorkspace() {
  const { controller, state: accessState } = useCRMAccess()
  const { message } = AntApp.useApp()
  const [mode, setMode] = useState<CRMMode>({ kind: 'list' })
  const [tags, setTags] = useState<Tag[]>([])
  const [tagsLoading, setTagsLoading] = useState(true)
  const [tagsError, setTagsError] = useState<string | null>(null)
  const [selectedTag, setSelectedTag] = useState<number | undefined>()
  const [leads, setLeads] = useState<Lead[]>([])
  const [count, setCount] = useState(0)
  const [hasMore, setHasMore] = useState(false)
  const [listLoading, setListLoading] = useState(true)
  const [moreLoading, setMoreLoading] = useState(false)
  const [listError, setListError] = useState<string | null>(null)
  const [expandedContacts, setExpandedContacts] = useState<Set<string>>(() => new Set())
  const [detail, setDetail] = useState<Lead | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailError, setDetailError] = useState<string | null>(null)
  const [hiddenByFilter, setHiddenByFilter] = useState(false)
  const [newSubmission, setNewSubmission] = useState(0)
  const leadRef = useRef(leads)
  leadRef.current = leads
  const countRef = useRef(count)
  countRef.current = count
  const filterRef = useRef(selectedTag)
  filterRef.current = selectedTag
  const listController = useRef<AbortController | null>(null)
  const tagsController = useRef<AbortController | null>(null)
  const listSequence = useRef(0)
  const moreBusy = useRef(false)
  const restoreAnchorAfterRefresh = useRef(false)
  const detailController = useRef<AbortController | null>(null)
  const pageAnchor = useRef<ScrollAnchor>({ leadId: null, top: 0, scrollY: 0 })
  const viewHeading = useRef<HTMLHeadingElement>(null)
  const detailHeading = useRef<HTMLHeadingElement>(null)
  const botUrl = telegramBotUrl()

  const loadTags = useCallback(async () => {
    tagsController.current?.abort()
    const request = new AbortController()
    tagsController.current = request
    setTagsLoading(true)
    try {
      const result = await controller.runWithAccess((() => getTags({ signal: request.signal })))
      if (!request.signal.aborted) {
        setTags(result)
        setTagsError(null)
      }
    } catch (error) {
      if (!request.signal.aborted) setTagsError(getFailureMessage(error))
    } finally {
      if (!request.signal.aborted) setTagsLoading(false)
    }
  }, [controller])

  const refreshList = useCallback(async (tagId: number | undefined, fillTo = 0) => {
    listController.current?.abort()
    const request = new AbortController()
    listController.current = request
    const sequence = ++listSequence.current
    setListError(null)
    setListLoading(true)
    setMoreLoading(false)
    moreBusy.current = false
    const result: Lead[] = []
    let page: LeadPage
    try {
      do {
        const options: LeadListOptions = {
          ...(tagId === undefined ? {} : { tagId }),
          ...(result.length ? { beforeId: result[result.length - 1].id } : {}),
          limit: PAGE_SIZE,
        }
        page = await controller.runWithAccess((() => getLeads(options, { signal: request.signal })))
        if (request.signal.aborted || sequence !== listSequence.current || filterRef.current !== tagId) return
        result.push(...page.results)
        if (!page.results.length || result.length >= fillTo || !page.next) break
      } while (result.length < fillTo)

      if (request.signal.aborted || sequence !== listSequence.current || filterRef.current !== tagId) return
      setLeads(result)
      leadRef.current = result
      setCount(page.count)
      countRef.current = page.count
      setHasMore(page.next !== null && result.length > 0)
      setListError(null)
      if (restoreAnchorAfterRefresh.current) {
        restoreAnchorAfterRefresh.current = false
        const anchor = pageAnchor.current
        window.requestAnimationFrame(() => {
          const target = anchor.leadId ? findVisibleLeadElement(anchor.leadId) : null
          if (target) window.scrollBy({ top: target.getBoundingClientRect().top - anchor.top })
          else window.scrollTo({ top: anchor.scrollY })
        })
      }
    } catch (error) {
      if (!request.signal.aborted && sequence === listSequence.current) {
        restoreAnchorAfterRefresh.current = false
        setListError(getFailureMessage(error))
      }
    } finally {
      if (!request.signal.aborted && sequence === listSequence.current) setListLoading(false)
    }
  }, [controller])

  useEffect(() => {
    void loadTags()
    void refreshList(undefined)
    const refreshOnReturn = () => {
      if (document.visibilityState === 'visible') {
        void loadTags()
        void refreshList(filterRef.current, leadRef.current.length)
      }
    }
    document.addEventListener('visibilitychange', refreshOnReturn)
    window.addEventListener('focus', refreshOnReturn)
    return () => {
      document.removeEventListener('visibilitychange', refreshOnReturn)
      window.removeEventListener('focus', refreshOnReturn)
      listController.current?.abort()
      tagsController.current?.abort()
      detailController.current?.abort()
    }
  }, [loadTags, refreshList])

  const loadMore = async () => {
    if (moreBusy.current || !hasMore || !leads.length) return
    moreBusy.current = true
    const request = new AbortController()
    listController.current?.abort()
    listController.current = request
    const sequence = ++listSequence.current
    setMoreLoading(true)
    setListError(null)
    try {
      const page = await controller.runWithAccess((() => getLeads({
        ...(selectedTag === undefined ? {} : { tagId: selectedTag }),
        beforeId: leads[leads.length - 1].id,
        limit: PAGE_SIZE,
      }, { signal: request.signal })))
      if (request.signal.aborted || sequence !== listSequence.current || filterRef.current !== selectedTag) return
      const merged = mergeLeads(leadRef.current, page.results)
      setLeads(merged)
      leadRef.current = merged
      setCount(page.count)
      countRef.current = page.count
      setHasMore(page.next !== null)
    } catch (error) {
      if (!request.signal.aborted && sequence === listSequence.current) setListError(getFailureMessage(error))
    } finally {
      if (!request.signal.aborted && sequence === listSequence.current) {
        setMoreLoading(false)
        moreBusy.current = false
      }
    }
  }

  const retryTags = () => void loadTags()
  const changeFilter = (value: number | undefined) => {
    restoreAnchorAfterRefresh.current = false
    listController.current?.abort()
    listSequence.current++
    moreBusy.current = false
    setSelectedTag(value)
    filterRef.current = value
    setLeads([])
    leadRef.current = []
    setCount(0)
    countRef.current = 0
    setHasMore(false)
    setExpandedContacts(new Set())
    void refreshList(value)
  }

  const captureScrollAnchor = () => {
    const visible = [...document.querySelectorAll<HTMLElement>('[data-lead-id], [class*="crm-lead-"]')]
      .find((element) => element.getClientRects().length > 0 && element.getBoundingClientRect().bottom > 0)
    const leadId = visible?.dataset.leadId ?? (visible
      ? [...visible.classList].find((name) => name.startsWith('crm-lead-'))?.slice('crm-lead-'.length) ?? null
      : null)
    pageAnchor.current = {
      leadId,
      top: visible?.getBoundingClientRect().top ?? 0,
      scrollY: window.scrollY,
    }
  }

  const showDetail = (lead: Lead, wasHidden = false) => {
    setDetail(lead)
    setDetailError(null)
    setHiddenByFilter(wasHidden)
    setMode({ kind: 'detail', leadId: lead.id })
  }

  const openDetail = (lead: Lead, wasHidden = false) => {
    captureScrollAnchor()
    showDetail(lead, wasHidden)
  }

  useEffect(() => {
    if (mode.kind !== 'detail') return
    detailHeading.current?.focus()
    detailController.current?.abort()
    const request = new AbortController()
    detailController.current = request
    setDetailLoading(true)
    void controller.runWithAccess((() => getLead(mode.leadId, { signal: request.signal }))).then((result) => {
      if (!request.signal.aborted) {
        setDetail(result)
        setDetailError(null)
      }
    }).catch((error: unknown) => {
      if (!request.signal.aborted) setDetailError(getFailureMessage(error))
    }).finally(() => {
      if (!request.signal.aborted) setDetailLoading(false)
    })
    return () => request.abort()
  }, [controller, mode])

  const returnToList = () => {
    if (mode.kind === 'detail') detailController.current?.abort()
    setMode({ kind: 'list' })
    const anchor = pageAnchor.current
    restoreAnchorAfterRefresh.current = true
    window.requestAnimationFrame(() => {
      const target = anchor.leadId ? findVisibleLeadElement(anchor.leadId) : null
      if (target) window.scrollBy({ top: target.getBoundingClientRect().top - anchor.top })
      else window.scrollTo({ top: anchor.scrollY })
      viewHeading.current?.focus({ preventScroll: true })
      void refreshList(filterRef.current, leadRef.current.length)
    })
  }

  const startCreate = () => {
    captureScrollAnchor()
    setMode({ kind: 'create' })
  }

  const finishCreate = (created: Lead) => {
    const hidden = selectedTag !== undefined && !created.tags.some((tag) => tag.id === selectedTag)
    setHiddenByFilter(hidden)
    if (!hidden) {
      const exists = leadRef.current.some((lead) => lead.id === created.id)
      const updated = mergeLeads(leadRef.current, [created])
      setLeads(updated)
      leadRef.current = updated
      if (!exists) {
        setCount((value) => value + 1)
        countRef.current += 1
      }
    }
    setNewSubmission((value) => value + 1)
    showDetail(created, hidden)
    if (hidden) void message.success('Лид создан. Он не подходит к текущему фильтру.')
  }

  const retryList = () => void refreshList(filterRef.current, leadRef.current.length)
  const retryDetail = () => {
    if (detail) setMode({ kind: 'detail', leadId: detail.id })
  }
  const toggleContacts = (id: string) => setExpandedContacts((current) => {
    const next = new Set(current)
    if (next.has(id)) next.delete(id)
    else next.add(id)
    return next
  })

  const rows = leads.map((lead) => ({ ...lead, key: lead.id }))
  const columns: ColumnsType<Lead & { key: string }> = [
    { title: 'Имя', key: 'name', render: (_, lead) => leadLink(lead, openDetail) },
    { title: 'Контакты', key: 'contacts', render: (_, lead) => <ContactText lead={lead} expanded={expandedContacts.has(lead.id)} onExpand={toggleContacts} /> },
    { title: 'Источник', dataIndex: 'source', key: 'source', render: (source: Lead['source']) => sourceLabel(source) },
    { title: 'Статус', dataIndex: 'status', key: 'status', render: (status: Lead['status']) => statusLabel(status) },
    { title: 'Теги', dataIndex: 'tags', key: 'tags', render: (values: Tag[]) => values.length ? values.map((tag) => <AntTag key={tag.id}>{tag.name}</AntTag>) : 'Без тегов' },
    { title: 'Создан', dataIndex: 'created_at', key: 'created_at', render: formatDate },
  ]

  if (mode.kind === 'create') return <ManualLeadForm
    key={newSubmission}
    tags={tags}
    tagsLoading={tagsLoading}
    tagsError={tagsError}
    offline={accessState.offline}
    onRetryTags={retryTags}
    onExit={returnToList}
    onCreated={finishCreate}
  />

  if (mode.kind === 'detail') return <Flex vertical gap="middle" className="crm-subview">
    <Flex wrap align="center" justify="space-between" gap="middle">
      <Button onClick={returnToList}>К списку заявок</Button>
      {detailLoading && <Spin size="small" aria-label="Загружаем карточку" />}
    </Flex>
    {hiddenByFilter && <Alert type="info" showIcon title="Этот лид не подходит к текущему фильтру" description="Карточка созданного лида открыта отдельно. Вернитесь к списку, чтобы продолжить работу с фильтром." />}
    {detailError && <Alert type="warning" showIcon title="Не удалось обновить карточку" description={detailError}
      action={<Button size="small" onClick={retryDetail}>Повторить</Button>} />}
    {detail ? <Card title={<Typography.Title level={2} tabIndex={-1} ref={detailHeading} style={{ margin: 0 }}>{detail.name}</Typography.Title>}>
      <Flex vertical gap="middle">
        <section><Typography.Text strong>Контакты</Typography.Text><ContactLinks lead={detail} /></section>
        <section><Typography.Text strong>Запрос</Typography.Text><Typography.Paragraph style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{detail.request}</Typography.Paragraph></section>
        <section><Typography.Text strong>Источник</Typography.Text><div>{sourceLabel(detail.source)}</div></section>
        <section><Typography.Text strong>Статус</Typography.Text><div>{statusLabel(detail.status)}</div></section>
        <section><Typography.Text strong>Теги</Typography.Text><div>{detail.tags.length ? detail.tags.map((tag) => <AntTag key={tag.id}>{tag.name}</AntTag>) : 'Без тегов'}</div></section>
        <section><Typography.Text strong>Создан</Typography.Text><div>{formatDate(detail.created_at)} ({timezoneLabel()})</div></section>
      </Flex>
    </Card> : !detailLoading && <Result status="404" title="Не удалось загрузить карточку"
      subTitle={detailError ?? 'Заявка недоступна.'} extra={<Button onClick={retryDetail}>Повторить</Button>} />}
  </Flex>

  return <Flex vertical gap="middle" className="crm-workspace">
    <Flex wrap align="center" justify="space-between" gap="middle">
      <div><Typography.Title level={1} ref={viewHeading} tabIndex={-1} style={{ margin: 0 }}>Заявки</Typography.Title>
        <Typography.Text type="secondary">Время: {timezoneLabel()}</Typography.Text>
      </div>
      <Space wrap>
        {botUrl && <Button href={botUrl} target="_blank" rel="noreferrer">Открыть Telegram-бота</Button>}
        <Button type="primary" onClick={startCreate}>Добавить лид</Button>
      </Space>
    </Flex>
    <Flex wrap gap="middle" align="center">
      <Select<number | undefined>
        aria-label="Фильтр по тегу"
        allowClear
        value={selectedTag}
        placeholder="Все теги"
        loading={tagsLoading}
        options={tags.map((tag) => ({ value: tag.id, label: tag.name }))}
        onChange={changeFilter}
        style={{ minWidth: 210 }}
      />
      {selectedTag !== undefined && <Button onClick={() => changeFilter(undefined)}>Сбросить фильтр</Button>}
    </Flex>
    {tagsError && <Alert type="warning" showIcon title="Не удалось загрузить теги" description={tagsError}
      action={<Button size="small" onClick={retryTags} disabled={tagsLoading}>Повторить</Button>} />}
    {listLoading && !leads.length && <Card><Skeleton active paragraph={{ rows: 4 }} /></Card>}
    {listError && (leads.length
      ? <Alert type="warning" showIcon role="alert" title="Не удалось обновить список" description={listError}
        action={<Button onClick={retryList} disabled={listLoading}>Повторить</Button>} />
      : <Alert type="error" showIcon role="alert" title="Не удалось загрузить заявки" description={listError}
        action={<Button onClick={retryList} disabled={listLoading}>Повторить</Button>} />)}
    {!listLoading && !listError && count === 0 && (selectedTag === undefined
      ? <Card><Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="Пока нет заявок">
        <Flex vertical gap="small" align="center">
          <Typography.Text>Добавьте лид вручную{botUrl ? ' или откройте Telegram-бота.' : '.'}</Typography.Text>
          {botUrl && <Button href={botUrl} target="_blank" rel="noreferrer">Открыть Telegram-бота</Button>}
          <Button type="primary" onClick={startCreate}>Добавить лид</Button>
        </Flex>
      </Empty></Card>
      : <Card><Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="По этому тегу заявок нет">
        <Button onClick={() => changeFilter(undefined)}>Сбросить фильтр</Button>
      </Empty></Card>)}
    {count > 0 && <>
      <Typography.Text type="secondary">Заявок: {count}</Typography.Text>
      {listLoading && <Typography.Text role="status">Обновляем список…</Typography.Text>}
      <div className="lead-desktop-table">
        <Table<Lead & { key: string }>
          rowKey="id"
          columns={columns}
          dataSource={rows}
          onRow={(lead) => ({ className: `crm-lead-${lead.id}` })}
          pagination={false}
          loading={listLoading && leads.length > 0}
          locale={{ emptyText: <Empty description="По этому фильтру заявок нет" /> }}
          scroll={{ x: 760 }}
        />
      </div>
      <div className="lead-mobile-list">
        <Flex vertical gap="small">
          {leads.map((lead) => <LeadRowCard
            key={lead.id}
            lead={lead}
            expanded={expandedContacts.has(lead.id)}
            onExpand={toggleContacts}
            onOpen={openDetail}
          />)}
        </Flex>
      </div>
      {hasMore && <Button onClick={() => void loadMore()} loading={moreLoading} disabled={listLoading} block>
        Показать ещё
      </Button>}
    </>}
  </Flex>
}
