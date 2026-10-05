import { Button, Card, Flex, Table, Tag, theme, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import type { Lead } from './api'
import { formatListDate, sourceLabel, statusLabel } from './leadPresentation'

type Props = {
  leads: Lead[]
  selectedId?: string
  expanded: Set<string>
  mobile: boolean
  onExpand: (id: string) => void
  onOpen: (lead: Lead) => void
}

function LeadIdentity({ lead, expanded, onExpand, onOpen }: {
  lead: Lead; expanded: boolean; onExpand: Props['onExpand']; onOpen: Props['onOpen']
}) {
  const { token } = theme.useToken()
  const contacts = expanded ? lead.contacts : lead.contacts.slice(0, 2)
  return <Flex vertical gap={6} className="lead-identity">
    <Button type="link" className="lead-name-button" aria-label={`Открыть карточку: ${lead.name}`}
      style={{ color: token.colorText, fontWeight: 700 }} onClick={() => onOpen(lead)}>{lead.name}</Button>
    <Flex vertical gap={2}>
      {contacts.map((contact, index) => <Typography.Text type="secondary" style={{ fontSize: 14, overflowWrap: 'anywhere' }} key={index}>{contact.value}</Typography.Text>)}
      {!expanded && lead.contacts.length > 2 && <Button type="link" size="small" style={{ padding: 0, alignSelf: 'start' }} onClick={() => onExpand(lead.id)}>Ещё {lead.contacts.length - 2}</Button>}
    </Flex>
    <Typography.Text type="secondary" style={{ fontSize: 14 }}>{sourceLabel(lead.source)} · {statusLabel(lead.status)}</Typography.Text>
  </Flex>
}

function Directions({ lead }: { lead: Lead }) {
  return <Flex wrap gap={6}>{lead.tags.length ? lead.tags.map(tag => <Tag key={tag.id} style={{ margin: 0, whiteSpace: 'normal', overflowWrap: 'anywhere' }}>{tag.name}</Tag>) : <Typography.Text type="secondary">Без направления</Typography.Text>}</Flex>
}

export function LeadListView({ leads, selectedId, expanded, mobile, onExpand, onOpen }: Props) {
  const { token } = theme.useToken()
  const identity = (lead: Lead) => <LeadIdentity lead={lead} expanded={expanded.has(lead.id)} onExpand={onExpand} onOpen={onOpen} />
  const request = (lead: Lead) => <Typography.Paragraph ellipsis={{ rows: 2 }} style={{ margin: 0, overflowWrap: 'anywhere' }}>{lead.request}</Typography.Paragraph>
  const openRow = (event: React.MouseEvent, lead: Lead) => {
    if ((event.target as HTMLElement).closest('button, a')) return
    if (window.getSelection()?.toString()) return
    onOpen(lead)
  }
  if (mobile) return <Flex vertical gap={12}>
    {leads.map(lead => <Card key={lead.id} data-lead-id={lead.id} className="lead-mobile-card"
      styles={{ body: { padding: 20 } }}
      style={{ background: lead.id === selectedId ? token.colorPrimaryBg : token.colorBgContainer,
        borderInlineStart: `4px solid ${lead.id === selectedId ? token.colorPrimary : 'transparent'}`, cursor: 'pointer' }}
      onClick={event => openRow(event, lead)}>
      <Flex vertical gap={12}>
        {identity(lead)}
        {request(lead)}
        <Flex wrap align="center" justify="space-between" gap={8}>
          <Directions lead={lead} />
          <Typography.Text type="secondary" style={{ fontSize: 14 }}>{formatListDate(lead.created_at)}</Typography.Text>
        </Flex>
      </Flex>
    </Card>)}
  </Flex>
  const columns: ColumnsType<Lead> = [
    { title: 'Имя', key: 'name', width: '30%', render: (_, lead) => identity(lead),
      onCell: lead => ({ style: { borderInlineStart: `4px solid ${lead.id === selectedId ? token.colorPrimary : 'transparent'}` } }) },
    { title: 'Запрос', key: 'request', width: '30%', render: (_, lead) => request(lead) },
    { title: 'Направление', key: 'tags', width: '21%', render: (_, lead) => <Directions lead={lead} /> },
    { title: 'Добавлено', key: 'created_at', width: '19%', render: (_, lead) => <Typography.Text style={{ fontSize: 14 }}>{formatListDate(lead.created_at)}</Typography.Text> },
  ]
  return <Card className="lead-table-surface" styles={{ body: { padding: 0 } }}>
    <Table<Lead> rowKey="id" tableLayout="fixed" columns={columns} dataSource={leads} pagination={false}
      onRow={lead => ({ 'data-lead-id': lead.id, onClick: event => openRow(event, lead),
        style: { cursor: 'pointer', background: lead.id === selectedId ? token.colorPrimaryBg : undefined, verticalAlign: 'top' } })} />
  </Card>
}
