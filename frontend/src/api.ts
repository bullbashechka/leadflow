type RequestOptions = { signal?: AbortSignal; timeoutMs?: number }

export async function checkHealth({ signal, timeoutMs = 8000 }: RequestOptions = {}): Promise<void> {
  const timeout = new AbortController()
  const timer = setTimeout(() => timeout.abort(new DOMException('Request timed out', 'TimeoutError')), timeoutMs)
  const combined = signal ? AbortSignal.any([signal, timeout.signal]) : timeout.signal
  try {
    const response = await fetch('/api/health/', {
      signal: combined,
      credentials: 'same-origin',
      cache: 'no-store',
    })
    if (!response.ok) throw new Error('API unavailable')
    const data: unknown = await response.json()
    if (typeof data !== 'object' || data === null || !('status' in data) || data.status !== 'ok') {
      throw new Error('Unexpected API response')
    }
  } finally {
    clearTimeout(timer)
  }
}
