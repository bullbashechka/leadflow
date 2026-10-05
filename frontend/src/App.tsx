import { Alert, Button, Flex, theme, Typography } from 'antd'
import { CRMWorkspace } from './CRMWorkspace'
import { AuthBoundary, useCRMAccess } from './AuthBoundary'
import './App.css'

function Workspace() {
  const { controller: access, state } = useCRMAccess()
  const { token } = theme.useToken()

  return <Flex vertical className="authenticated-workspace" style={{ border: `1px solid ${token.colorBorderSecondary}`, borderRadius: 28, boxShadow: '0 8px 32px #171a2105' }}>
    <header className="page-header" style={{ background: token.colorBgContainer, borderRadius: '28px 28px 0 0', borderBottom: `1px solid ${token.colorBorderSecondary}` }}>
      <Flex align="center" justify="space-between" gap="middle" wrap className="workspace-header">
        <Flex align="center" gap={32} className="workspace-header-start">
          <Typography.Link href="/" aria-label="Leadflow — главная" strong className="workspace-brand" style={{ color: token.colorText, fontSize: 24 }}>
            <svg width="24" height="32" viewBox="0 0 24 32" fill="none" aria-hidden="true"><path d="M3 16C3 8 10 2 21 2V17C21 24 14 30 3 30V16Z" fill={token.colorPrimary}/><path d="M3 16C3 11 7 8 12 8V24C9 27 6 29 3 30V16Z" fill="#85d943"/></svg>
            <span>Leadflow</span>
          </Typography.Link>
          <nav aria-label="Раздел CRM" className="workspace-navigation">
            <Typography.Text aria-current="page" className="workspace-active-section" style={{ background: token.colorText, color: token.colorBgContainer, borderRadius: 24 }}>Заявки</Typography.Text>
          </nav>
        </Flex>
        <Button className="workspace-logout" onClick={() => void access.logOut()}>Выйти</Button>
      </Flex>
    </header>
    <main className="page-content page-content-crm">
      <Flex vertical gap={24}>
        {state.offline && <Alert type="warning" showIcon role="status" title="Нет связи с сервером"
          description="Доступ сохранён до окончания срока входа. Проверка связи повторяется автоматически." />}
        <CRMWorkspace />
      </Flex>
    </main>
  </Flex>
}

function App() {
  const { token } = theme.useToken()
  return <Flex vertical className="page" style={{ background: token.colorBgLayout }}>
      <div className="page-access"><AuthBoundary><Workspace /></AuthBoundary></div>
      <footer className="page-footer">
        <Typography.Text type="secondary">Leadflow · Заявки агентства</Typography.Text>
      </footer>
    </Flex>
}

export default App
