import { useCallback, useEffect, useRef, useState } from 'react'
import { Alert, App as AntApp, Button, Card, Flex, Form, Input, Select, Typography } from 'antd'
import { ApiError, createLead } from './api'
import type { Lead, Tag } from './api'
import { useCRMAccess } from './AuthBoundary'
import { getFailureMessage } from './crmErrors'

type FormValues = { name: string; contacts: string[]; request: string; tag_ids: number[] }
type FormFieldError =
  | { name: 'name' | 'request' | 'tag_ids'; errors: string[] }
  | { name: ['contacts', number]; errors: string[] }

export function ManualLeadForm({
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
    Добавить заявку
  </Typography.Title>

  return <Flex vertical gap="middle" className="crm-subview crm-create-view">
    <Flex wrap align="center" justify="space-between" gap="middle">
      {title}
      <Button onClick={requestExit} disabled={saving || unknown}>К списку заявок</Button>
    </Flex>
    {offline && <Alert type="warning" showIcon title="Нет связи с сервером" description="Текст формы сохранён в этой вкладке. Дождитесь связи, чтобы отправить заявку." />}
    {tagsError && <Alert type="warning" showIcon title="Не удалось загрузить направления" description={tagsError}
      action={<Button size="small" onClick={onRetryTags} disabled={tagsLoading}>Повторить</Button>} />}
    {tagsError && tags.length === 0 && <Typography.Text type="secondary">Можно создать заявку без направления.</Typography.Text>}
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
        <Form.Item label="Направления" name="tag_ids">
          <Select
            mode="multiple"
            allowClear
            placeholder={tagsLoading ? 'Загружаем…' : 'Необязательно'}
            options={tags.map((tag) => ({ value: tag.id, label: tag.name }))}
            loading={tagsLoading}
            disabled={saving || unknown || conflict || tags.length === 0}
          />
        </Form.Item>
        <Button type="primary" htmlType="submit" loading={saving} disabled={unknown || conflict || offline} block>
          Сохранить заявку
        </Button>
      </Form>
    </Card>
  </Flex>
}

