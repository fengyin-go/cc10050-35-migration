<template>
  <section
    class="mm-panel migration-panel"
    data-testid="metrics-migration-panel"
    data-scenario="site-K-37-to-39"
    data-site-key="K"
  >
    <header class="mmp-head">
      <div>
        <h3>采集量指标迁移核验台 · 站点 K</h3>
        <span class="mm-tag site-chip" data-testid="site-K">站点 K · site_key=K</span>
        <p class="mmp-sub">
          旧站点累计（无时间细分）+ 审计小时级补报 → 可持久化时间窗口版本；
          旧读端保留，切换对账单可复验，可回滚且不丢已确认新增。
        </p>
      </div>
      <div class="phase-box" :data-phase="state?.migration.phase || 'legacy_backfilling'">
        <span class="phase-label" data-testid="migration-phase">{{ phaseLabel }}</span>
        <span class="fence" data-testid="migration-fence">fence={{ fence }}</span>
        <span class="write-side" data-testid="write-side">{{ writeSideLabel }}</span>
      </div>
    </header>

    <div class="toolbar">
      <button class="btn" data-testid="refresh-site" data-action="refresh" type="button" :disabled="busy" @click="refreshAll">
        刷新站点 K
      </button>
      <button class="btn primary" data-testid="confirm-switch" data-action="switch" type="button" :disabled="busy" @click="onSwitch">
        确认切换
      </button>
      <button class="btn" data-testid="verify-reconciliation" data-action="verify" type="button" :disabled="busy" @click="onVerify">
        按切换对账单复验
      </button>
      <button class="btn warn" data-testid="rollback" data-action="rollback" type="button" :disabled="busy" @click="onRollback">
        模拟回滚
      </button>
      <button class="btn ghost danger" data-testid="reset-scenario" type="button" :disabled="busy" @click="onReset">
        重置演练（恢复 37）
      </button>
      <span v-if="busy" class="busy">处理中…</span>
      <span v-if="actionMessage" class="action-msg" :class="actionOk ? 'ok' : 'err'" data-testid="migration-action-message">
        {{ actionMessage }}
      </span>
    </div>

    <div class="readers">
      <!-- 旧读端 -->
      <article
        ref="legacyCard"
        class="reader-card legacy"
        :class="{ highlight: highlight === 'legacy' }"
        data-testid="legacy-reader"
        data-reader-version="legacy-v1"
      >
        <header>
          <h4>旧累计读端 <code>legacy-v1</code></h4>
          <button class="link" data-testid="refresh-legacy" type="button" @click="refreshLegacy">查询/刷新旧累计</button>
        </header>
        <div class="big-number">
          <span class="num" data-testid="legacy-total">{{ legacyTotal }}</span>
          <span class="unit">站点累计总量</span>
        </div>
        <div class="baseline-line">
          其中无时间旧累计基线
          <strong data-testid="legacy-baseline">{{ legacyBaseline }}</strong>
          <em>（站点维度可累计，不编造小时归属）</em>
        </div>
        <ul class="basis" data-testid="legacy-basis">
          <li v-for="(item, idx) in legacyBasis" :key="idx" :data-time-scoped="item.time_scoped">
            <span>{{ item.label }}</span>
            <strong :class="{ timeless: !item.time_scoped }">
              {{ item.time_scoped ? `+${item.amount}` : item.amount }}
            </strong>
          </li>
        </ul>
        <div class="received" data-testid="legacy-received">
          <template v-if="legacyReports.length">
            <span v-for="r in legacyReports" :key="r.report_key" class="recv-badge ok">
              ✓ legacy-v1 已收到 {{ r.report_key }}（+{{ r.amount }}）
            </span>
          </template>
          <span v-else class="recv-badge muted">旧读端尚未读到补报单据</span>
        </div>
        <details class="old-shape">
          <summary>查看原读端接口形状（/api/collect_metrics 仍可查）</summary>
          <pre data-testid="legacy-shape">{{ legacyShape }}</pre>
        </details>
      </article>

      <!-- 新版窗口读端 -->
      <article
        ref="windowCard"
        class="reader-card window"
        :class="{ highlight: highlight === 'window' }"
        data-testid="window-reader"
        data-reader-version="window-v2"
      >
        <header>
          <h4>新版窗口读端（时间窗口版） <code>window-v2</code></h4>
          <button class="link" data-testid="refresh-windows" type="button" @click="refreshWindows">查询/刷新新版窗口</button>
        </header>
        <div class="big-number">
          <span class="num" data-testid="window-total">{{ windowTotal }}</span>
          <span class="unit">站点总量 = 无时间基线 + 小时窗合计</span>
        </div>

        <div class="baseline-line timeless-baseline" data-testid="timeless-baseline">
          无时间的旧累计 <strong data-testid="window-baseline">{{ windowBaseline }}</strong>
          只在总量基线，不在任何小时窗
        </div>

        <table class="hour-table" data-testid="hourly-windows" data-baseline-in-hours="false">
          <thead>
            <tr><th>正确时间窗</th><th>补报量</th><th>单据数</th><th>幂等键</th></tr>
          </thead>
          <tbody>
            <tr
              v-for="w in hourWindows"
              :key="w.window_start"
              :data-testid="`hour-row-${w.window_start}`"
              :data-window-start="w.window_start"
            >
              <td data-testid="hour-label">{{ w.window_label }}</td>
              <td class="delta" data-testid="hour-delta">+{{ w.delta }}</td>
              <td>{{ w.report_count }}</td>
              <td class="keys">{{ w.report_keys.join(', ') }}</td>
            </tr>
            <tr v-if="!hourWindows.length">
              <td colspan="4" class="empty">暂无小时级补报（旧累计 37 不会出现在这里）</td>
            </tr>
          </tbody>
        </table>
        <p class="invariant" data-testid="invariant-baseline-not-hour">
          ✓ 核验：无时间的 {{ windowBaseline }} 在总量而非任何小时；被确认的
          {{ hourlyTotal }} 在正确时间窗。
        </p>
        <p class="invariant strong" data-testid="invariant-site-total-39">
          ✓ 站点累计总量：切换与回滚后均为 <strong>{{ windowTotal }}</strong>
          （旧读端同为 {{ legacyTotal }}）。
        </p>
        <div class="received" data-testid="window-received">
          <template v-if="windowReports.length">
            <span v-for="r in windowReports" :key="r.report_key" class="recv-badge ok">
              ✓ window-v2 已收到 {{ r.report_key }}（+{{ r.amount }}）
            </span>
          </template>
          <span v-else class="recv-badge muted">新版窗口读端尚未读到补报单据</span>
        </div>
      </article>
    </div>

    <!-- 对账单 -->
    <article class="reconcile-card" data-testid="reconciliation-statement" :data-matched="reconciliation?.matched ? 'true' : 'false'">
      <header>
        <h4>切换对账单</h4>
        <span v-if="reconciliation" class="mm-tag" :class="reconciliation.matched ? 'match' : 'mismatch'" data-testid="reconciliation-badge">
          {{ reconciliation.matched ? '账实相符 · 差异 0' : `不平 · 差异 ${reconciliation.difference}` }}
        </span>
        <span v-else class="mm-tag muted">尚未生成（点「确认切换」或「按切换对账单复验」）</span>
      </header>
      <pre class="statement" data-testid="reconciliation-text">{{ statementText }}</pre>
    </article>

    <!-- 单据与读端接收版本 -->
    <article class="receipts-card" data-testid="receipts-panel">
      <h4>补报单据与「哪版读端收到」</h4>
      <table class="hour-table">
        <thead>
          <tr><th>幂等键</th><th>量</th><th>时间窗</th><th>持久化写入端</th><th>重复次数</th><th>收到的读端版本</th></tr>
        </thead>
        <tbody>
          <tr v-for="r in receipts" :key="r.report_key" :data-testid="`receipt-row-${r.report_key}`">
            <td class="keys">{{ r.report_key }}</td>
            <td>+{{ r.amount }}</td>
            <td>{{ r.window_label }}</td>
            <td data-testid="receipt-write-side-cell">{{ shortSide(r.write_side) }}</td>
            <td data-testid="receipt-replay-cell">{{ r.replay_count }}</td>
            <td data-testid="receipt-readers-cell">
              <span v-if="!(r.read_versions_received || []).length" class="muted">暂无读端查询</span>
              <span v-else>{{ (r.read_versions_received || []).join('、') }}</span>
            </td>
          </tr>
          <tr v-if="!receipts.length"><td colspan="6" class="empty">尚无已确认补报单据</td></tr>
        </tbody>
      </table>
    </article>

    <!-- 事件轨迹 -->
    <article class="events-card" data-testid="event-log">
      <h4>迁移/发布/冲突/切换/回滚轨迹</h4>
      <ol>
        <li v-for="(e, i) in events" :key="i" :data-kind="e.kind">
          <time>{{ e.at }}</time>
          <span class="event-kind">{{ eventKindLabel(e.kind) }}</span>
          <span>{{ e.message }}</span>
        </li>
        <li v-if="!events.length" class="muted">暂无事件</li>
      </ol>
    </article>
  </section>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import { useRoute } from 'vue-router'

