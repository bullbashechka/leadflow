import { useEffect, useState } from 'react'
import { Alert, Button, Card, Divider, Flex, Spin, Typography } from 'antd'
import { checkHealth } from './api'
import { AuthBoundary, useCRMAccess } from './AuthBoundary'
import './App.css'

type Connection = 'checking' | 'ready' | 'error'

function Workspace() {
  const { controller: access, state } = useCRMAccess()
  const [attempt, setAttempt] = useState(0)
  const [connection, setConnection] = useState<Connection>('checking')

  useEffect(() => {
    const controller = new AbortController()
    checkHealth({ signal: controller.signal }).then(
      () => { if (!controller.signal.aborted) setConnection('ready') },
      () => { if (!controller.signal.aborted) setConnection('error') },
    )
    return () => controller.abort()
  }, [attempt])

  const retry = () => {
    setConnection('checking')
    setAttempt((value) => value + 1)
  }

  return <Flex vertical gap={24}>
    <Flex align="center" justify="space-between" gap="middle" wrap>
      <Typography.Title level={1} style={{ margin: 0 }}>CRM</Typography.Title>
      <Button onClick={() => void access.logOut()}>Выйти</Button>
    </Flex>
    {state.offline && <Alert type="warning" showIcon role="status" title="Нет связи с сервером"
      description="Доступ сохранён до окончания срока входа. Проверка связи повторяется автоматически." />}
    <Typography.Paragraph type="secondary" style={{ margin: 0 }}>
      Доступ открыт. Список заявок и формы появятся позже.
    </Typography.Paragraph>
    <section aria-labelledby="connection-title">
      <Card title={<Typography.Title level={2} id="connection-title" style={{ margin: 0 }}>Соединение с сервером</Typography.Title>}>
        <Flex vertical gap={24}>
          <Alert
            type={connection === 'ready' ? 'success' : connection === 'error' ? 'error' : 'info'}
            showIcon icon={connection === 'checking' ? <Spin size="small" /> : undefined}
            role="status" aria-live="polite" aria-atomic="true"
            title={connection === 'checking' ? 'Проверяем соединение…' : connection === 'ready' ? 'Соединение установлено' : 'Не удалось подключиться'}
            description={connection === 'checking' ? 'Это займёт несколько секунд.' : connection === 'ready' ? 'Результат последней проверки: сервер и база данных доступны.' : 'Проверьте соединение и повторите попытку.'}
          />
          <Button type="primary" onClick={retry} disabled={connection === 'checking'} block>
            {connection === 'error' ? 'Повторить' : 'Проверить ещё раз'}
          </Button>
        </Flex>
      </Card>
    </section>
  </Flex>
}

function App() {
  return <Flex vertical className="page">
      <header className="page-header">
        <Flex align="center" justify="space-between" gap="middle" wrap>
          <Typography.Link href="/" aria-label="Leadflow — главная" strong>
            Leadflow
          </Typography.Link>
        </Flex>
        <Divider style={{ margin: '24px 0 0' }} />
      </header>
      <main className="page-content">
        <AuthBoundary><Workspace /></AuthBoundary>
      </main>
      <footer className="page-footer">
        <Typography.Text type="secondary">Leadflow · Заявки агентства</Typography.Text>
      </footer>
    </Flex>
}

export default App
