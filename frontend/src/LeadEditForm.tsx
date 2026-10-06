import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Alert, App as AntApp, Button, Card, Flex, Form, Input, Radio, Select, Typography } from 'antd'
import { ApiError, updateLead } from './api'
import type { Lead, LeadUpdate, Tag } from './api'
import { useCRMAccess } from './AuthBoundary'
import { getFailureMessage } from './crmErrors'

type Values = { name: string; contacts: string[]; request: string; note: string; tag_ids: number[] }
type Field = keyof Values
type Attempt = { payload: LeadUpdate; original: Values; indexes: number[] }
const fields: Array<{ key: Field; label: string }> = [
  { key: 'name', label: 'Имя' },
  { key: 'contacts', label: 'Контакты' },
  { key: 'request', label: 'Запрос' },
  { key: 'note', label: 'Заметка' },
  { key: 'tag_ids', label: 'Теги' },
]

function valuesFrom(lead: Lead): Values {
  return {
    name: lead.name,
    contacts: lead.contacts.map(({ value }) => value),
    request: lead.request,
    note: lead.note,
    tag_ids: lead.tags.map(({ id }) => id).sort((first, second) => first - second),
  }
}

function normalize(field: Field, value: unknown) {
  if (field === 'tag_ids') return JSON.stringify(Array.isArray(value) ? [...value].sort() : [])
  if (field === 'contacts') return JSON.stringify(value)
  return JSON.stringify(value)
}

function readValue(field: Field, values: Values) {
  return values[field]
}

function displayValue(field: Field, value: unknown, tags: Tag[]) {
  if (field === 'tag_ids') {
    const ids = Array.isArray(value) ? value as number[] : []
    const names = ids.map((id) => tags.find((tag) => tag.id === id)?.name ?? 'Удалённый тег')
    return names.length ? names.join(', ') : 'Без тегов'
  }
  if (field === 'contacts') return (Array.isArray(value) ? value : []).join('\n') || 'Нет контактов'
  return typeof value === 'string' && value ? value : 'Не указано'
}

