import { useEffect, useRef, useState } from 'react'
import { Alert, Button, Flex, Select } from 'antd'
import { ApiError, changeLeadStatus } from './api'
import type { Lead, LeadStatus, LeadStatusUpdate } from './api'
import { useCRMAccess } from './AuthBoundary'
import { getFailureMessage } from './crmErrors'
import { statusLabel } from './leadPresentation'

const statuses: LeadStatus[] = ['new', 'in_progress', 'closed']

export function LeadStatusControl({ lead, onChanged, onCurrent }: {
  lead: Lead
  onChanged: (lead: Lead) => void
  onCurrent: (lead: Lead) => void
}) {
  const { controller } = useCRMAccess()
  const baseline = useRef(lead)
  const frozen = useRef<LeadStatusUpdate | null>(null)
  const sent = useRef(false)
  const userChanged = useRef(false)
  const [selected, setSelected] = useState<LeadStatus>(lead.status)
  const [remote, setRemote] = useState<Lead | null>(null)
  const [saving, setSaving] = useState(false)
  const [unknown, setUnknown] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (lead.id !== baseline.current.id) {
      baseline.current = lead
      frozen.current = null
      userChanged.current = false
      setSelected(lead.status)
      setRemote(null)
      setUnknown(false)
      setError(null)
    } else if (lead.version !== baseline.current.version && !userChanged.current && !remote) {
      baseline.current = lead
      setSelected(lead.status)
    }
  }, [lead, remote])

  const send = async (operation: LeadStatusUpdate) => {
    frozen.current = operation
    sent.current = false
    setSaving(true)
    setError(null)
    try {
      const result = await controller.runWithAccess((csrf) => {
        sent.current = true
        return changeLeadStatus(lead.id, operation, csrf)
      })
      baseline.current = result.lead
      frozen.current = null
      userChanged.current = false
      setSelected(result.lead.status)
      setRemote(null)
      setUnknown(false)
      setError(null)
      onChanged(result.lead)
    } catch (cause) {
      if (cause instanceof ApiError && cause.code === 'version_conflict' && cause.currentLead) {
        setRemote(cause.currentLead)
        onCurrent(cause.currentLead)
        setError('Статус изменился в другой вкладке. Сверьте варианты и явно сохраните выбор.')
      } else if (cause instanceof ApiError && cause.code === 'lead_deleted') {
        setUnknown(false)
        setError('Заявка уже удалена.')
      } else if (!sent.current || (cause instanceof ApiError && cause.status >= 400 && cause.status < 500)) {
        frozen.current = null
        setUnknown(false)
        setError(getFailureMessage(cause))
      } else {
        setUnknown(true)
        setError('Результат смены статуса не подтверждён. Повторите ту же операцию.')
      }
    } finally {
      setSaving(false)
    }
  }

  const save = () => {
    if (selected === baseline.current.status) return
    void send({ operation_id: crypto.randomUUID(), expected_version: baseline.current.version, status: selected })
  }

  const resolve = (status: LeadStatus) => {
    if (!remote) return
    baseline.current = remote
    setSelected(status)
    setRemote(null)
    userChanged.current = true
    void send({ operation_id: crypto.randomUUID(), expected_version: remote.version, status })
  }

  return <Flex vertical gap={8}>
    {remote && <Alert type="warning" showIcon role="alert" title="Сверьте статус"
      description={`Сейчас в CRM: «${statusLabel(remote.status)}». Вы выбрали: «${statusLabel(selected)}».`}
      action={<Flex wrap gap={8}>
        <Button size="small" disabled={saving} onClick={() => resolve(remote.status)}>Оставить текущий</Button>
        <Button size="small" type="primary" disabled={saving} onClick={() => resolve(selected)}>Сохранить мой статус</Button>
      </Flex>} />}
    {unknown && <Alert type="warning" showIcon title={error}
      action={<Button size="small" loading={saving} onClick={() => frozen.current && void send(frozen.current)}>Повторить</Button>} />}
    {!unknown && error && !remote && <Alert type="error" showIcon role="alert" title={error} />}
    <Flex wrap gap={8} align="center">
      <Select<LeadStatus> aria-label="Новый статус" value={selected} disabled={saving || unknown || Boolean(remote)}
        options={statuses.map((status) => ({ value: status, label: statusLabel(status) }))}
        onChange={(value) => { userChanged.current = value !== baseline.current.status; setSelected(value); setError(null) }} />
      <Button type="primary" disabled={saving || unknown || Boolean(remote) || selected === baseline.current.status}
        loading={saving} onClick={save}>Сохранить статус</Button>
    </Flex>
  </Flex>
}
