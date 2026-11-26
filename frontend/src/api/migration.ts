import { request } from './client'

// 迁移核验接口的响应结构（后端字段保持中文/下划线并存，这里只约束用到的部分）。
export type MigrationReceipt = {
  report_key: string
  site: string
  amount: number
  window_label?: string
  window_start?: string
  write_side: string
  fence_version: number
  replay_count: number
  legacy_reader_received?: boolean
  window_reader_received?: boolean
  read_versions_received?: string[]
  legacy_seen_at?: string | null
  window_seen_at?: string | null
}

export type HourWindow = {
  window_start: string
  window_label: string
  delta: number
  report_count: number
  report_keys: string[]
}

export type ReaderReport = {
  report_key: string
  amount: number
  window_label?: string
  reader_received?: boolean
}

export type LegacyReader = {
  reader_version: string
  reader_label: string
  site: string
  site_label: string
  cumulative_total: number
  baseline_total: number
  received_reports: ReaderReport[]
  basis: { kind: string; label: string; amount: number; time_scoped: boolean }[]
  read_at: string
  legacy_row?: Record<string, string | number>
}

export type WindowReader = {
  reader_version: string
  reader_label: string
  site: string
  site_label: string
  baseline_total: number
  baseline: { label: string; amount: number; time_scoped: boolean; appears_in_hourly_windows: boolean }
  windows: HourWindow[]
  hourly_total: number
  window_total: number
  total: number
  received_reports: ReaderReport[]
  read_at: string
}

export type Reconciliation = {
  matched: boolean
  statement: string
  legacy_total: number
  baseline_total: number
  hourly_total: number
  window_total: number
  difference: number
  report_count: number
  fence_version: number
  phase: string
  message?: string
  created_at?: string
}

export type MigrationInfo = {
  phase: string
  phase_label: string
  fence_version: number
  write_side: string
  write_side_label: string
}

export type MigrationEvent = {
  at: string
  kind: string
  site: string
  message: string
}

export type MigrationState = {
  ok: boolean
  site: string
  site_label: string
  canonical_report_key: string
  canonical_window: {
    window_start: string
    window_label: string
    audit_time: string
    amount: number
  }
  migration: MigrationInfo
  legacy: LegacyReader
  windows: WindowReader
  reconciliation: Reconciliation | null
  receipts: MigrationReceipt[]
  events: MigrationEvent[]
}

export type ConflictBody = {
  ok: false
  code: 'migration_conflict'
  conflict: true
  message: string
  current_fence: number
  expected_fence: number
  retry: Record<string, unknown>
}

export type PublishResult = {
  ok: boolean
  duplicate?: boolean
  message: string
  receipt?: MigrationReceipt
  legacy?: LegacyReader
  windows?: WindowReader
}

async function readJson<T>(response: Response): Promise<T> {
  return (await response.json()) as T
}

export async function fetchState(site = 'K'): Promise<MigrationState> {
  const response = await request(`/api/metrics-migration/state?site=${encodeURIComponent(site)}`)
  return readJson<MigrationState>(response)
}

export async function fetchLegacy(site = 'K'): Promise<LegacyReader> {
  const response = await request(`/api/metrics-migration/legacy?site=${encodeURIComponent(site)}`)
  return readJson<LegacyReader>(response)
}

export async function fetchWindows(site = 'K'): Promise<WindowReader> {
  const response = await request(`/api/metrics-migration/windows?site=${encodeURIComponent(site)}`)
  return readJson<WindowReader>(response)
}

export async function publishBackfill(body: Record<string, unknown> = {}): Promise<{ status: number; data: PublishResult | ConflictBody }> {
  const response = await request('/api/metrics-migration/backfill', {
    method: 'POST',
    body: JSON.stringify({ site: 'K', ...body }),
  })
  const data = await response.json()
  return { status: response.status, data }
}

export async function conflictDrill(body: Record<string, unknown> = {}): Promise<{ status: number; data: ConflictBody }> {
  const response = await request('/api/metrics-migration/backfill/conflict', {
    method: 'POST',
    body: JSON.stringify({ site: 'K', ...body }),
  })
  const data = await response.json()
  return { status: response.status, data }
}

export async function confirmSwitch(expectedFence?: number): Promise<{ ok: boolean; message: string; reconciliation?: Reconciliation; migration?: MigrationInfo }> {
  const response = await request('/api/metrics-migration/switch/confirm', {
    method: 'POST',
    body: JSON.stringify(expectedFence === undefined ? {} : { expected_fence: expectedFence }),
  })
  return response.json()
}

export async function verifyReconciliation(): Promise<Reconciliation> {
  const response = await request('/api/metrics-migration/reconciliation/verify', {
    method: 'POST',
    body: JSON.stringify({ site: 'K' }),
  })
  return response.json()
}

export async function rollbackMigration(): Promise<{ ok: boolean; message: string; reconciliation?: Reconciliation; migration?: MigrationInfo }> {
  const response = await request('/api/metrics-migration/rollback', { method: 'POST', body: '{}' })
  return response.json()
}

export async function resetScenario(): Promise<MigrationState> {
  const response = await request('/api/metrics-migration/reset', {
    method: 'POST',
    body: JSON.stringify({ site: 'K' }),
  })
  return response.json()
}