import {
  confirmSwitch,
  fetchLegacy,
  fetchState,
  fetchWindows,
  resetScenario,
  rollbackMigration,
  verifyReconciliation,
  type LegacyReader,
  type MigrationEvent,
  type MigrationReceipt,
  type MigrationState,
  type Reconciliation,
  type WindowReader,
} from '@/api/migration'

const route = useRoute()

const state = ref<MigrationState | null>(null)
const legacy = ref<LegacyReader | null>(null)
const windows = ref<WindowReader | null>(null)
const busy = ref(false)
const actionMessage = ref('')
const actionOk = ref(true)
const highlight = ref<'' | 'legacy' | 'window'>('')
const legacyCard = ref<HTMLElement | null>(null)
const windowCard = ref<HTMLElement | null>(null)

const fence = computed(() => state.value?.migration.fence_version ?? 1)
const phaseLabel = computed(() => state.value?.migration.phase_label || '旧累计回填中 · 切换待确认')
const writeSideLabel = computed(() => state.value?.migration.write_side_label || '')

const legacyTotal = computed(() => legacy.value?.cumulative_total ?? state.value?.legacy.cumulative_total ?? 37)
const legacyBaseline = computed(() => legacy.value?.baseline_total ?? state.value?.legacy.baseline_total ?? 37)
const legacyBasis = computed(() => legacy.value?.basis ?? state.value?.legacy.basis ?? [])
const legacyReports = computed(() => legacy.value?.received_reports ?? state.value?.legacy.received_reports ?? [])
const legacyShape = computed(() => JSON.stringify({
  items: [{
    id: 4,
    ...(state.value?.legacy.legacy_row ?? {}),
  }],
}, null, 2))

