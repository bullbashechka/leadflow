import { useEffect, useState } from 'react'
import { Alert, Button, Card, Divider, Flex, Space, Spin, Typography } from 'antd'
import { checkHealth } from './api'
import './App.css'

type Connection = 'checking' | 'ready' | 'error'

function App() {
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

  return (
    <Flex vertical className="page">
      <header className="page-header">
        <Flex align="center" justify="space-between" gap="middle" wrap>
          <Typography.Link href="/" aria-label="Leadflow — главная" strong>
            Leadflow
          </Typography.Link>
          <Typography.Text type="secondary">Локальная разработка</Typography.Text>
        </Flex>
        <Divider style={{ margin: '24px 0 0' }} />
      </header>
      <main className="page-content">
        <Flex vertical gap={24}>
          <Space orientation="vertical" size="small">
            <Typography.Text type="secondary">Этап 0</Typography.Text>
            <Typography.Title level={1} style={{ margin: 0 }}>Основа проекта</Typography.Title>
            <Typography.Paragraph type="secondary" style={{ margin: 0 }}>
              Готовим CRM для заявок агентства. Список заявок и формы появятся на следующих этапах.
            </Typography.Paragraph>
          </Space>
          <section aria-labelledby="connection-title">
            <Card title={
              <Typography.Title level={2} id="connection-title" style={{ margin: 0 }}>
                Соединение с сервером
              </Typography.Title>
            }>
              <Flex vertical gap={24}>
                <Alert
                  type={connection === 'ready' ? 'success' : connection === 'error' ? 'error' : 'info'}
                  showIcon
                  icon={connection === 'checking' ? <Spin size="small" /> : undefined}
                  role="status"
                  aria-live="polite"
                  aria-atomic="true"
                  title={
                    connection === 'checking' ? 'Проверяем соединение…'
                      : connection === 'ready' ? 'Соединение установлено'
                        : 'Не удалось подключиться'
                  }
                  description={
                    connection === 'checking' ? 'Это займёт несколько секунд.'
                      : connection === 'ready' ? 'Сервер и база данных доступны.'
                        : 'Проверьте, что локальное окружение запущено, и повторите попытку.'
                  }
                />
                <Button type="primary" htmlType="button" onClick={retry} disabled={connection === 'checking'} block>
                  {connection === 'error' ? 'Повторить' : 'Проверить ещё раз'}
                </Button>
              </Flex>
            </Card>
          </section>
          <Typography.Text type="secondary">
            Проверка обращается к настоящему серверу. Сбор заявок пока не доступен.
          </Typography.Text>
        </Flex>
      </main>
      <footer className="page-footer">
        <Typography.Text type="secondary">Leadflow · Основа мини-CRM</Typography.Text>
      </footer>
    </Flex>
  )
}

export default App
