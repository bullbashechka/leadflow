import { useLayoutEffect, useRef, useState } from 'react'
import type { Ref } from 'react'
import { Alert, App as AntApp, Button, Card, Divider, Flex, Result, Spin, Tag, theme, Typography } from 'antd'
import { ApiError, deleteLead } from './api'
import type { Lead, Tag as LeadTag } from './api'
import { useCRMAccess } from './AuthBoundary'
import { getFailureMessage } from './crmErrors'
import { formatExactDate, timezoneLabel, sourceLabel } from './leadPresentation'
import { LeadEditForm } from './LeadEditForm'
import { LeadStatusControl } from './LeadStatusControl'

function contactHref(type: Lead['contacts'][number]['type'], value: string) {
  if (type === 'phone') return `tel:${value.trim().replace(/[ ()-]/g, '')}`
  if (type === 'email') return `mailto:${value.trim().split('@').map(encodeURIComponent).join('@')}`
  const text = value.trim()
  const username = text.startsWith('@') ? text.slice(1) : new URL(text.startsWith('http') ? text : `https://${text}`).pathname.split('/').filter(Boolean)[0]
  return `https://t.me/${encodeURIComponent(username)}`
}

export function LeadDetails({ lead, loading, error, hiddenByFilter, panel, mobile, headingRef, onClose, onRetry,
  tags, notice, onDirty, onChanged, onCurrent, onManageTags, onDeleted,
}: {
  lead: Lead | null
  loading: boolean
  error: string | null
  hiddenByFilter: boolean
  panel: boolean
  mobile: boolean
  headingRef: Ref<HTMLHeadingElement>
  onClose: () => void
  onRetry: () => void
  tags: LeadTag[]
  notice: string | null
  onDirty: (dirty: boolean) => void
  onChanged: (lead: Lead) => void
  onCurrent: (lead: Lead) => void
  onManageTags: (onSelect?: (tag: LeadTag) => void) => void
  onDeleted: () => void
}) {
  const { token } = theme.useToken()
  const { modal } = AntApp.useApp()
  const { controller } = useCRMAccess()
  const container = useRef<HTMLElement>(null)
  const deletion = useRef<{ id: string; operationId: string; expectedVersion: number } | null>(null)
  const sent = useRef(false)
  const [deleting, setDeleting] = useState(false)
  const [deleteUnknown, setDeleteUnknown] = useState(false)
  const [deleteError, setDeleteError] = useState<string | null>(null)

  useLayoutEffect(() => {
    if (!panel) return
    const fitToViewport = () => {
      const element = container.current
      if (element) element.style.setProperty('--detail-top', `${Math.max(24, element.getBoundingClientRect().top)}px`)
    }
    fitToViewport()
    window.addEventListener('scroll', fitToViewport, { passive: true })
    window.addEventListener('resize', fitToViewport)
    return () => {
      window.removeEventListener('scroll', fitToViewport)
      window.removeEventListener('resize', fitToViewport)
    }
  }, [panel])

  const runDelete = async (operation: { id: string; operationId: string; expectedVersion: number }) => {
    setDeleting(true)
    setDeleteError(null)
    sent.current = false
    try {
      await controller.runWithAccess((csrf) => {
        sent.current = true
        return deleteLead(operation.id, operation.operationId, operation.expectedVersion, csrf)
      })
      deletion.current = null
      setDeleteUnknown(false)
      onDeleted()
    } catch (cause) {
      if (cause instanceof ApiError && cause.code === 'version_conflict' && cause.currentLead) {
        deletion.current = null
        setDeleteUnknown(false)
        onCurrent(cause.currentLead)
        setDeleteError('Заявка изменилась в другой вкладке. Проверьте актуальные данные перед удалением.')
      } else if (cause instanceof ApiError && cause.code === 'lead_deleted') {
        deletion.current = null
        setDeleteUnknown(false)
        setDeleteError('Заявка уже удалена в другой вкладке.')
      } else if (!sent.current || (cause instanceof ApiError && cause.status >= 400 && cause.status < 500)) {
        deletion.current = null
        setDeleteUnknown(false)
        setDeleteError(getFailureMessage(cause))
      } else {
        setDeleteUnknown(true)
        setDeleteError('Результат удаления не подтверждён. Повторите ту же операцию.')
      }
    } finally {
      setDeleting(false)
    }
  }

  const confirmDelete = () => {
    if (!lead) return
    const target = lead
    modal.confirm({
      title: 'Удалить заявку?',
      content: <Flex vertical gap={8}>
        <Typography.Text strong>{target.name}</Typography.Text>
        <Typography.Text>Контакт: {target.contacts.map(({ value }) => value).join(', ')}</Typography.Text>
        <Alert type="warning" showIcon title="Удаление нельзя отменить. Восстановление в MVP недоступно." />
      </Flex>,
      okText: 'Удалить заявку',
      cancelText: 'Оставить заявку',
      okButtonProps: { danger: true },
      onOk: () => {
        const operation = { id: target.id, operationId: crypto.randomUUID(), expectedVersion: target.version }
        deletion.current = operation
        return runDelete(operation)
      },
    })
  }

  const contacts = lead && <Flex vertical gap={10} component="section" aria-label="Контакты">
    <Typography.Text strong={mobile} type={mobile ? undefined : 'secondary'}>Контакты</Typography.Text>
    {lead.contacts.map((contact, index) => <Typography.Link key={`${contact.type}-${contact.value}-${index}`}
      href={contactHref(contact.type, contact.value)}
      target={contact.type === 'telegram' ? '_blank' : undefined}
      rel={contact.type === 'telegram' ? 'noreferrer' : undefined}
      style={{ overflowWrap: 'anywhere' }}>{contact.value}</Typography.Link>)}
  </Flex>
  const directions = lead && <Flex vertical gap={10} component="section" aria-label="Теги">
    <Typography.Text strong={mobile} type={mobile ? undefined : 'secondary'}>Теги</Typography.Text>
    <Flex wrap gap={6}>{lead.tags.length ? lead.tags.map(tag => <Tag key={tag.id} style={{ margin: 0, maxWidth: '100%', whiteSpace: 'normal', overflowWrap: 'anywhere' }}>{tag.name}</Tag>) : <Typography.Text>Без тегов</Typography.Text>}</Flex>
  </Flex>
  const request = lead && <Flex vertical gap={10} component="section" aria-label="Запрос">
    <Typography.Text strong={mobile} type={mobile ? undefined : 'secondary'}>Запрос</Typography.Text>
    <Typography.Paragraph style={{ margin: 0, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{lead.request}</Typography.Paragraph>
  </Flex>
  const note = lead && <Flex vertical gap={10} component="section" aria-label="Заметка менеджера">
    <Typography.Text strong={mobile} type={mobile ? undefined : 'secondary'}>Заметка менеджера</Typography.Text>
    <Typography.Paragraph style={{ margin: 0, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>
      {lead.note || <Typography.Text type="secondary">Нет заметки</Typography.Text>}
    </Typography.Paragraph>
  </Flex>
  const timestamp = lead && <Typography.Text type="secondary" style={{ fontSize: mobile ? 14 : undefined }}>{formatExactDate(lead.created_at)} · {timezoneLabel()}</Typography.Text>

  return <section ref={container} className="crm-detail" aria-label="Карточка заявки">
    <Card variant={mobile ? 'borderless' : 'outlined'} style={mobile ? { background: 'transparent', boxShadow: 'none' } : undefined}
      styles={{ body: { padding: mobile ? 0 : 24 } }}>
      <Flex vertical gap={24}>
        <Flex vertical={!panel} align={panel ? 'start' : 'stretch'} justify="space-between" gap={8}>
          {lead ? <Typography.Title level={2} ref={headingRef} tabIndex={-1} style={{ margin: 0, minWidth: 0, overflowWrap: 'anywhere' }}>{lead.name}</Typography.Title>
            : <Typography.Text type="secondary">Карточка заявки</Typography.Text>}
          <Button type="text" aria-label="К списку заявок" title="К списку заявок" style={{ flexShrink: 0, order: panel ? undefined : -1, alignSelf: panel || mobile ? 'start' : 'end', paddingInline: mobile ? 0 : undefined }} onClick={onClose}>{panel ? '✕' : mobile ? '← Заявки' : '← К списку'}</Button>
        </Flex>
        {loading && <Spin size="small" aria-label="Загружаем карточку" />}
        {hiddenByFilter && <Alert type="info" showIcon title="Эта заявка не подходит к текущему фильтру"
          description="Заявка остаётся открытой. Можно изменить условия или сбросить фильтры, чтобы найти её в списке." />}
        {notice && <Alert type="info" showIcon title={notice} />}
        {error && <Alert type="warning" showIcon title="Не удалось обновить карточку" description={error}
          action={<Button size="small" onClick={onRetry}>Повторить</Button>} />}
        {deleteError && <Alert type={deleteUnknown ? 'warning' : 'error'} showIcon role="alert" title={deleteError}
          action={deleteUnknown ? <Button size="small" loading={deleting} onClick={() => deletion.current && void runDelete(deletion.current)}>Повторить удаление</Button> : undefined} />}
        {lead ? <>
          <Flex vertical={!mobile} align={mobile ? 'center' : undefined} wrap gap={12}>
            <div><Tag style={mobile ? { margin: 0 } : { background: token.colorPrimaryBg, borderColor: token.colorPrimaryBorder }}>{lead.status === 'new' ? 'Новый' : lead.status === 'in_progress' ? 'В работе' : 'Закрыт'}</Tag></div>
            <Typography.Text type="secondary">{sourceLabel(lead.source)}</Typography.Text>
            {lead.is_demo && <Tag color="blue" style={{ margin: 0 }}>Демо</Tag>}
            {!mobile && timestamp}
          </Flex>
          <LeadStatusControl lead={lead} onChanged={onChanged} onCurrent={onCurrent} />
          {mobile ? <>
            <Card styles={{ body: { padding: 16 } }}>{contacts}</Card>
            <Card styles={{ body: { padding: 16 } }}>{request}</Card>
            <Card styles={{ body: { padding: 16 } }}>{directions}</Card>
            <Card styles={{ body: { padding: 16 } }}>{note}</Card>
            {timestamp}
          </> : <><Divider style={{ margin: 0 }} />{contacts}{request}{directions}{note}</>}
          <Divider style={{ margin: 0 }} />
          <LeadEditForm lead={lead} tags={tags} mobile={mobile} onDirty={onDirty}
            onUpdated={onChanged} onCurrent={onCurrent} onManageTags={onManageTags} />
          <Flex wrap justify="space-between" gap={8}>
            <Button onClick={() => onManageTags()}>Управление тегами</Button>
            <Button danger disabled={deleting || deleteUnknown} loading={deleting} onClick={confirmDelete}>Удалить заявку</Button>
          </Flex>
        </> : !loading && <Result status="404" title="Не удалось загрузить карточку"
          subTitle={error ?? 'Заявка недоступна.'} extra={<Button onClick={onRetry}>Повторить</Button>} />}
      </Flex>
    </Card>
  </section>
}