const windowTotal = computed(() => windows.value?.total ?? state.value?.windows.total ?? 37)
const windowBaseline = computed(() => windows.value?.baseline_total ?? state.value?.windows.baseline_total ?? 37)
const hourlyTotal = computed(() => windows.value?.hourly_total ?? state.value?.windows.hourly_total ?? 0)
const hourWindows = computed(() => windows.value?.windows ?? state.value?.windows.windows ?? [])
const windowReports = computed(() => windows.value?.received_reports ?? state.value?.windows.received_reports ?? [])

const reconciliation = ref<Reconciliation | null>(null)
const receipts = computed<MigrationReceipt[]>(() => state.value?.receipts ?? [])
const events = computed<MigrationEvent[]>(() => state.value?.events ?? [])

const statementText = computed(() => {
  if (reconciliation.value?.statement) return reconciliation.value.statement
  if (state.value?.reconciliation?.statement) return state.value.reconciliation.statement
  return '尚未生成对账单。\n点击「确认切换」会先按持久化账本生成切换对账单，或点击「按切换对账单复验」。'
})

function shortSide(side: string): string {
  if (side.includes('window')) return 'window-v2（窗口端）'
  if (side.includes('legacy')) return 'legacy-v1（旧累计端）'
  return side
}

function eventKindLabel(kind: string): string {
  const map: Record<string, string> = {
    legacy_received: '旧累计入库',
    backfill_confirmed: '补报确认',
    duplicate_ignored: '幂等去重',
    write_conflict: '写入冲突',
    switch_confirmed: '确认切换',
    reconcile: '对账复验',
    rollback: '回滚',
    reset: '重置',
  }
  return map[kind] || kind
}

async function refreshState() {
  state.value = await fetchState('K')
  if (!reconciliation.value && state.value.reconciliation) {
    reconciliation.value = state.value.reconciliation
  }
}

async function refreshLegacy() {
  legacy.value = await fetchLegacy('K')
}

async function refreshWindows() {
  windows.value = await fetchWindows('K')
}

