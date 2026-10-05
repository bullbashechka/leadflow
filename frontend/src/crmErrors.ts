import { ApiError } from './api'
import { AccessInterruptedError } from './auth'

export function getFailureMessage(error: unknown) {
  if (error instanceof ApiError) return error.message
  if (error instanceof AccessInterruptedError) return 'Повторите действие после входа в CRM.'
  return 'Не удалось связаться с сервером. Проверьте соединение и повторите.'
}
