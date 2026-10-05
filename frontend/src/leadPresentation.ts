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
