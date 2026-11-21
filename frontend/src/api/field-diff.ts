import { request } from './client'

/** 字段变更对照视图用到的返回结构；字段名与后端 field_diff 服务保持一致。 */

export type ScanField = {
  field_id: string
  name: string
  physical_column: string
  data_type?: string
  ordinal?: number
}

export type ScanBatch = {
  id: number
  scan_batch_id: string
  source_id: number
  source_no: string
  source_type: string
  batch_no: string
  version: number
  fields: ScanField[]
  field_count: number
  input_digest: string
  scanned_by: string
  scanned_at: string
  status: string
}

export type DiffEntry = {
  id: number
  diff_batch_id: number
  diff_batch_no: string
  source_id: number
  source_no: string
  field_id: string
  field_name: string
  physical_column: string
  baseline_column: string
  alias: string
  change_type: string
  blocking: boolean
  blocking_measures: string[]
  resolution: string
  resolution_note: string
  reviewer: string
  resolved_at: string
  校验结论?: string
  关联校验编号?: string
}

export type DiffBatch = {
  id: number
  batch_no: string
  model_id: number
  model_no: string
  model_name: string
  source_ids: number[]
  baseline_scan_ids: number[]
  target_scan_ids: number[]
  alias_version: number
  input_digest: string
  input_snapshot: Record<string, unknown>
  status: string
  pending_count: number
  confirmed_count: number
  confirmed_at: string
  confirmed_by: string
  last_validation_no: string
  last_validation_result: string
  created_at: string
  entries?: DiffEntry[]
  pending_entries?: DiffEntry[]
  confirmed_entries?: DiffEntry[]
}

export type MeasureStatus = {
  measure: string
  status: string
  blocked_by: { source_id: number; field_id: string; alias: string; physical_column: string }[]
}

export type AliasEntry = {
  alias_version: number
  source_id: number
  field_id: string
  alias: string
  physical_column: string
  status: string
  confirmed_by: string
  note: string
}

export type Promotion = {
  id: number
  promotion_no: string
  model_id: number
  model_no: string
  status: string
  result: string
  conclusion: string
  requested_by: string
  started_at: string
  concluded_at: string
  diff_batch_id: number
  diff_batch_no: string
  alias_version: number
  validation_no: string
  missing_fields: AliasEntry[]
  measures: MeasureStatus[]
  pinned_measures?: MeasureStatus[]
  pinned?: {
    diff_batch_id: number
    target_scan_ids: number[]
    baseline_scan_ids: number[]
    alias_version: number
    aliases: AliasEntry[]
    target_scans: Record<string, ScanBatch>
    entries: DiffEntry[]
  }
  scans?: ScanBatch[]
  baseline_scans?: ScanBatch[]
  alias_snapshot?: AliasEntry[]
  current_alias_version?: number
  current_aliases?: AliasEntry[]
  alias_changed_since_validation?: boolean
  current_measures?: MeasureStatus[]
  current_missing_fields?: AliasEntry[]
  can_repromote?: boolean
  previous_confirmed_missing?: Record<string, DiffEntry[]>
  summary?: string
}

export type HistoryEntry = DiffEntry & {
  batch: {
    id: number
    batch_no: string
    model_id: number
    input_digest: string
    baseline_scan_ids: number[]
    target_scan_ids: number[]
    alias_version: number
    confirmed_at: string
    confirmed_by: string
  }
}

export type PromotionList = { ok: boolean; items: Promotion[]; total: number }
export type ModelOverview = {
  ok: boolean
  model: Record<string, unknown>
  source_ids: number[]
  scans: ScanBatch[]
  batches: DiffBatch[]
  alias_version: number
  aliases: AliasEntry[]
  latest_promotion: Promotion | null
}
export type BatchList = { ok: boolean; items: DiffBatch[]; total: number }
export type ScanList = { ok: boolean; items: ScanBatch[]; total: number }
export type EntryList = { ok: boolean; items: DiffEntry[]; total: number }

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await request(path, init)
  const body = (await response.json()) as T & { detail?: unknown }
  if (!response.ok) {
    const detail = body?.detail
    if (typeof detail === 'string') throw new Error(detail)
    if (detail && typeof detail === 'object' && 'message' in detail) {
      throw new Error(String((detail as { message: unknown }).message))
    }
    throw new Error(`字段变更对照请求失败（${response.status}）`)
  }
  return body
}

export function listScans(sourceId?: number) {
  const query = sourceId ? `?source_id=${sourceId}` : ''
  return call<ScanList>(`/api/field-diff/scans${query}`)
}

export function listBatches(modelId: number) {
  return call<BatchList>(`/api/field-diff/diffs?model_id=${modelId}`)
}

export function getModelOverview(modelId: number) {
  return call<ModelOverview>(`/api/field-diff/models/${modelId}/overview`)
}

export function createBatch(modelId: number, sourceId?: number) {
  return call<{ ok: boolean; batch: DiffBatch; entries: DiffEntry[] }>('/api/field-diff/diffs', {
    method: 'POST',
    body: JSON.stringify({ model_id: modelId, source_id: sourceId ?? null }),
  })
}

export function getBatch(batchId: number) {
  return call<DiffBatch>(`/api/field-diff/diffs/${batchId}`)
}

export function confirmEntry(
  entryId: number,
  resolution: string,
  options: { note?: string; alias?: string; physical_column?: string; reviewer?: string } = {},
) {
  return call<{ ok: boolean; entry: DiffEntry; batch: DiffBatch; alias_version: number | null }>(
    `/api/field-diff/entries/${entryId}/confirm`,
    { method: 'POST', body: JSON.stringify({ resolution, reviewer: 'reviewer', ...options }) },
  )
}

export function confirmBatch(batchId: number) {
  return call<{ ok: boolean; batch: DiffBatch }>(`/api/field-diff/diffs/${batchId}/confirm`, {
    method: 'POST',
    body: JSON.stringify({ reviewer: 'reviewer' }),
  })
}

export function startPromotion(modelId: number) {
  return call<{ ok: boolean; promotion: Promotion; validation: Record<string, unknown> }>(
    `/api/field-diff/models/${modelId}/promotions`,
    { method: 'POST', body: JSON.stringify({ requested_by: 'model_owner' }) },
  )
}

export function finishPromotion(promotionId: number) {
  return call<{ ok: boolean; message: string; promotion: Promotion }>(
    `/api/field-diff/promotions/${promotionId}/finish`,
    { method: 'POST', body: JSON.stringify({ concluded_by: 'model_owner' }) },
  )
}

export function getPromotion(promotionId: number) {
  return call<Promotion>(`/api/field-diff/promotions/${promotionId}`)
}

export function listPromotions(modelId: number) {
  return call<PromotionList>(`/api/field-diff/models/${modelId}/promotions`)
}

export function listAliasVersions(modelId: number) {
  return call<{ ok: boolean; current_version: number; versions: { alias_version: number; entries: AliasEntry[] }[] }>(
    `/api/field-diff/models/${modelId}/aliases`,
  )
}

export function lookupConfirmedMissing(fieldId: string, modelId: number) {
  return call<{ ok: boolean; items: HistoryEntry[]; total: number }>(
    `/api/field-diff/confirmed-missing?field_id=${encodeURIComponent(fieldId)}&model_id=${modelId}`,
  )
}