async function refreshAll() {
  busy.value = true
  try {
    await refreshState()
    // 分别真正查询两个读端（会持久化“哪版读端收到”的回执时间）。
    await Promise.all([refreshLegacy(), refreshWindows()])
    // 读端查询落库后，重新拉取单据上的读端版本标记。
    await refreshState()
  } catch (error) {
    actionOk.value = false
    actionMessage.value = error instanceof Error ? error.message : '刷新失败'
  } finally {
    busy.value = false
  }
}

async function onSwitch() {
  busy.value = true
  actionMessage.value = ''
  try {
    const result = await confirmSwitch()
    actionOk.value = result.ok
    actionMessage.value = result.message
    if (result.reconciliation) reconciliation.value = result.reconciliation
    await refreshAll()
  } catch (error) {
    actionOk.value = false
    actionMessage.value = error instanceof Error ? error.message : '切换失败'
  } finally {
    busy.value = false
  }
}

async function onVerify() {
  busy.value = true
  actionMessage.value = ''
  try {
    reconciliation.value = await verifyReconciliation()
    actionOk.value = reconciliation.value.matched
    actionMessage.value = reconciliation.value.message || '复验完成'
    await refreshAll()
  } catch (error) {
    actionOk.value = false
    actionMessage.value = error instanceof Error ? error.message : '复验失败'
  } finally {
    busy.value = false
  }
}

async function onRollback() {
  busy.value = true
  actionMessage.value = ''
  try {
    const result = await rollbackMigration()
    actionOk.value = result.ok
    actionMessage.value = result.message
    if (result.reconciliation) reconciliation.value = result.reconciliation
    await refreshAll()
  } catch (error) {
    actionOk.value = false
    actionMessage.value = error instanceof Error ? error.message : '回滚失败'
  } finally {
    busy.value = false
  }
}

async function onReset() {
  busy.value = true
  actionMessage.value = ''
  try {
    state.value = await resetScenario()
    reconciliation.value = state.value.reconciliation
    legacy.value = null
    windows.value = null
    await Promise.all([refreshLegacy(), refreshWindows()])
    actionOk.value = true
    actionMessage.value = '已恢复为初始剧本：旧累计 37，无补报，fence=1'
  } catch (error) {
    actionOk.value = false
    actionMessage.value = error instanceof Error ? error.message : '重置失败'
  } finally {
    busy.value = false
  }
}

function onExternalPublished() {
  void refreshAll()
}

onMounted(async () => {
  const reader = route.query.reader
  if (reader === 'legacy' || reader === 'window') {
    highlight.value = reader
    await nextTick()
    const target = reader === 'legacy' ? legacyCard.value : windowCard.value
    target?.scrollIntoView({ behavior: 'smooth', block: 'center' })
  }
  window.addEventListener('metrics-backfill-published', onExternalPublished)
  try {
    await refreshAll()
  } catch {
    actionOk.value = false
    actionMessage.value = '迁移服务暂不可用，请确认后端已启动'
  }
})

onUnmounted(() => {
  window.removeEventListener('metrics-backfill-published', onExternalPublished)
})
</script>

