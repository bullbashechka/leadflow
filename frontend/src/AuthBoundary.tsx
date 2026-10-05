import { createContext, useContext, useEffect, useRef, useState, useSyncExternalStore } from 'react'
import type { KeyboardEvent, ReactNode } from 'react'
import { Alert, Button, Card, Flex, Form, Input, Modal, Spin, Typography } from 'antd'
import { createBrowserAuth } from './browserAuth'
import type { AuthController, AuthState } from './auth'

const AccessContext = createContext<{ controller: AuthController; state: AuthState } | null>(null)

export function useCRMAccess() {
  const value = useContext(AccessContext)
  if (!value) throw new Error('CRM access requires AuthBoundary')
  return value
}

function LoginForm({ controller, state }: { controller: AuthController; state: AuthState }) {
  const [form] = Form.useForm<{ password: string }>()
  useEffect(() => {
    if (state.kind === 'authenticated') form.resetFields()
  }, [form, state.kind])

  const keepModalFocus = (event: KeyboardEvent<HTMLDivElement>) => {
    if (!state.hasOpened || event.key !== 'Tab') return
    // The library's focus lock does not stop Tab from entering browser chrome.
    const controls = [...event.currentTarget.querySelectorAll<HTMLElement>('input, button, [tabindex="0"]')]
      .filter((element) => !element.hasAttribute('disabled') && element.getClientRects().length > 0)
    const first = controls[0]
    const last = controls.at(-1)
    const target = event.shiftKey && document.activeElement === first ? last
      : !event.shiftKey && document.activeElement === last ? first : null
    if (target) {
      event.preventDefault()
      target.focus()
    }
  }

  if (state.kind === 'checking') {
    return <Flex vertical gap="middle" onKeyDownCapture={keepModalFocus}>
      {state.error
        ? <Alert type="error" showIcon title="Не удалось проверить доступ" description={state.error} role="alert" />
        : <Flex align="center" gap="middle" role="status"><Spin /><Typography.Text>Проверяем доступ…</Typography.Text></Flex>}
      {state.error && <Button block onClick={() => void controller.refresh()}>Повторить</Button>}
    </Flex>
  }
  if (state.kind === 'logout-pending') {
    return <Flex vertical gap="middle" onKeyDownCapture={keepModalFocus}>
      <Alert type="warning" showIcon role="status" title="Данные скрыты"
        description={state.busy ? 'Подтверждаем выход…' : 'Не удалось подтвердить выход. Повторим попытку при восстановлении связи.'} />
      {state.error && <Typography.Paragraph role="alert" style={{ margin: 0 }}>{state.error}</Typography.Paragraph>}
      <Button block type="primary" loading={state.busy} onClick={() => void controller.refresh()}>Повторить выход</Button>
    </Flex>
  }
  return <Flex vertical gap="middle" onKeyDownCapture={keepModalFocus}>
    <Typography.Paragraph type="secondary" style={{ margin: 0 }}>
      Введите общий демонстрационный пароль.
    </Typography.Paragraph>
    {state.error && <Alert showIcon type={state.offline ? 'warning' : 'error'} role="alert" title={state.error} />}
    <Form form={form} layout="vertical" requiredMark={false} onFinish={({ password }) => void controller.logIn(password)}>
      <Form.Item name="password" label="Пароль" rules={[{ required: true, message: 'Введите пароль.' }]}>
        <Input.Password autoComplete="current-password" autoFocus disabled={state.busy} maxLength={1024} />
      </Form.Item>
      <Button block type="primary" htmlType="submit" loading={state.busy}>Войти</Button>
    </Form>
  </Flex>
}

export function AuthBoundary({ children }: { children: ReactNode }) {
  const [access] = useState(createBrowserAuth)
  const state = useSyncExternalStore(access.controller.subscribe, access.controller.getSnapshot)
  const lastFocus = useRef<HTMLElement | null>(null)
  useEffect(() => access.connect(), [access])
  const open = state.kind === 'authenticated'
  const title = state.kind === 'logout-pending' ? 'Выход из CRM' : 'Вход в CRM'

  return <AccessContext.Provider value={{ controller: access.controller, state }}>
    <div hidden={!open} inert={!open} aria-hidden={!open} onFocusCapture={(event) => { lastFocus.current = event.target as HTMLElement }}>
      {state.hasOpened && children}
    </div>
    {!state.hasOpened
      ? <Card className="crm-login" styles={{ body: { padding: 24 }, header: { padding: 24 } }} title={<Flex vertical gap={8}>
        <Typography.Text type="secondary">Leadflow</Typography.Text>
        <Typography.Title level={1} style={{ margin: 0, fontSize: 32 }}>{title}</Typography.Title>
      </Flex>}>
        <LoginForm controller={access.controller} state={state} />
      </Card>
      : <Modal open={!open} title={title} closable={false} keyboard={false} mask={{ closable: false }}
        footer={null} destroyOnHidden={false} focusable={{ trap: true, focusTriggerAfterClose: false }} width={440}
        afterOpenChange={(visible) => { if (!visible && lastFocus.current?.isConnected) lastFocus.current.focus({ preventScroll: true }) }}>
        <LoginForm controller={access.controller} state={state} />
      </Modal>}
  </AccessContext.Provider>
}
