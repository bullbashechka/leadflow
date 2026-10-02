import { Alert, Button, Divider, Flex, Typography } from 'antd'
import { CRMWorkspace } from './CRMWorkspace'
import { AuthBoundary, useCRMAccess } from './AuthBoundary'
import './App.css'

function Workspace() {
  const { controller: access, state } = useCRMAccess()

  return <Flex vertical gap={24}>
    <Flex align="center" justify="space-between" gap="middle" wrap>
      <Typography.Title level={1} style={{ margin: 0 }}>CRM</Typography.Title>
      <Button onClick={() => void access.logOut()}>Выйти</Button>
    </Flex>
    {state.offline && <Alert type="warning" showIcon role="status" title="Нет связи с сервером"
      description="Доступ сохранён до окончания срока входа. Проверка связи повторяется автоматически." />}
    <CRMWorkspace />
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
      <main className="page-content page-content-crm">
        <AuthBoundary><Workspace /></AuthBoundary>
      </main>
      <footer className="page-footer">
        <Typography.Text type="secondary">Leadflow · Заявки агентства</Typography.Text>
      </footer>
    </Flex>
}

export default App
