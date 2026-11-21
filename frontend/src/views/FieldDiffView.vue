<template>
  <section class="page field-diff-page" data-testid="field-diff-view">
    <header class="page-head">
      <div>
        <h2>字段变更对照</h2>
        <p class="page-desc">
          按版本核对来源字段快照与语义模型字段别名：区分真正缺列与仍指向同一物理列的改名，
          一条待确认差异不会混入已确认批次。
        </p>
      </div>
      <div class="page-actions">
        <label class="inline-field">
          <span>语义模型</span>
          <select v-model.number="modelId" data-testid="diff-model" @change="reloadBatches">
            <option v-for="model in models" :key="model.id" :value="model.id">
              {{ model.模型编号 }} · {{ model.模型名称 }}
            </option>
          </select>
        </label>
        <button class="btn" data-testid="diff-refresh" type="button" @click="refreshAll">刷新对照</button>
        <button class="btn primary" data-testid="diff-create" type="button" :disabled="loading" @click="createBatchNow">
          生成对照批次
        </button>
      </div>
    </header>

    <p v-if="errorMessage" class="error-text" data-testid="diff-error">{{ errorMessage }}</p>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">对照批次</span>
        <strong class="stat-value" data-testid="stat-batches">{{ batches.length }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">待确认差异</span>
        <strong class="stat-value" data-testid="stat-pending">{{ activeBatch?.pending_count ?? 0 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">已确认差异</span>
        <strong class="stat-value" data-testid="stat-confirmed">{{ activeBatch?.confirmed_count ?? 0 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">当前别名版本</span>
        <strong class="stat-value" data-testid="stat-alias">v{{ aliasVersion }}</strong>
      </article>
    </div>

    <!-- 批次 -->
    <section class="diff-block">
      <header class="block-head">
        <h3>版本化对照批次</h3>
        <span class="block-hint">每批记录当次基准快照、目标快照和别名版本，输入摘要可用于还原</span>
      </header>
      <table class="data-table">
        <thead>
          <tr>
            <th>批次编号</th>
            <th>基准快照</th>
            <th>目标快照</th>
            <th>别名版本</th>
            <th>状态</th>
            <th>待确认</th>
            <th>输入摘要</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="batch in batches"
            :key="batch.id"
            :data-testid="`diff-batch-${batch.id}`"
            :class="{ selected: batch.id === activeBatchId }"
            @click="selectBatch(batch.id)"
          >
            <td>{{ batch.batch_no }}</td>
            <td>{{ scanLabel(batch.baseline_scan_ids) }}</td>
            <td>{{ scanLabel(batch.target_scan_ids) }}</td>
            <td>v{{ batch.alias_version }}</td>
            <td>
              <span :class="['tag', batch.status === '已确认' ? 'tag-ok' : 'tag-pending']">{{ batch.status }}</span>
            </td>
            <td :data-testid="`diff-batch-pending-${batch.id}`">{{ batch.pending_count }}</td>
            <td class="mono">{{ batch.input_digest }}</td>
          </tr>
          <tr v-if="!batches.length">
            <td colspan="7" class="empty-state">还没有对照批次，点击「生成对照批次」按当前快照对照</td>
          </tr>
        </tbody>
      </table>
    </section>

    <template v-if="activeBatch">
      <!-- 差异明细 -->
      <section class="diff-block">
        <header class="block-head">
          <h3>差异明细 · {{ activeBatch.batch_no }}</h3>
          <div class="block-actions">
            <span class="block-hint">
              基准 {{ scanLabel(activeBatch.baseline_scan_ids) }} → 目标
              {{ scanLabel(activeBatch.target_scan_ids) }}，别名 v{{ activeBatch.alias_version }}
            </span>
            <button
              class="btn"
              data-testid="diff-confirm-batch"
              type="button"
              :disabled="activeBatch.pending_count > 0 || activeBatch.status === '已确认'"
              @click="confirmBatchNow"
            >
              确认整批
            </button>
          </div>
        </header>

        <p class="section-hint" data-testid="diff-pending-hint">
          待确认 {{ pendingEntries.length }} 条（不并入已确认批次）
          <template v-if="activeBatch.status === '已确认'">
            · 本批已于 {{ activeBatch.confirmed_at }} 由 {{ activeBatch.confirmed_by }} 确认，历史口径按原样保留
          </template>
        </p>
        <table class="data-table">
          <thead>
            <tr>
              <th>差异类型</th>
              <th>来源</th>
              <th>字段标识</th>
              <th>基准列</th>
              <th>目标列</th>
              <th>别名</th>
              <th>影响度量</th>
              <th>处理结论</th>
              <th>复核操作</th>
            </tr>
          </thead>
          <tbody>
            <template v-for="group in entryGroups" :key="group.key">
              <tr v-if="group.key === 'pending'" class="group-row" data-testid="diff-group-pending">
                <td colspan="9">待确认差异 · {{ group.entries.length }} 条</td>
              </tr>
              <tr v-if="group.key === 'confirmed'" class="group-row" data-testid="diff-group-confirmed">
                <td colspan="9">已确认差异 · {{ group.entries.length }} 条</td>
              </tr>
              <tr
                v-for="entry in group.entries"
                :key="entry.id"
                :data-testid="`diff-entry-${entry.id}`"
                :data-change="entry.change_type"
                :data-resolution="entry.resolution"
              >
                <td>
                  <span :class="['tag', changeClass(entry.change_type)]">{{ entry.change_type }}</span>
                  <small v-if="entry.blocking" class="blocking-note">阻断</small>
                </td>
                <td>{{ entry.source_no || entry.source_id }}</td>
                <td class="mono" :data-testid="`diff-entry-field-${entry.id}`">{{ entry.field_id }}</td>
                <td>{{ entry.baseline_column || '—' }}</td>
                <td>{{ entry.physical_column || '—' }}</td>
                <td>{{ entry.alias || '—' }}</td>
                <td>{{ entry.blocking_measures.join('、') || '—' }}</td>
                <td :data-testid="`diff-entry-resolution-${entry.id}`">
                  <span :class="['tag', resolutionClass(entry.resolution)]">{{ entry.resolution }}</span>
                  <small v-if="entry.resolution_note" class="resolution-note">{{ entry.resolution_note }}</small>
                  <small v-if="entry.校验结论" class="resolution-note">校验：{{ entry.校验结论 }}</small>
                </td>
                <td class="row-actions">
                  <template v-if="entry.resolution === '待确认'">
                    <button
                      v-if="entry.change_type === '缺列' || entry.change_type === '删除'"
                      class="link"
                      type="button"
                      :data-testid="`diff-resolve-missing-${entry.id}`"
                      @click="resolve(entry, '确认缺列')"
                    >确认缺列</button>
                    <button
                      v-if="entry.change_type === '新增'"
                      class="link"
                      type="button"
                      :data-testid="`diff-resolve-new-${entry.id}`"
                      @click="resolve(entry, '映射新增字段')"
                    >映射新增字段</button>
                    <button
                      v-if="entry.change_type === '改名'"
                      class="link"
                      type="button"
                      :data-testid="`diff-resolve-rename-${entry.id}`"
                      @click="resolve(entry, '确认改名')"
                    >确认改名</button>
                    <button
                      class="link"
                      type="button"
                      :data-testid="`diff-resolve-alias-${entry.id}`"
                      @click="openAliasFix(entry)"
                    >更正别名</button>
                    <button
                      class="link"
                      type="button"
                      :data-testid="`diff-resolve-ignore-${entry.id}`"
                      @click="resolve(entry, '忽略')"
                    >忽略</button>
                  </template>
                  <button
                    v-else-if="entry.change_type === '缺列' || entry.change_type === '删除'"
                    class="link"
                    type="button"
                    :data-testid="`diff-history-${entry.id}`"
                    @click="loadHistory(entry)"
                  >查历史缺列</button>
                  <span v-else class="muted">—</span>
                </td>
              </tr>
            </template>
            <tr v-if="!entryGroups.length">
              <td colspan="9" class="empty-state">该批次没有字段差异</td>
            </tr>
          </tbody>
        </table>
      </section>

      <!-- 快照 -->
      <section class="diff-block">
        <header class="block-head">
          <h3>来源字段快照</h3>
          <span class="block-hint">快照内容不可变，新扫描只会生成新版本</span>
        </header>
        <div class="snapshot-grid">
          <article v-for="scan in snapshots" :key="scan.id" class="snapshot-card" :data-testid="`diff-scan-${scan.id}`">
            <header>
              <strong>{{ scan.scan_batch_id }}</strong>
              <span :class="['tag', scan.status === '生效' ? 'tag-ok' : 'tag-muted']">{{ scan.status }}</span>
            </header>
            <p class="muted">
              {{ scan.source_no || scan.source_id }} · {{ scan.scanned_at }} · {{ scan.field_count }} 列
            </p>
            <table class="mini-table">
              <thead><tr><th>字段标识</th><th>物理列</th></tr></thead>
              <tbody>
                <tr v-for="field in scan.fields" :key="`${scan.id}-${field.field_id}`">
                  <td class="mono">{{ field.field_id }}</td>
                  <td>{{ field.physical_column }}</td>
                </tr>
              </tbody>
            </table>
          </article>
          <p v-if="!snapshots.length" class="empty-state">该批次没有关联快照</p>
        </div>
      </section>
    </template>

    <!-- 投产校验 -->
    <section class="diff-block">
      <header class="block-head">
        <h3>语义模型投产校验</h3>
        <div class="block-actions">
          <span class="block-hint">先读取差异明细并冻结输入，再写结论；结论会回写到差异清单</span>
          <button class="btn primary" data-testid="promo-start" type="button" @click="startPromotionNow">
            按本版发起投产校验
          </button>
        </div>
      </header>

      <div class="promo-grid">
        <article class="promo-card">
          <h4>本次投产</h4>
          <p v-if="!promotion" class="muted">尚未发起投产校验</p>
          <dl v-else>
            <dt>投产编号</dt><dd data-testid="promo-no">{{ promotion.promotion_no }}</dd>
            <dt>状态</dt><dd data-testid="promo-status">{{ promotion.status }} {{ promotion.result }}</dd>
            <dt>差异批次</dt><dd>{{ promotion.diff_batch_no || '—' }}</dd>
            <dt>本次采用别名</dt><dd>v{{ promotion.alias_version }}</dd>
            <dt>结论</dt><dd data-testid="promo-conclusion">{{ promotion.conclusion || '尚未写结论' }}</dd>
          </dl>
          <div class="page-actions">
            <button
              class="btn"
              data-testid="promo-finish"
              type="button"
              :disabled="!promotion || promotion.status === '已完成'"
              @click="finishPromotionNow"
            >写入校验结论</button>
            <button
              class="btn ghost"
              data-testid="promo-requery"
              type="button"
              :disabled="!promotion"
              @click="requeryPromotion"
            >重新查询本次投产</button>
          </div>
        </article>

        <article class="promo-card">
          <h4>度量能否随本版投产</h4>
          <table class="mini-table">
            <thead><tr><th>度量</th><th>当次冻结</th><th>当前别名</th></tr></thead>
            <tbody>
              <tr v-for="item in measureRows" :key="item.measure">
                <td>{{ item.measure }}</td>
                <td :data-testid="`promo-measure-pinned-${item.measure}`">
                  <span :class="['tag', measureClass(item.pinned)]">{{ item.pinned || '—' }}</span>
                </td>
                <td :data-testid="`promo-measure-current-${item.measure}`">
                  <span :class="['tag', measureClass(item.current)]">{{ item.current || '—' }}</span>
                </td>
              </tr>
              <tr v-if="!measureRows.length">
                <td colspan="3" class="empty-state">暂无度量</td>
              </tr>
            </tbody>
          </table>
          <p v-if="promotion?.alias_changed_since_validation" class="alias-warning" data-testid="promo-alias-changed">
            复核人已在本次校验读取差异之后更正别名（v{{ promotion.alias_version }} →
            v{{ promotion.current_alias_version }}）。当次结论保持不变，重新查询可见当前别名下的结果。
          </p>
          <p v-if="promotion?.summary" class="promo-summary" data-testid="promo-summary">{{ promotion.summary }}</p>
        </article>

        <article class="promo-card">
          <h4>从本次投产回到当次输入</h4>
          <dl>
            <dt>实际采用扫描</dt>
            <dd data-testid="promo-scans">
              <span v-for="scan in promotion?.scans || []" :key="scan.id" class="chip">
                {{ scan.scan_batch_id }}（{{ scan.batch_no }}）
              </span>
              <span v-if="!(promotion?.scans || []).length" class="muted">—</span>
            </dd>
            <dt>采用的别名映射</dt>
            <dd data-testid="promo-aliases">
              <span v-for="alias in promotion?.alias_snapshot || []" :key="`${alias.source_id}-${alias.field_id}`" class="chip">
                {{ alias.alias }} → {{ alias.field_id }}
              </span>
              <span v-if="!(promotion?.alias_snapshot || []).length" class="muted">—</span>
            </dd>
            <dt>输入摘要</dt>
            <dd class="mono">{{ promotion?.pinned?.diff_batch_id ? `batch#${promotion.pinned.diff_batch_id}` : '—' }}</dd>
          </dl>
        </article>
      </div>

      <div v-if="history.length" class="history-panel" data-testid="diff-history-panel">
        <header class="block-head">
          <h4>按字段标识查回的上一轮已确认缺列</h4>
          <button class="btn ghost" type="button" @click="history = []">收起</button>
        </header>
        <table class="mini-table">
          <thead><tr><th>字段标识</th><th>批次</th><th>结论</th><th>复核人</th><th>确认时间</th><th>输入摘要</th></tr></thead>
          <tbody>
            <tr v-for="item in history" :key="`${item.id}-${item.batch.batch_no}`">
              <td class="mono">{{ item.field_id }}</td>
              <td>{{ item.batch.batch_no }}</td>
              <td>{{ item.resolution }}</td>
              <td>{{ item.reviewer }}</td>
              <td>{{ item.resolved_at }}</td>
              <td class="mono">{{ item.batch.input_digest }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <!-- 更正别名 -->
    <div v-if="aliasFix" class="modal-backdrop" data-testid="alias-fix-modal">
      <form class="modal" @submit.prevent="submitAliasFix">
        <h3>更正别名映射</h3>
        <p class="muted">
          字段标识 {{ aliasFix.entry.field_id }}（{{ aliasFix.entry.change_type }}）更正后会追加新的别名版本，
          已生成的校验结论不会被改写。
        </p>
        <label class="create-field">
          <span>别名</span>
          <input v-model="aliasFix.alias" data-testid="alias-fix-alias" required />
        </label>
        <label class="create-field">
          <span>指向物理列</span>
          <input v-model="aliasFix.physical" data-testid="alias-fix-physical" required />
        </label>
        <label class="create-field">
          <span>更正说明</span>
          <input v-model="aliasFix.note" data-testid="alias-fix-note" />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="aliasFix = null">取消</button>
          <button class="btn primary" data-testid="alias-fix-submit" type="submit">提交更正</button>
        </div>
      </form>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { listEntries } from '@/api/backend-service'
import {
  confirmBatch,
  confirmEntry,
  createBatch,
  finishPromotion,
  getBatch,
  getModelOverview,
  getPromotion,
  listPromotions,
  listScans,
  lookupConfirmedMissing,
  startPromotion,
} from '@/api/field-diff'
import type { DiffBatch, DiffEntry, HistoryEntry, Promotion, ScanBatch } from '@/api/field-diff'

const models = ref<{ id: number; 模型编号: string; 模型名称: string }[]>([])
const modelId = ref<number>(1)
const batches = ref<DiffBatch[]>([])
const activeBatchId = ref<number | null>(null)
const activeBatch = ref<DiffBatch | null>(null)
const snapshots = ref<ScanBatch[]>([])
const promotion = ref<Promotion | null>(null)
const aliasVersion = ref(0)
const history = ref<HistoryEntry[]>([])
const aliasFix = ref<{ entry: DiffEntry; alias: string; physical: string; note: string } | null>(null)
const loading = ref(false)
const errorMessage = ref('')

const pendingEntries = computed(
  () => activeBatch.value?.entries?.filter((entry) => entry.resolution === '待确认') ?? [],
)
const confirmedEntries = computed(
  () => activeBatch.value?.entries?.filter((entry) => entry.resolution !== '待确认') ?? [],
)
// 待确认与已确认分区展示：一条待确认差异不会混进已确认批次的呈现里。
const entryGroups = computed(() => {
  const groups = [] as { key: string; entries: DiffEntry[] }[]
  if (pendingEntries.value.length) groups.push({ key: 'pending', entries: pendingEntries.value })
  if (confirmedEntries.value.length) groups.push({ key: 'confirmed', entries: confirmedEntries.value })
  return groups
})

const measureRows = computed(() => {  const pinned = promotion.value?.pinned_measures ?? promotion.value?.measures ?? []
  const current = promotion.value?.current_measures ?? []
  const measures = Array.from(new Set([...pinned.map((m) => m.measure), ...current.map((m) => m.measure)]))
  return measures.map((measure) => ({
    measure,
    pinned: pinned.find((m) => m.measure === measure)?.status ?? '',
    current: current.find((m) => m.measure === measure)?.status ?? '',
  }))
})

function changeClass(type: string) {
  if (type === '新增') return 'tag-ok'
  if (type === '缺列' || type === '删除') return 'tag-danger'
  if (type === '改名') return 'tag-info'
  return 'tag-muted'
}

function resolutionClass(resolution: string) {
  if (resolution === '待确认') return 'tag-pending'
  if (resolution === '确认缺列') return 'tag-danger'
  return 'tag-ok'
}

function measureClass(status?: string) {
  if (status === '可投产') return 'tag-ok'
  if (status === '阻断') return 'tag-danger'
  return 'tag-muted'
}

function scanLabel(ids?: number[]) {
  if (!ids || !ids.length) return '—'
  return ids
    .map((id) => snapshots.value.find((scan) => scan.id === id)?.scan_batch_id ?? `#${id}`)
    .join('、')
}

async function loadModels() {
  try {
    const payload = await listEntries('semantic_model')
    models.value = payload.items as unknown as typeof models.value
    if (models.value.length && !models.value.some((m) => m.id === modelId.value)) {
      modelId.value = models.value[0].id
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '语义模型读取失败'
  }
}

async function reloadBatches() {
  errorMessage.value = ''
  try {
    // 一次取回模型的当前批次、当前别名版本和最近投产，减少来回请求。
    const overview = await getModelOverview(modelId.value)
    batches.value = overview.batches
    aliasVersion.value = overview.alias_version
    if (!batches.value.some((batch) => batch.id === activeBatchId.value)) {
      activeBatchId.value = batches.value[0]?.id ?? null
    }
    await loadActiveBatch()
    await loadPromotion()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '对照批次读取失败'
  }
}
async function loadActiveBatch() {
  if (!activeBatchId.value) {
    activeBatch.value = null
    snapshots.value = []
    return
  }
  const batch = await getBatch(activeBatchId.value)
  activeBatch.value = batch
  await loadSnapshots(batch)
}

async function loadSnapshots(batch: DiffBatch) {
  const ids = new Set([...(batch.baseline_scan_ids || []), ...(batch.target_scan_ids || [])])
  const payload = await listScans()
  snapshots.value = payload.items.filter((scan) => ids.has(scan.id))
}

async function selectBatch(batchId: number) {
  activeBatchId.value = batchId
  try {
    await loadActiveBatch()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '对照批次读取失败'
  }
}

async function createBatchNow() {
  loading.value = true
  errorMessage.value = ''
  try {
    const payload = await createBatch(modelId.value)
    activeBatchId.value = payload.batch.id
    await reloadBatches()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '生成对照批次失败'
  } finally {
    loading.value = false
  }
}

async function resolve(entry: DiffEntry, resolution: string) {
  errorMessage.value = ''
  try {
    await confirmEntry(entry.id, resolution, { note: `复核结论：${resolution}` })
    await reloadBatches()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '差异确认失败'
  }
}

function openAliasFix(entry: DiffEntry) {
  aliasFix.value = {
    entry,
    alias: entry.alias || entry.physical_column || entry.field_id,
    physical: entry.physical_column || '',
    note: '复核更正别名映射',
  }
}

async function submitAliasFix() {
  if (!aliasFix.value) return
  errorMessage.value = ''
  try {
    await confirmEntry(aliasFix.value.entry.id, '已更正别名', {
      alias: aliasFix.value.alias,
      physical_column: aliasFix.value.physical,
      note: aliasFix.value.note,
    })
    aliasFix.value = null
    await reloadBatches()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '更正别名失败'
  }
}

async function confirmBatchNow() {
  if (!activeBatch.value) return
  errorMessage.value = ''
  try {
    await confirmBatch(activeBatch.value.id)
    await reloadBatches()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '整批确认失败'
  }
}

async function loadPromotion() {
  promotion.value = null
  try {
    const payload = await listPromotions(modelId.value)
    if (payload.items.length) {
      promotion.value = await getPromotion(payload.items[0].id)
    }
  } catch {
    promotion.value = null
  }
}

async function startPromotionNow() {
  errorMessage.value = ''
  try {
    const payload = await startPromotion(modelId.value)
    promotion.value = await getPromotion(payload.promotion.id)
    await reloadBatches()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '发起投产校验失败'
  }
}

async function finishPromotionNow() {
  if (!promotion.value) return
  errorMessage.value = ''
  try {
    const payload = await finishPromotion(promotion.value.id)
    promotion.value = await getPromotion(payload.promotion.id)
    await reloadBatches()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '写入校验结论失败'
  }
}

async function requeryPromotion() {
  if (!promotion.value) return
  errorMessage.value = ''
  try {
    promotion.value = await getPromotion(promotion.value.id)
    const overview = await getModelOverview(modelId.value)
    aliasVersion.value = overview.alias_version
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '重新查询投产失败'
  }
}

async function loadHistory(entry: DiffEntry) {
  errorMessage.value = ''
  try {
    const payload = await lookupConfirmedMissing(entry.field_id, modelId.value)
    history.value = payload.items
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '历史缺列查询失败'
  }
}

async function refreshAll() {
  await reloadBatches()
}

onMounted(async () => {
  await loadModels()
  await reloadBatches()
})
</script>

<style scoped>
.field-diff-page .inline-field { display: flex; flex-direction: column; font-size: 12px; color: var(--muted); gap: 4px; }
.field-diff-page .inline-field select { padding: 6px 8px; border: 1px solid var(--border); border-radius: 6px; }
.diff-block { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px; margin-bottom: 14px; }
.block-head { display: flex; justify-content: space-between; align-items: center; gap: 10px; margin-bottom: 8px; }
.block-head h3, .block-head h4 { margin: 0; font-size: 14px; }
.block-actions { display: flex; align-items: center; gap: 10px; }
.block-hint { color: var(--muted); font-size: 12px; }
.data-table tr.selected { background: #eef4ff; cursor: pointer; }
.data-table tbody tr { cursor: pointer; }
.group-row td { background: #f8fafc; color: var(--muted); font-size: 12px; font-weight: 600; cursor: default; }
.section-hint { color: var(--muted); font-size: 12px; margin: 4px 0 8px; }
.tag { display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 12px; background: #f1f5f9; color: #334155; }
.tag-ok { background: #ecfdf3; color: #027a48; }
.tag-danger { background: #fef3f2; color: #b42318; }
.tag-pending { background: #fffaeb; color: #b54708; }
.tag-info { background: #eff8ff; color: #175cd3; }
.tag-muted { background: #f8fafc; color: #64748b; }
.blocking-note { margin-left: 6px; color: #b42318; font-size: 11px; }
.resolution-note { display: block; color: var(--muted); font-size: 11px; }
.mono { font-family: ui-monospace, monospace; font-size: 12px; }
.muted { color: var(--muted); font-size: 12px; }
.snapshot-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 12px; }
.snapshot-card { border: 1px solid var(--border); border-radius: 8px; padding: 10px; }
.snapshot-card header { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
.mini-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.mini-table th, .mini-table td { border-bottom: 1px solid #eef2f6; padding: 4px 6px; text-align: left; }
.promo-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 12px; }
.promo-card { border: 1px solid var(--border); border-radius: 8px; padding: 12px; }
.promo-card h4 { margin: 0 0 8px; font-size: 13px; }
.promo-card dl { margin: 0; }
.promo-card dt { color: var(--muted); font-size: 11px; margin-top: 6px; }
.promo-card dd { margin: 2px 0 0; font-size: 13px; }
.alias-warning { background: #fffaeb; border: 1px solid #fedf89; color: #93370d; border-radius: 6px; padding: 8px; font-size: 12px; }
.promo-summary { background: #f8fafc; border-radius: 6px; padding: 8px; font-size: 12px; color: #334155; }
.chip { display: inline-block; background: #f1f5f9; border-radius: 10px; padding: 1px 8px; font-size: 12px; margin: 0 4px 4px 0; }
.history-panel { margin-top: 12px; border-top: 1px solid var(--border); padding-top: 10px; }
</style>
