import type { Lead } from './api'

export function sourceLabel(source: Lead['source']) {
  return source === 'manual' ? 'Вручную' : 'Telegram-бот'
}

export function statusLabel(status: Lead['status']) {
  return ({ new: 'Новый', in_progress: 'В работе', closed: 'Закрыт' })[status]
}

export function timezoneLabel() {
  return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
}

export function formatExactDate(value: string, timeZone = timezoneLabel()) {
  return new Intl.DateTimeFormat('ru-RU', { dateStyle: 'short', timeStyle: 'short', timeZone }).format(new Date(value))
}

export function formatListDate(value: string, now = new Date(), timeZone = timezoneLabel()) {
  const calendarDay = (date: Date) => {
    const parts = new Intl.DateTimeFormat('en', { year: 'numeric', month: 'numeric', day: 'numeric', timeZone }).formatToParts(date)
    const part = (type: string) => Number(parts.find(item => item.type === type)?.value)
    return Date.UTC(part('year'), part('month') - 1, part('day'))
  }
  const date = new Date(value)
  const daysAgo = (calendarDay(now) - calendarDay(date)) / 86_400_000
  if (daysAgo === 0 || daysAgo === 1) {
    const time = new Intl.DateTimeFormat('ru-RU', { timeStyle: 'short', timeZone }).format(date)
    return `${daysAgo === 0 ? 'Сегодня' : 'Вчера'}, ${time}`
  }
  return formatExactDate(value, timeZone)
}

export function contactHref(type: Lead['contacts'][number]['type'], value: string) {
  const text = value.trim()
  if (type === 'phone') return `tel:${text.replace(/[ ()-]/g, '')}`
  if (type === 'email') return `mailto:${text.split('@').map(encodeURIComponent).join('@')}`
  let username = text.startsWith('@') ? text.slice(1) : ''
  if (!username) {
    try {
      const url = new URL(/^(t\.me|telegram\.me)\//.test(text) ? `https://${text}` : text)
      if (!['http:', 'https:'].includes(url.protocol) || !['t.me', 'telegram.me'].includes(url.host.toLowerCase())
        || url.username || url.password || text.includes('?') || text.includes('#')) return undefined
      username = url.pathname.replace(/^\//, '').replace(/\/$/, '')
    } catch { return undefined }
  }
  return /^[A-Za-z][A-Za-z0-9_]{4,31}$/.test(username) ? `https://t.me/${encodeURIComponent(username)}` : undefined
}
