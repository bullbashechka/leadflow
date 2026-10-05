import assert from 'node:assert/strict'
import { test } from 'node:test'
import { formatListDate } from '../src/leadPresentation.ts'

test('list dates use the device calendar day across UTC and year boundaries', () => {
  const now = new Date('2026-01-01T02:00:00Z')
  assert.equal(formatListDate('2025-12-31T23:30:00Z', now, 'Asia/Almaty'), 'Сегодня, 04:30')
  assert.equal(formatListDate('2025-12-30T23:30:00Z', now, 'Asia/Almaty'), 'Вчера, 04:30')
  assert.equal(formatListDate('2025-12-29T23:30:00Z', now, 'Asia/Almaty'), '30.12.2025, 04:30')
})

test('yesterday follows local calendar days across daylight saving rather than 24 hours', () => {
  const now = new Date('2026-03-09T04:30:00Z')
  assert.equal(formatListDate('2026-03-08T05:15:00Z', now, 'America/New_York'), 'Вчера, 00:15')
  assert.equal(formatListDate('2026-03-09T04:00:00Z', now, 'America/New_York'), 'Сегодня, 00:00')
})
