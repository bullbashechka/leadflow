import { useEffect, useState } from 'react'
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
    <div className="page">
      <header className="header">
        <a className="brand" href="/" aria-label="Leadflow — главная">Leadflow<span>.</span></a>
        <span className="stage">Локальная разработка</span>
      </header>
      <main>
        <p className="eyebrow">Этап 0</p>
        <h1>Основа проекта</h1>
        <p className="intro">Готовим CRM для заявок агентства. Список заявок и формы появятся на следующих этапах.</p>
        <section className="connection-card" aria-labelledby="connection-title">
          <h2 id="connection-title">Соединение с сервером</h2>
          <div className={`connection-status ${connection}`} role="status" aria-live="polite" aria-atomic="true">
            <span className="indicator" aria-hidden="true" />
            <div>
              <p className="status-title">
                {connection === 'checking' && 'Проверяем соединение…'}
                {connection === 'ready' && 'Соединение установлено'}
                {connection === 'error' && 'Не удалось подключиться'}
              </p>
              <p className="status-description">
                {connection === 'checking' && 'Это займёт несколько секунд.'}
                {connection === 'ready' && 'Сервер и база данных доступны.'}
                {connection === 'error' && 'Проверьте, что локальное окружение запущено, и повторите попытку.'}
              </p>
            </div>
          </div>
          <button type="button" onClick={retry} disabled={connection === 'checking'}>
            {connection === 'error' ? 'Повторить' : 'Проверить ещё раз'}
          </button>
        </section>
        <p className="footnote">Проверка обращается к настоящему серверу. Сбор заявок пока не доступен.</p>
      </main>
      <footer>Leadflow · Основа мини-CRM</footer>
    </div>
  )
}

export default App
