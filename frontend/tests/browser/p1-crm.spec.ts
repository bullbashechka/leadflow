import { expect, test } from '@playwright/test'
import { enter, incomingLead, mockAccess, noOverflow, screenshot } from './crmFixtures'

test('P1: edit conflicts require a field choice; a status update keeps an unsaved note', async ({ page, context }) => {
  const server = await mockAccess(context)
  const lead = incomingLead(31)
  lead.name = 'Исходное имя'
  server.leads.push(lead)
  await page.goto('/')
  await enter(page, 'demo')
  await page.getByRole('button', { name: 'Открыть карточку: Исходное имя' }).click()
  await page.getByRole('button', { name: 'Редактировать' }).click()
  await page.getByLabel('Имя', { exact: true }).fill('Мой вариант')
  await page.getByLabel('Заметка', { exact: true }).fill('Моя заметка')
  server.conflictNextUpdate = true
  await page.getByRole('button', { name: 'Сохранить изменения' }).click()

  const nameChoices = page.getByRole('radiogroup', { name: 'Сохранить поле «Имя»' })
  const noteChoices = page.getByRole('radiogroup', { name: 'Сохранить поле «Заметка»' })
  await expect(nameChoices).toBeVisible()
  await nameChoices.getByText('Сохранить мой вариант', { exact: true }).click()
  await noteChoices.getByText('Оставить текущее', { exact: true }).click()
  await page.getByRole('button', { name: 'Сверить и сохранить' }).click()
  await expect(page.getByRole('heading', { name: 'Мой вариант' })).toBeVisible()
  await expect(page.getByText('Актуальная заметка')).toBeVisible()

  const statusSelect = page.getByLabel('Новый статус')
  await statusSelect.click()
  await statusSelect.press('ArrowDown')
  await statusSelect.press('ArrowDown')
  await statusSelect.press('Enter')
  await page.getByRole('button', { name: 'Сохранить статус' }).click()
  await expect.poll(() => server.leads[0].status).toBe('closed')

  await page.getByRole('button', { name: 'Редактировать' }).click()
  const note = page.getByLabel('Заметка', { exact: true })
  await note.fill('Черновик после смены статуса')
  await statusSelect.click()
  await statusSelect.press('ArrowUp')
  await statusSelect.press('ArrowUp')
  await statusSelect.press('Enter')
  await page.getByRole('button', { name: 'Сохранить статус' }).click()
  await expect(note).toHaveValue('Черновик после смены статуса')
  await noOverflow(page)
})

test('P1: tag creation is immediate, assignment waits for lead save, and leaving warns about drafts', async ({ page, context }) => {
  const server = await mockAccess(context)
  const lead = incomingLead(32)
  lead.name = 'Заявка для тегов'
  server.leads.push(lead)
  await page.goto('/')
  await enter(page, 'demo')
  await page.getByRole('button', { name: 'Открыть карточку: Заявка для тегов' }).click()
  await page.getByRole('button', { name: 'Редактировать' }).click()
  await page.getByRole('button', { name: 'Создать тег' }).click()
  const tagDialog = page.getByRole('dialog', { name: 'Управление тегами' })
  await tagDialog.getByLabel('Название нового тега').fill('Учебный проект')
  await tagDialog.getByRole('button', { name: 'Создать тег' }).click()
  await expect(tagDialog).toBeHidden()
  await page.getByRole('button', { name: 'Сохранить изменения' }).click()
  await expect(page.getByRole('region', { name: 'Теги' }).getByText('Учебный проект', { exact: true })).toBeVisible()
  await expect(server.leads[0].tags).toEqual(expect.arrayContaining([
    expect.objectContaining({ name: 'Учебный проект' }),
  ]))

  await page.getByRole('button', { name: 'К списку заявок' }).click()
  const search = page.getByRole('searchbox', { name: 'Поиск заявок' })
  await search.fill('заявка для тегов')
  await search.press('Enter')
  const statusFilter = page.getByLabel('Статус')
  await statusFilter.click()
  await statusFilter.press('Enter')
  const tagFilter = page.locator('.crm-direction-filter')
  await tagFilter.click()
  await page.getByText('Учебный проект', { exact: true }).last().click()
  await expect(tagFilter).toContainText('Учебный проект')
  await expect(page.getByLabel('Всего заявок: 1')).toBeVisible()
  await page.getByRole('button', { name: 'Открыть карточку: Заявка для тегов' }).click()
  await page.getByRole('region', { name: 'Карточка заявки' }).getByRole('button', { name: 'Управление тегами' }).click()
  const manager = page.getByRole('dialog', { name: 'Управление тегами' })
  await manager.getByRole('button', { name: 'Удалить тег Учебный проект' }).click()
  await page.getByRole('button', { name: 'Удалить тег', exact: true }).last().click()
  await expect(manager.getByText(/Тег удалён/)).toBeVisible()
  await manager.getByRole('button', { name: 'Закрыть' }).click()
  await expect(manager).toBeHidden()
  await expect(page.getByRole('heading', { name: 'Заявка для тегов' })).toBeVisible()
  await expect(page.getByRole('region', { name: 'Теги' }).getByText('Учебный проект', { exact: true })).toHaveCount(0)
  await expect(page.getByRole('region', { name: 'Карточка заявки' }).getByText(/Фильтр по этому тегу сброшен/)).toBeVisible()
  expect(server.leads).toHaveLength(1)

  await page.getByRole('button', { name: 'К списку заявок' }).click()
  await expect(search).toHaveValue('заявка для тегов')
  await expect(page.locator('.crm-filter-controls').getByText('Новый', { exact: true })).toBeVisible()
  await expect(tagFilter).toContainText('Направление: все')
  await expect(page.getByLabel('Всего заявок: 1')).toBeVisible()
  await page.getByRole('button', { name: 'Открыть карточку: Заявка для тегов' }).click()
  await page.getByRole('button', { name: 'Редактировать' }).click()
  await page.getByLabel('Имя', { exact: true }).fill('Незавершённое имя')
  await page.getByRole('button', { name: 'К списку заявок' }).click()
  const leaveDialog = page.getByRole('dialog', { name: 'Выйти без сохранения?' })
  await expect(leaveDialog).toBeVisible()
  await leaveDialog.getByRole('button', { name: 'Остаться' }).click()
  await expect(page.getByLabel('Имя', { exact: true })).toHaveValue('Незавершённое имя')
  await noOverflow(page)
})