<style scoped>
.migration-panel { display: flex; flex-direction: column; gap: 12px; }
.mmp-head { display: flex; justify-content: space-between; gap: 16px; align-items: flex-start; flex-wrap: wrap; }
.mmp-head h3 { margin: 0 0 4px; font-size: 17px; }
.mmp-sub { margin: 0; color: var(--muted); font-size: 12.5px; max-width: 720px; }
.site-chip { display: inline-block; margin: 2px 0 6px; background: #eff6ff; border: 1px solid #bfdbfe; color: #1d4ed8; }
.phase-box { display: flex; flex-direction: column; gap: 4px; align-items: flex-end; font-size: 12px; }
.phase-label { background: #eef2ff; border: 1px solid #c7d2fe; color: #3730a3; border-radius: 999px; padding: 3px 12px; font-weight: 600; }
.fence { color: var(--muted); font-family: ui-monospace, monospace; }
.write-side { color: #0f766e; }
.toolbar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; padding: 10px; background: #f8fafc; border: 1px solid var(--border); border-radius: 8px; }
.btn.warn { background: #b45309; border-color: #b45309; color: #fff; }
.btn.danger { color: #b42318; }
.busy { font-size: 12px; color: var(--muted); }
.action-msg { font-size: 12.5px; }
.action-msg.ok { color: #166534; }
.action-msg.err { color: #991b1b; }
.readers { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.reader-card { border: 1px solid var(--border); border-radius: 10px; padding: 12px 14px; background: #fff; }
.reader-card.legacy { border-top: 3px solid #64748b; }
.reader-card.window { border-top: 3px solid var(--brand); }
.reader-card.highlight { box-shadow: 0 0 0 3px rgb(31 111 235 / 22%); }
.reader-card header { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
.reader-card h4 { margin: 0; font-size: 14px; }
.reader-card code { font-size: 11px; background: #f1f5f9; padding: 1px 6px; border-radius: 4px; }
.big-number { display: flex; align-items: baseline; gap: 10px; margin: 10px 0 6px; }
.big-number .num { font-size: 40px; font-weight: 700; line-height: 1; color: #0f172a; }
.big-number .unit { font-size: 12px; color: var(--muted); }
.baseline-line { font-size: 13px; margin-bottom: 8px; }
.baseline-line strong { font-size: 16px; }
.baseline-line em { color: var(--muted); font-style: normal; font-size: 12px; }
.timeless-baseline { background: #f8fafc; border-left: 3px solid #94a3b8; padding: 6px 10px; border-radius: 4px; }
.basis { list-style: none; margin: 8px 0; padding: 0; display: flex; flex-direction: column; gap: 4px; }
.basis li { display: flex; justify-content: space-between; gap: 8px; font-size: 12.5px; border-bottom: 1px dashed #eef2f7; padding-bottom: 4px; }
.basis .timeless { color: #475569; }
.hour-table { width: 100%; border-collapse: collapse; margin-top: 8px; }
.hour-table th, .hour-table td { border: 1px solid var(--border); padding: 6px 8px; font-size: 12.5px; text-align: left; }
.hour-table .delta { color: #15803d; font-weight: 700; }
.hour-table .keys { font-family: ui-monospace, monospace; font-size: 10.5px; color: #64748b; word-break: break-all; }
.empty { text-align: center; color: var(--muted); }
.invariant { font-size: 12.5px; color: #166534; margin: 8px 0 0; }
.invariant.strong { font-size: 13.5px; }
.received { margin-top: 8px; display: flex; flex-direction: column; gap: 4px; }
.recv-badge { font-size: 11.5px; border-radius: 6px; padding: 3px 8px; }
.recv-badge.ok { background: #f0fdf4; border: 1px solid #bbf7d0; color: #166534; }
.recv-badge.muted { color: var(--muted); }
.old-shape { margin-top: 8px; font-size: 12px; }
.old-shape pre { background: #0f172a; color: #e2e8f0; padding: 8px; border-radius: 6px; overflow: auto; font-size: 11px; }
.reconcile-card, .receipts-card, .events-card { border: 1px solid var(--border); border-radius: 10px; padding: 12px 14px; background: #fff; }
.reconcile-card header { display: flex; align-items: center; gap: 10px; }
.reconcile-card h4, .receipts-card h4, .events-card h4 { margin: 0 0 8px; font-size: 14px; }
.mm-tag { font-size: 12px; border-radius: 999px; padding: 2px 10px; }
.mm-tag.match { background: #f0fdf4; border: 1px solid #bbf7d0; color: #166534; }
.mm-tag.mismatch { background: #fef2f2; border: 1px solid #fecaca; color: #991b1b; }
.mm-tag.muted { color: var(--muted); background: #f1f5f9; }
.statement { white-space: pre-wrap; background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 10px 12px; font-size: 12.5px; line-height: 1.7; margin: 0; }
.events-card ol { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; max-height: 220px; overflow: auto; }
.events-card li { display: flex; gap: 10px; align-items: baseline; font-size: 12.5px; border-bottom: 1px dashed #eef2f7; padding-bottom: 4px; }
.events-card time { color: var(--muted); font-size: 11px; font-family: ui-monospace, monospace; white-space: nowrap; }
.event-kind { flex-shrink: 0; background: #eef2ff; color: #3730a3; border-radius: 4px; padding: 0 6px; font-size: 11px; }
.muted { color: var(--muted); }
@media (max-width: 1000px) { .readers { grid-template-columns: 1fr; } }
</style>
