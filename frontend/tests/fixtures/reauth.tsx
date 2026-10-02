// Test-only UI. Vite's production entry does not import this fixture.
import { StrictMode, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { Button, ConfigProvider, Flex, Input, Typography } from 'antd'
import ruRU from 'antd/locale/ru_RU'
import 'antd/dist/reset.css'
import { AuthBoundary, useCRMAccess } from '../../src/AuthBoundary'
import { requestApi } from '../../src/api'
import { theme } from '../../src/theme'
import '../../src/index.css'
import '../../src/App.css'

function TestForm() {
  const { controller, state } = useCRMAccess()
  const [text, setText] = useState('')
  const [operation] = useState(() => crypto.randomUUID())
  const [result, setResult] = useState('')
  const save = async () => {
    try {
      await controller.runWithAccess((token) => requestApi('/api/test-save/', {
        method: 'POST', headers: { 'X-CSRFToken': token, 'Content-Type': 'application/json' },
        body: JSON.stringify({ submission_id: operation, text }),
      }))
      setResult('Подтверждено')
    } catch {
      setResult('Результат неизвестен')
    }
  }
  return <Flex vertical gap="middle">
    <Typography.Title level={1}>Тестовая форма</Typography.Title>
    <Typography.Paragraph>Проверка доступа. Эта форма не создаёт лидов.</Typography.Paragraph>
    <label htmlFor="draft">Текст черновика</label>
    <Input.TextArea id="draft" value={text} onChange={(event) => setText(event.target.value)} />
    <Button onClick={() => void save()} disabled={state.offline}>Сохранить (тест)</Button>
    <Button onClick={() => void controller.logOut()}>Выйти</Button>
    <Typography.Text role="status">{result}</Typography.Text>
    <span aria-label="Идентификатор операции">{operation}</span>
  </Flex>
}

createRoot(document.getElementById('root')!).render(
  <StrictMode><ConfigProvider theme={theme} locale={ruRU}>
    <main className="page"><div className="page-content"><AuthBoundary><TestForm /></AuthBoundary></div></main>
  </ConfigProvider></StrictMode>,
)