test('P1: search, tag and status filters combine; demo leads are marked and deletion is confirmed', async ({ page, context }) => {
  const server = await mockAccess(context)
  const website = { id: 1, name: 'Сайт', is_system: true }
  const advertising = { id: 2, name: 'Реклама', is_system: true }
  const matching = incomingLead(41, [website])
  matching.name = 'Анна Миронова'
  matching.request = 'Нужен сайт для кафе'
  matching.contacts = [{ type: 'phone', value: '+7 (701) 123-45-67' }]
  const otherStatus = incomingLead(42, [website])
  otherStatus.name = 'Борис'
  otherStatus.request = 'Анна спрашивает про кафе'
  otherStatus.status = 'closed'
  const otherTag = incomingLead(43, [advertising])
  otherTag.name = 'Анна'
  otherTag.request = 'Проект кафе'
  otherTag.status = 'new'
  otherTag.is_demo = true
  server.leads.push(matching, otherStatus, otherTag)
  await page.goto('/')
  await enter(page, 'demo')

  const search = page.getByRole('searchbox', { name: 'Поиск заявок' })
  await search.fill('анна кафе')
  await search.press('Enter')
  const tagFilter = page.getByLabel('Направление')
  await tagFilter.click()
  await tagFilter.press('Enter')
  const statusFilter = page.getByLabel('Статус')
  await statusFilter.click()
  await statusFilter.press('Enter')
  await expect(page.getByLabel('Всего заявок: 1')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Открыть карточку: Анна Миронова' })).toBeVisible()

  await search.fill('7011234567')
  await search.press('Enter')
  await expect(page.getByLabel('Всего заявок: 1')).toBeVisible()

  await page.getByRole('button', { name: 'Сбросить всё' }).click()
  await expect(page.locator(`[data-lead-id="${otherTag.id}"]`).getByText('Демо', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Открыть карточку: Анна', exact: true }).click()
  const detailRegion = page.getByRole('region', { name: 'Карточка заявки' })
  await expect(detailRegion.getByText('Демо', { exact: true })).toBeVisible()
  await detailRegion.getByRole('button', { name: 'Удалить заявку', exact: true }).click()
  const deleteDialog = page.getByRole('dialog', { name: 'Удалить заявку?' })
  await expect(deleteDialog).toContainText('Анна')
  await expect(deleteDialog).toContainText('нельзя отменить')
  await deleteDialog.getByRole('button', { name: 'Оставить заявку' }).click()
  await expect(server.leads).toHaveLength(3)
  await detailRegion.getByRole('button', { name: 'Удалить заявку', exact: true }).click()
  await page.getByRole('dialog', { name: 'Удалить заявку?' }).getByRole('button', { name: 'Удалить заявку' }).click()
  await expect.poll(() => server.leads.length).toBe(2)
  await noOverflow(page)
})

test('P1: edit form and its draft adapt across phone, tablet and desktop widths', async ({ page, context }) => {
  const server = await mockAccess(context)
  const lead = incomingLead(51)
  lead.name = 'Адаптивная заявка'
  server.leads.push(lead)
  await page.goto('/')
  await enter(page, 'demo')
  await page.getByRole('button', { name: 'Открыть карточку: Адаптивная заявка' }).click()
  await page.getByRole('button', { name: 'Редактировать' }).click()
  const note = page.getByLabel('Заметка', { exact: true })
  await note.fill('Черновик сохраняется при смене ширины')

  for (const width of [320, 375, 390, 767, 768, 1200, 1440]) {
    await page.setViewportSize({ width, height: width < 768 ? 840 : 1000 })
    await expect(note).toHaveValue('Черновик сохраняется при смене ширины')
    await note.focus()
    await expect(note).toBeFocused()
    await noOverflow(page)
    await screenshot(page, `p1-edit-${width}`)
  }

  await page.setViewportSize({ width: 320, height: 840 })
  const detailRegion = page.getByRole('region', { name: 'Карточка заявки' })
  const save = detailRegion.getByRole('button', { name: 'Сохранить изменения' })
  await save.scrollIntoViewIfNeeded()
  const saveBox = await save.boundingBox()
  const dockBox = await page.getByRole('navigation', { name: 'Управление CRM' }).boundingBox()
  expect(saveBox).not.toBeNull()
  expect(dockBox).not.toBeNull()
  expect(saveBox!.y + saveBox!.height).toBeLessThanOrEqual(dockBox!.y)
  await detailRegion.getByRole('button', { name: 'Управление тегами' }).click()
  await expect(page.getByRole('dialog', { name: 'Управление тегами' })).toBeVisible()
  await noOverflow(page)
  await screenshot(page, 'p1-tags-320')
})
