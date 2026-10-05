import { useLayoutEffect, useRef } from 'react'
import type { Ref } from 'react'
import { Alert, Button, Card, Divider, Flex, Result, Spin, Tag, theme, Typography } from 'antd'
import type { Lead } from './api'
import { formatExactDate, timezoneLabel, sourceLabel, statusLabel } from './leadPresentation'

function contactHref(type: Lead['contacts'][number]['type'], value: string) {
  if (type === 'phone') return `tel:${value.trim().replace(/[ ()-]/g, '')}`
  if (type === 'email') return `mailto:${value.trim().split('@').map(encodeURIComponent).join('@')}`
  const text = value.trim()
  const username = text.startsWith('@') ? text.slice(1) : new URL(text.startsWith('http') ? text : `https://${text}`).pathname.split('/').filter(Boolean)[0]
  return `https://t.me/${encodeURIComponent(username)}`
}

export function LeadDetails({ lead, loading, error, hiddenByFilter, panel, headingRef, onClose, onRetry }: {
  lead: Lead | null
  loading: boolean
  error: string | null
  hiddenByFilter: boolean
  panel: boolean
  headingRef: Ref<HTMLHeadingElement>
  onClose: () => void
  onRetry: () => void
}) {
  const { token } = theme.useToken()
  const container = useRef<HTMLElement>(null)
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
  return <section ref={container} className="crm-detail" aria-label="Карточка заявки">
    <Card styles={{ body: { padding: 24 } }}>
      <Flex vertical gap={24}>
        <Flex vertical={!panel} align={panel ? 'start' : 'stretch'} justify="space-between" gap={8}>
          {lead ? <Typography.Title level={2} ref={headingRef} tabIndex={-1} style={{ margin: 0, minWidth: 0, overflowWrap: 'anywhere' }}>{lead.name}</Typography.Title>
            : <Typography.Text type="secondary">Карточка заявки</Typography.Text>}
          <Button type="text" aria-label="К списку заявок" title="К списку заявок" style={{ flexShrink: 0, order: panel ? undefined : -1, alignSelf: panel ? 'start' : 'end' }} onClick={onClose}>{panel ? '✕' : '← К списку'}</Button>
        </Flex>
        {loading && <Spin size="small" aria-label="Загружаем карточку" />}
        {hiddenByFilter && <Alert type="info" showIcon title="Эта заявка не подходит к текущему фильтру"
          description="Заявка сохранена. Сбросьте фильтр, чтобы увидеть её в списке." />}
        {error && <Alert type="warning" showIcon title="Не удалось обновить карточку" description={error}
          action={<Button size="small" onClick={onRetry}>Повторить</Button>} />}
        {lead ? <>
          <Flex vertical gap={12}>
            <div><Tag style={{ background: token.colorPrimaryBg, borderColor: token.colorPrimaryBorder }}>{statusLabel(lead.status)}</Tag></div>
            <Typography.Text type="secondary">{sourceLabel(lead.source)}</Typography.Text>
            <Typography.Text type="secondary">{formatExactDate(lead.created_at)} · {timezoneLabel()}</Typography.Text>
          </Flex>
          <Divider style={{ margin: 0 }} />
          <Flex vertical gap={10} component="section">
            <Typography.Text type="secondary">Контакты</Typography.Text>
            {lead.contacts.map((contact, index) => <Typography.Link key={`${contact.type}-${contact.value}-${index}`}
              href={contactHref(contact.type, contact.value)}
              target={contact.type === 'telegram' ? '_blank' : undefined}
              rel={contact.type === 'telegram' ? 'noreferrer' : undefined}
              style={{ overflowWrap: 'anywhere' }}>{contact.value}</Typography.Link>)}
          </Flex>
          <Flex vertical gap={10} component="section">
            <Typography.Text type="secondary">Направления</Typography.Text>
            <Flex wrap gap={6}>{lead.tags.length ? lead.tags.map(tag => <Tag key={tag.id} style={{ margin: 0, maxWidth: '100%', whiteSpace: 'normal', overflowWrap: 'anywhere' }}>{tag.name}</Tag>) : <Typography.Text>Без направления</Typography.Text>}</Flex>
          </Flex>
          <Flex vertical gap={10} component="section">
            <Typography.Text type="secondary">Запрос</Typography.Text>
            <Typography.Paragraph style={{ margin: 0, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{lead.request}</Typography.Paragraph>
          </Flex>
        </> : !loading && <Result status="404" title="Не удалось загрузить карточку"
          subTitle={error ?? 'Заявка недоступна.'} extra={<Button onClick={onRetry}>Повторить</Button>} />}
      </Flex>
    </Card>
  </section>
}
