import { useRef, useState } from 'react'
import { Alert, Button, Flex, Form, Input, Modal, Popconfirm, Tag as AntTag, Typography } from 'antd'
import { ApiError, createTag, deleteTag } from './api'
import type { Tag } from './api'
import { useCRMAccess } from './AuthBoundary'
import { getFailureMessage } from './crmErrors'

type CreateOperation = { name: string; operationId: string }
type DeleteOperation = { tagId: number; operationId: string }

export function TagManagerModal({
  open,
  tags,
  mobile,
  error,
  loading,
  onClose,
  onRetry,
  onChanged,
  onTagAvailable,
  onDeleted,
}: {
  open: boolean
  tags: Tag[]
  mobile: boolean
  error: string | null
  loading: boolean
  onClose: () => void
  onRetry: () => void
  onChanged: () => void
  onTagAvailable?: (tag: Tag) => void
  onDeleted: (tagId: number, affectedLeads: number) => void
}) {
  const { controller } = useCRMAccess()
  const [form] = Form.useForm<{ name: string }>()
  const [saving, setSaving] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [unknown, setUnknown] = useState(false)
  const [message, setMessage] = useState<{ type: 'info' | 'success' | 'warning'; text: string } | null>(null)
  const [formError, setFormError] = useState<string | null>(null)
  const [duplicate, setDuplicate] = useState<Tag | null>(null)
  const createOperation = useRef<CreateOperation | null>(null)
  const deleteOperation = useRef<DeleteOperation | null>(null)
  const sent = useRef(false)

  const runCreate = async (operation: CreateOperation) => {
    setSaving(true)
    setUnknown(false)
    setFormError(null)
    sent.current = false
    try {
      const result = await controller.runWithAccess((csrf) => {
        sent.current = true
        return createTag(operation.name, operation.operationId, csrf)
      })
      createOperation.current = null
      setUnknown(false)
      setDuplicate(result.created ? null : result.tag)
      setMessage(result.created
        ? { type: 'success', text: `Тег «${result.tag.name}» добавлен в общий список.` }
        : { type: 'info', text: `Тег «${result.tag.name}» уже существует. Можно использовать его.` })
      if (result.created) {
        form.resetFields()
        if (onTagAvailable) {
          onTagAvailable(result.tag)
          onClose()
        }
      }
      onChanged()
    } catch (cause) {
      if (!sent.current || (cause instanceof ApiError && cause.status >= 400 && cause.status < 500)) {
        createOperation.current = null
        setUnknown(false)
        setFormError(getFailureMessage(cause))
      } else {
        setUnknown(true)
        setFormError('Результат создания не подтверждён. Повторите ту же операцию.')
      }
    } finally {
      setSaving(false)
    }
  }

  const submit = async ({ name }: { name: string }) => {
    if (!createOperation.current) {
      createOperation.current = { name: name.trim(), operationId: crypto.randomUUID() }
    }
    await runCreate(createOperation.current)
  }

  const runDelete = async (operation: DeleteOperation) => {
    setDeleting(true)
    sent.current = false
    setFormError(null)
    try {
      const result = await controller.runWithAccess((csrf) => {
        sent.current = true
        return deleteTag(operation.tagId, operation.operationId, csrf)
      })
      deleteOperation.current = null
      setUnknown(false)
      setMessage({ type: 'success', text: `Тег удалён. Он снят с ${result.affected_leads} заявок.` })
      onChanged()
      onDeleted(operation.tagId, result.affected_leads)
    } catch (cause) {
      if (!sent.current || (cause instanceof ApiError && cause.status >= 400 && cause.status < 500)) {
        deleteOperation.current = null
        setUnknown(false)
        setFormError(getFailureMessage(cause))
      } else {
        setUnknown(true)
        setFormError('Результат удаления не подтверждён. Повторите ту же операцию.')
      }
    } finally {
      setDeleting(false)
    }
  }

  const retry = () => {
    if (createOperation.current) void runCreate(createOperation.current)
    else if (deleteOperation.current) void runDelete(deleteOperation.current)
  }

  return <Modal
    open={open}
    title="Управление тегами"
    onCancel={() => { if (!saving && !deleting && !unknown) onClose() }}
    closable={!saving && !deleting && !unknown}
    maskClosable={!saving && !deleting && !unknown}
    footer={null}
    width={mobile ? 'calc(100vw - 24px)' : 560}
    destroyOnHidden
    styles={{ body: { maxHeight: '65vh', overflowY: 'auto' } }}
  >
    <Flex vertical gap="middle">
      <Typography.Paragraph type="secondary" style={{ margin: 0 }}>
        Новые теги сразу появляются в CRM. Удаление снимает тег со всех заявок.
      </Typography.Paragraph>
      {message && <Alert type={message.type} showIcon title={message.text}
        action={duplicate && onTagAvailable ? <Button size="small" onClick={() => {
          onTagAvailable(duplicate)
          onClose()
        }}>Использовать тег</Button> : undefined} />}
      {formError && <Alert type={unknown ? 'warning' : 'error'} showIcon role="alert" title={formError}
        action={unknown ? <Button size="small" loading={saving || deleting} onClick={retry}>Повторить</Button> : undefined} />}
      {error && <Alert type="warning" showIcon title="Не удалось обновить список тегов" description={error}
        action={<Button size="small" disabled={loading} onClick={onRetry}>Повторить</Button>} />}
      <Form form={form} layout="vertical" onFinish={(values) => void submit(values)}>
        <Form.Item label="Название нового тега" name="name" rules={[
          { required: true, whitespace: true, message: 'Введите название тега.' },
          { max: 40, message: 'Не более 40 символов.' },
        ]}>
          <Input aria-label="Название нового тега" maxLength={40} showCount disabled={saving || deleting || unknown} />
        </Form.Item>
        <Button type="primary" htmlType="submit" loading={saving} disabled={deleting || unknown}>Создать тег</Button>
      </Form>
      <Flex vertical gap={8} component="section" aria-label="Список тегов">
        <Typography.Text strong>Теги в CRM</Typography.Text>
        {tags.map((tag) => <Flex key={tag.id} align="center" justify="space-between" gap={12} wrap>
          <Flex align="center" wrap gap={8}>
            <AntTag style={{ margin: 0, whiteSpace: 'normal', overflowWrap: 'anywhere' }}>{tag.name}</AntTag>
            <Typography.Text type="secondary">{tag.lead_count ?? 0} заявок</Typography.Text>
            {tag.is_system && <Typography.Text type="secondary">Исходный</Typography.Text>}
          </Flex>
          {!tag.is_system && <Popconfirm
            title={`Удалить тег «${tag.name}»?`}
            description={`Он будет снят со всех ${tag.lead_count ?? 0} заявок. Сами заявки и другие теги сохранятся.`}
            okText="Удалить тег"
            cancelText="Отмена"
            okButtonProps={{ danger: true, disabled: deleting || saving || unknown }}
            onConfirm={() => {
              const operation = deleteOperation.current ?? { tagId: tag.id, operationId: crypto.randomUUID() }
              deleteOperation.current = operation
              void runDelete(operation)
            }}
          >
            <Button type="link" danger disabled={deleting || saving || unknown} aria-label={`Удалить тег ${tag.name}`}>Удалить</Button>
          </Popconfirm>}
        </Flex>)}
      </Flex>
    </Flex>
  </Modal>
}
