import { Flex, theme, Typography } from 'antd'

export function WorkspaceBrand() {
  const { token } = theme.useToken()
  return <Flex align="center" gap={10} className="workspace-brand">
    <svg width="24" height="32" viewBox="0 0 24 32" fill="none" aria-hidden="true"><path d="M3 16C3 8 10 2 21 2V17C21 24 14 30 3 30V16Z" fill={token.colorPrimary}/><path d="M3 16C3 11 7 8 12 8V24C9 27 6 29 3 30V16Z" fill="#85d943"/></svg>
    <Typography.Text strong style={{ fontSize: 24 }}>Leadflow</Typography.Text>
  </Flex>
}