export function LeadEditForm({
  lead,
  tags,
  mobile,
  deletionBlocked,
  canSave,
  onDirty,
  onBlocked,
  onUpdated,
  onCurrent,
  onManageTags,
}: {
  lead: Lead
  tags: Tag[]
  mobile: boolean
  deletionBlocked: boolean
  canSave: () => boolean
  onDirty: (dirty: boolean) => void
  onBlocked: (blocked: boolean) => void
  onUpdated: (lead: Lead) => void
  onCurrent: (lead: Lead) => void
  onManageTags: (onSelect: (tag: Tag) => void) => void
}) {
  const { controller, state: accessState } = useCRMAccess()
  const { modal } = AntApp.useApp()
  const confirmation = useRef<{ destroy: () => void } | null>(null)
  useEffect(() => {
    if (accessState.kind !== 'authenticated') confirmation.current?.destroy()
  }, [accessState.kind])
  useEffect(() => () => confirmation.current?.destroy(), [])
  const [form] = Form.useForm<Values>()
  const baseline = useRef(lead)
  const frozen = useRef<Attempt | null>(null)
  const sent = useRef(false)
  const [editing, setEditing] = useState(false)
  const [saving, setSaving] = useState(false)
  const [unknown, setUnknown] = useState(false)
  const [remote, setRemote] = useState<Lead | null>(null)
  const [choices, setChoices] = useState<Partial<Record<Field, 'current' | 'mine'>>>({})
  const [formError, setFormError] = useState<string | null>(null)

  useEffect(() => {
    if (lead.id !== baseline.current.id) {
      baseline.current = lead
      form.setFieldsValue(valuesFrom(lead))
      setEditing(false)
      setRemote(null)
    } else if (lead.version !== baseline.current.version && !frozen.current) {
      const before = valuesFrom(baseline.current)
      const latest = valuesFrom(lead)
      const editableFieldsUnchanged = fields.every(({ key }) =>
        normalize(key, readValue(key, before)) === normalize(key, readValue(key, latest)))
      const currentValues = form.getFieldsValue(true) as Values
      const draftIsClean = fields.every(({ key }) =>
        normalize(key, readValue(key, currentValues)) === normalize(key, readValue(key, before)))
      if (editableFieldsUnchanged || draftIsClean) {
        baseline.current = lead
        if (draftIsClean) form.setFieldsValue(latest)
      }
    }
  }, [form, lead])

  const conflictFields = useMemo(() => {
    if (!remote || !frozen.current) return []
    const draft = frozen.current.payload
    const current = valuesFrom(remote)
    const original = frozen.current.original
    return fields.flatMap(({ key, label }) => {
      const mine = readValue(key, draft)
      const theirs = readValue(key, current)
      const before = readValue(key, original)
      if (normalize(key, mine) === normalize(key, theirs)
        || normalize(key, mine) === normalize(key, before)
        || normalize(key, theirs) === normalize(key, before)) return []
      return [{ field: key, label, mine, current: theirs }]
    })
  }, [remote])

  const isDirty = useCallback(() => {
    if (!editing) return false
    const values = form.getFieldsValue(true) as Values
    const current = valuesFrom(baseline.current)
    return fields.some(({ key }) => normalize(key, readValue(key, values)) !== normalize(key, readValue(key, current)))
  }, [editing, form])

  const updateDirty = () => onDirty(isDirty())

  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => {
      if (!isDirty() && !saving && !unknown) return
      event.preventDefault()
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [isDirty, saving, unknown])

  const send = async (attempt: Attempt) => {
    if (!canSave()) return
    const snapshot = attempt.payload
    frozen.current = attempt
    onBlocked(true)
    setFormError(null)
    setSaving(true)
    sent.current = false
    try {
      const result = await controller.runWithAccess((csrfToken) => {
        sent.current = true
        return updateLead(lead.id, snapshot, csrfToken)
      })
      frozen.current = null
      setRemote(null)
      setUnknown(false)
      setChoices({})
      baseline.current = result.lead
      form.setFieldsValue(valuesFrom(result.lead))
      setEditing(false)
      onDirty(false)
      onBlocked(false)
      onUpdated(result.lead)
    } catch (error) {
      if (error instanceof ApiError && error.code === 'version_conflict' && error.currentLead) {
        frozen.current = attempt
        setUnknown(false)
        onBlocked(false)
        setRemote(error.currentLead)
        onCurrent(error.currentLead)
        setChoices({})
        setFormError(null)
      } else if (error instanceof ApiError && error.code === 'lead_deleted') {
        setUnknown(false)
        onBlocked(false)
        frozen.current = null
        setFormError('Заявка уже удалена. Скопируйте нужный текст перед выходом.')
      } else if ((!sent.current && !unknown) || (error instanceof ApiError && error.status >= 400 && error.status < 500)) {
        setUnknown(false)
        onBlocked(false)
        frozen.current = null
        setFormError(getFailureMessage(error))
        if (error instanceof ApiError) {
          const updates: Parameters<typeof form.setFields>[0] = []
          for (const [key, messages] of Object.entries(error.fieldErrors)) {
            const contact = /^contacts\.(\d+)$/.exec(key)
            if (contact) {
              const index = attempt.indexes[Number(contact[1])]
              if (index !== undefined) updates.push({ name: ['contacts', index], errors: messages })
              continue
            }
            if (key === 'name' || key === 'contacts' || key === 'request' || key === 'note' || key === 'tag_ids') {
              updates.push({ name: key, errors: messages })
            }
          }
          form.setFields(updates)
        }
      } else {
        setUnknown(true)
        setFormError('Не удалось подтвердить сохранение. Проверьте и повторите эту же операцию.')
      }
    } finally {
      setSaving(false)
    }
  }

  const startEditing = () => {
    baseline.current = lead
    form.setFieldsValue(valuesFrom(lead))
    setFormError(null)
    setRemote(null)
    setChoices({})
    setEditing(true)
  }

  const discard = () => {
    frozen.current = null
    form.setFieldsValue(valuesFrom(baseline.current))
    setEditing(false)
    setRemote(null)
    setFormError(null)
    setChoices({})
    onDirty(false)
  }

  const cancel = () => {
    if (saving || unknown) return
    if (!isDirty()) { discard(); return }
    confirmation.current = modal.confirm({
      title: 'Выйти без сохранения?', content: 'Изменения в карточке будут потеряны.',
      okText: 'Выйти без сохранения', cancelText: 'Остаться', onOk: discard,
    })
  }

  const submit = async (values: Values) => {
    const nonblank = values.contacts.map((contact, index) => ({ contact, index }))
      .filter(({ contact }) => contact.trim().length > 0)
    await send({ original: valuesFrom(baseline.current), indexes: nonblank.map(({ index }) => index), payload: {
      operation_id: crypto.randomUUID(),
      expected_version: baseline.current.version,
      name: values.name,
      contacts: nonblank.map(({ contact }) => contact),
      request: values.request,
      note: values.note ?? '',
      tag_ids: values.tag_ids ?? [],
    } })
  }

  const saveResolution = async () => {
    if (!canSave()) return
    if (!remote || !frozen.current) return
    if (conflictFields.some(({ field }) => !choices[field])) {
      setFormError('Выберите значение для каждого изменившегося поля.')
      return
    }
    const draft = frozen.current.payload
    const current = valuesFrom(remote)
    const original = frozen.current.original
    const merged = { ...current }
    for (const { key } of fields) {
      const mine = readValue(key, draft)
      const theirs = readValue(key, current)
      const before = readValue(key, original)
      if (choices[key] === 'mine') merged[key] = mine as never
      else if (choices[key] === 'current') merged[key] = theirs as never
      else if (normalize(key, mine) !== normalize(key, before)) merged[key] = mine as never
    }
    baseline.current = remote
    setRemote(null)
    setChoices({})
    form.setFieldsValue(merged)
    await submit(merged)
  }

  const retry = () => {
    if (frozen.current) void send(frozen.current)
  }

  const disabled = deletionBlocked || saving || unknown || Boolean(remote)

  return <Flex vertical gap="middle">
    {unknown && <Alert type="warning" showIcon role="alert" title="Результат сохранения неизвестен"
      description={formError} action={<Button onClick={retry} loading={saving} disabled={deletionBlocked}>Проверить и повторить</Button>} />}
    {!unknown && formError && <Alert type={remote ? 'error' : 'warning'} showIcon role="alert" title={formError} />}

    {editing ? <>
      <Form<Values> form={form} layout="vertical" requiredMark={false} onFinish={(values) => void submit(values)}
        onValuesChange={() => { setFormError(null); updateDirty() }}>
        <Form.Item label="Имя" name="name" rules={[
          { required: true, whitespace: true, message: 'Введите имя.' },
          { max: 100, message: 'Не более 100 символов.' },
        ]}>
          <Input autoComplete="name" disabled={disabled} />
        </Form.Item>
        <Form.Item label="Контакты">
          <Form.List name="contacts" rules={[{ validator: async (_, values: string[]) => {
            if (!values?.some((value) => value.trim().length > 0)) throw new Error('Укажите хотя бы один контакт.')
            if (values.filter(value => value.trim()).length > 20) throw new Error('Не более 20 контактов.')
          } }]}>
            {(items, { add, remove }, meta) => <Flex vertical gap="small">
              {items.map((item, index) => <Flex key={item.key} align="start" gap="small">
                <Form.Item name={item.name} style={{ flex: 1, marginBottom: 4 }}>
                  <Input aria-label={`Контакт ${index + 1}`} disabled={disabled} />
                </Form.Item>
                {items.length > 1 && <Button htmlType="button" disabled={disabled} onClick={() => remove(item.name)}
                  aria-label={`Удалить контакт ${index + 1}`}>Удалить</Button>}
              </Flex>)}
              <Button htmlType="button" block disabled={disabled || items.length >= 20} onClick={() => add('')}>Добавить контакт</Button>
              <Form.ErrorList errors={meta.errors} />
            </Flex>}
          </Form.List>
        </Form.Item>
        <Form.Item label="Запрос" name="request" rules={[
          { required: true, whitespace: true, message: 'Опишите запрос.' },
          { max: 2000, message: 'Не более 2000 символов.' },
        ]}>
          <Input.TextArea autoSize={{ minRows: 3, maxRows: 10 }} disabled={disabled} />
        </Form.Item>
        <Form.Item label="Заметка" name="note" rules={[{ max: 5000, message: 'Не более 5000 символов.' }]}>
          <Input.TextArea autoSize={{ minRows: 2, maxRows: 8 }} showCount disabled={disabled} />
        </Form.Item>
        <Form.Item label="Теги" name="tag_ids">
          <Select mode="multiple" allowClear placeholder="Можно без тегов" disabled={disabled}
            options={tags.map((tag) => ({ value: tag.id, label: tag.name }))} />
        </Form.Item>
        <Button htmlType="button" disabled={disabled} onClick={() => onManageTags((tag) => {
          const selected = form.getFieldValue('tag_ids') ?? []
          if (!selected.includes(tag.id)) form.setFieldsValue({ tag_ids: [...selected, tag.id] })
          updateDirty()
        })}>Создать тег</Button>
        <Flex wrap gap="small" justify="end">
          <Button htmlType="button" disabled={saving || unknown} onClick={cancel}>Отменить</Button>
          <Button type="primary" htmlType="submit" loading={saving} disabled={disabled}>Сохранить изменения</Button>
        </Flex>
      </Form>
      {remote && <Card size="small" title="Сверьте изменения" aria-label="Сравнение заявки">
        <Flex vertical gap="middle">
          <Typography.Text>Заявка изменилась в другой вкладке. Выберите значения для полей, которые меняли обе стороны.</Typography.Text>
          {conflictFields.map(({ field, label, mine, current }) => <Flex key={field} vertical gap={8}>
            <Typography.Text strong>{label}</Typography.Text>
            <Flex vertical={mobile} gap={8}>
              <Card size="small" title="Сейчас в CRM" style={{ flex: 1, minWidth: 0 }}>
                <Typography.Paragraph style={{ margin: 0, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>
                  {displayValue(field, current, tags)}
                </Typography.Paragraph>
              </Card>
              <Card size="small" title="Ваш вариант" style={{ flex: 1, minWidth: 0 }}>
                <Typography.Paragraph style={{ margin: 0, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>
                  {displayValue(field, mine, tags)}
                </Typography.Paragraph>
              </Card>
            </Flex>
            <Radio.Group aria-label={`Сохранить поле «${label}»`} value={choices[field]}
              onChange={(event) => setChoices((prior) => ({ ...prior, [field]: event.target.value }))}>
              <Radio.Button value="current">Оставить текущее</Radio.Button>
              <Radio.Button value="mine">Сохранить мой вариант</Radio.Button>
            </Radio.Group>
          </Flex>)}
          <Button type="primary" onClick={() => void saveResolution()} loading={saving} disabled={deletionBlocked}>
            Сверить и сохранить
          </Button>
        </Flex>
      </Card>}
    </> : <Flex justify="end">
      <Button type="primary" onClick={startEditing} disabled={deletionBlocked}>Редактировать</Button>
    </Flex>}
  </Flex>
}
