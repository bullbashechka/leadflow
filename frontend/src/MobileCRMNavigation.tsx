import { useEffect, useRef, useState } from 'react'
import { Button, Drawer, Flex, theme, Typography } from 'antd'
import { useCRMAccess } from './AuthBoundary'

function NavigationIcon({ kind }: { kind: 'list' | 'add' | 'menu' }) {
  return <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden="true">
    {kind === 'add' ? <path d="M12 4v16M4 12h16" /> : kind === 'menu' ? <path d="M4 6h16M4 12h16M4 18h16" />
      : <><path d="M8 6h12M8 12h12M8 18h12" /><path d="M4 6h.01M4 12h.01M4 18h.01" strokeWidth="3" /></>}
  </svg>
}

export function MobileCRMNavigation({ botUrl, onList, onCreate }: {
  botUrl: string | null
  onList: () => void
  onCreate: () => void
}) {
  const { controller, state } = useCRMAccess()
  const { token } = theme.useToken()
  const [menuOpen, setMenuOpen] = useState(false)
  const menuButton = useRef<HTMLButtonElement>(null)
  useEffect(() => {
    if (state.kind !== 'authenticated') setMenuOpen(false)
  }, [state.kind])

  const actionStyle = { height: 64, borderRadius: 24, padding: '8px 12px', fontSize: 13 }
  return <>
    <Flex component="nav" aria-label="Управление CRM" align="center" justify="space-between" className="crm-mobile-dock"
      style={{ background: token.colorBgContainer, borderRadius: 32, boxShadow: '0 8px 32px #171a2110' }}>
      <Button type="text" aria-current="page" onClick={onList} className="crm-dock-action"
        style={{ ...actionStyle, background: token.colorPrimaryBg, color: token.colorText }}>
        <Flex vertical align="center" gap={2}><NavigationIcon kind="list" /><span>Заявки</span></Flex>
      </Button>
      <Button type="text" aria-label="Добавить заявку" onClick={onCreate} className="crm-dock-action" style={actionStyle}>
        <Flex vertical align="center" gap={2}>
          <Flex align="center" justify="center" style={{ background: token.colorPrimary, borderRadius: 18, width: 52, height: 42 }}><NavigationIcon kind="add" /></Flex>
          <span>Добавить</span>
        </Flex>
      </Button>
      <Button type="text" ref={menuButton} onClick={() => setMenuOpen(true)} aria-haspopup="dialog" aria-expanded={menuOpen}
        className="crm-dock-action" style={{ ...actionStyle, color: token.colorTextSecondary }}>
        <Flex vertical align="center" gap={2}><NavigationIcon kind="menu" /><span>Меню</span></Flex>
      </Button>
    </Flex>
    {/* Keep the panel inside AuthBoundary so its content is hidden and inert when access locks. */}
    <Drawer title="Меню CRM" placement="bottom" size="auto" getContainer={false} rootStyle={{ position: 'fixed' }}
      open={menuOpen && state.kind === 'authenticated'} onClose={() => setMenuOpen(false)}
      focusable={{ trap: true, focusTriggerAfterClose: false }}
      afterOpenChange={open => { if (!open && controller.state.kind === 'authenticated') menuButton.current?.focus({ preventScroll: true }) }}
      styles={{ section: { borderRadius: '24px 24px 0 0' }, wrapper: { maxHeight: '80dvh' }, close: { width: 44, height: 44 }, body: { paddingBottom: 'max(24px, env(safe-area-inset-bottom))' } }}>
      <Flex vertical gap={12}>
        {botUrl && <Button size="large" block href={botUrl} target="_blank" rel="noreferrer" onClick={() => setMenuOpen(false)}>Открыть Telegram-бота ↗</Button>}
        <Button size="large" block onClick={() => { setMenuOpen(false); void controller.logOut() }}>Выйти</Button>
        <Typography.Text type="secondary">Leadflow · Заявки агентства</Typography.Text>
      </Flex>
    </Drawer>
  </>
}
