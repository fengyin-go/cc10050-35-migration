<template>
  <section
    class="mm-panel audit-backfill"
    data-testid="audit-backfill-panel"
    data-scenario="k-late-backfill"
  >
    <header class="mm-head">
      <h3>审计窗口 · 迟到补报发布</h3>
      <span class="mm-tag" data-testid="audit-site-identity">站点身份：{{ siteLabel }}</span>
    </header>

    <div class="audit-grid">
      <div class="audit-meta">
        <div class="meta-row">
          <span>审计窗口</span>
          <strong data-testid="audit-window-label">{{ canonical?.canonical_window.window_label || '2026-11-26 09:00–10:00' }}</strong>
        </div>
        <div class="meta-row">
          <span>审计时间</span>
          <strong>{{ canonical?.canonical_window.audit_time || '2026-11-26 09:00' }}</strong>
        </div>
        <div class="meta-row">
          <span>站点</span>
          <strong data-testid="audit-site">站点 K（site_key=K）</strong>
        </div>
        <div class="meta-row">
          <span>迟到补报</span>
          <strong class="delta" data-testid="audit-late-amount">+{{ canonical?.canonical_window.amount ?? 2 }}</strong>
        </div>
        <div class="meta-row">
          <span>幂等键</span>
          <code class="key" data-testid="audit-report-key">{{ canonical?.canonical_report_key || 'audit-backfill:K:2026-11-26T09:00:+2' }}</code>
        </div>
      </div>

      <div class="publish-box">
        <label class="conflict-arm">
          <input v-model="armConflict" data-testid="arm-conflict" type="checkbox" />
          <span>模拟「写入与迁移确认发生冲突」（切换确认抢先提交 fence）</span>
        </label>

        <div class="publish-actions">
          <button
            class="btn primary"
            data-testid="publish-backfill"
            type="button"
            :disabled="busy"
            @click="onPublish"
          >
            {{ busy ? '发布中…' : '发布补报' }}
          </button>
          <button
            v-if="conflict"
            class="btn primary continue"
            data-testid="continue-publish"
            type="button"
            :disabled="busy"
            @click="onContinue"
          >
            继续发布（按新 fence 重试，同一补报只计一次）
          </button>
          <RouterLink class="btn ghost" data-testid="goto-legacy" to="/collect_metrics?reader=legacy">
            打开旧累计
          </RouterLink>
          <RouterLink class="btn ghost" data-testid="goto-windows" to="/collect_metrics?reader=window">
            打开新版窗口
          </RouterLink>
        </div>

        <div
          v-if="conflict"
          class="mm-banner conflict"
          data-testid="conflict-banner"
          role="alert"
        >
          <strong>写入与迁移确认发生冲突</strong>
          <p>{{ conflict.message }}</p>
          <p class="hint">补报尚未落库；值班员点击「继续发布」后按当前 fence 幂等提交。</p>
        </div>

        <div v-if="message" class="mm-banner" :class="messageOk ? 'ok' : 'error'" data-testid="publish-message">
          {{ message }}
        </div>

        <div v-if="receipt" class="receipt" data-testid="publish-receipt">
          <div class="receipt-title">补报单据（持久化）</div>
          <dl>
            <div><dt>幂等键</dt><dd>{{ receipt.report_key }}</dd></div>
            <div><dt>数量</dt><dd>+{{ receipt.amount }}</dd></div>
            <div><dt>时间窗</dt><dd>{{ receipt.window_label }}</dd></div>
            <div><dt>写入资格端</dt><dd data-testid="receipt-write-side">{{ writeSideLabel }}</dd></div>
            <div><dt>fence</dt><dd>{{ receipt.fence_version }}</dd></div>
            <div><dt>重复送达次数</dt><dd data-testid="receipt-replay-count">{{ receipt.replay_count }}</dd></div>
            <div><dt>哪版读端收到</dt>
              <dd data-testid="receipt-readers">
                <span v-if="(receipt.read_versions_received || []).length === 0">
                  尚未查询读端（请打开旧累计 / 新版窗口）
                </span>
                <span v-else>{{ (receipt.read_versions_received || []).join('、') }} 已收到</span>
              </dd>
            </div>
          </dl>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'

import {
  conflictDrill,
  fetchState,
  publishBackfill,
  type ConflictBody,
  type MigrationReceipt,
  type MigrationState,
} from '@/api/migration'

const emit = defineEmits<{ (e: 'published'): void }>()

const siteLabel = '站点 K'
const canonical = ref<MigrationState | null>(null)
const armConflict = ref(false)
const busy = ref(false)
const conflict = ref<ConflictBody | null>(null)
const message = ref('')
const messageOk = ref(false)
const receipt = ref<MigrationReceipt | null>(null)

const writeSideLabel = ref('')

function sideText(side: string): string {
  if (side.includes('window')) return '新版时间窗口写入端 window-v2（切换后有写入资格）'
  if (side.includes('legacy')) return '旧累计写入端 legacy-v1（切换前/回滚后有写入资格）'
  return side
}

async function refreshReceipt() {
  const state = await fetchState('K')
  canonical.value = state
  const found = state.receipts.find((item) => item.report_key === state.canonical_report_key)
    || state.receipts[state.receipts.length - 1]
    || null
  receipt.value = found
  if (found) writeSideLabel.value = sideText(found.write_side)
}

async function onPublish() {
  busy.value = true
  message.value = ''
  conflict.value = null
  try {
    if (armConflict.value) {
      // 第一步：确定性复现冲突（切换确认抢先，fence 已升级，补报未落库）。
      const result = await conflictDrill({})
      conflict.value = result.data as ConflictBody
      messageOk.value = false
      message.value = '首次提交被 fence 仲裁拒绝，请继续发布。'
      return
    }
    const result = await publishBackfill({})
    applyPublishResult(result.data)
  } catch (error) {
    messageOk.value = false
    message.value = error instanceof Error ? error.message : '发布失败'
  } finally {
    busy.value = false
  }
}

async function onContinue() {
  if (!conflict.value) return
  busy.value = true
  message.value = ''
  try {
    const result = await publishBackfill(conflict.value.retry)
    conflict.value = null
    applyPublishResult(result.data)
  } catch (error) {
    messageOk.value = false
    message.value = error instanceof Error ? error.message : '继续发布失败'
  } finally {
    busy.value = false
  }
}

function applyPublishResult(data: { ok: boolean; message: string; receipt?: MigrationReceipt }) {
  messageOk.value = Boolean(data.ok)
  message.value = data.message
  if (data.receipt) {
    receipt.value = data.receipt
    writeSideLabel.value = sideText(data.receipt.write_side)
  }
  emit('published')
  void refreshReceipt()
}

function onExternalPublished() {
  void refreshReceipt()
}

onMounted(() => {
  window.addEventListener('metrics-backfill-published', onExternalPublished)
  void refreshReceipt().catch(() => {
    // 后端未启动时保留静态规范值，页面其余功能仍可浏览。
  })
})

onUnmounted(() => {
  window.removeEventListener('metrics-backfill-published', onExternalPublished)
})
</script>

<style scoped>
.mm-panel {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 14px 16px;
  margin: 12px 0;
}
.mm-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 10px; }
.mm-head h3 { margin: 0; font-size: 16px; }
.mm-tag { font-size: 12px; color: #1d4ed8; background: #eff6ff; border: 1px solid #bfdbfe; border-radius: 999px; padding: 2px 10px; }
.audit-grid { display: grid; grid-template-columns: minmax(260px, 1fr) minmax(300px, 1.2fr); gap: 18px; }
.audit-meta { display: flex; flex-direction: column; gap: 8px; }
.meta-row { display: flex; justify-content: space-between; gap: 12px; border-bottom: 1px dashed #e5e7eb; padding-bottom: 6px; font-size: 13px; }
.meta-row span { color: var(--muted); }
.meta-row strong { text-align: right; }
.delta { color: #15803d; font-size: 16px; }
.key { font-size: 11px; color: #475569; word-break: break-all; text-align: right; }
.publish-actions { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.continue { background: #b45309; border-color: #b45309; }
.conflict-arm { display: flex; gap: 8px; align-items: flex-start; font-size: 12px; color: #92400e; background: #fffbeb; border: 1px solid #fde68a; border-radius: 8px; padding: 8px 10px; margin-bottom: 10px; }
.conflict-arm input { margin-top: 2px; }
.mm-banner { margin-top: 10px; border-radius: 8px; padding: 8px 12px; font-size: 13px; }
.mm-banner p { margin: 4px 0 0; }
.mm-banner.conflict { background: #fef2f2; border: 1px solid #fecaca; color: #991b1b; }
.mm-banner .hint { color: #b45309; font-size: 12px; }
.mm-banner.ok { background: #f0fdf4; border: 1px solid #bbf7d0; color: #166534; }
.mm-banner.error { background: #fef2f2; border: 1px solid #fecaca; color: #991b1b; }
.receipt { margin-top: 12px; border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px; background: #f8fafc; }
.receipt-title { font-weight: 600; font-size: 13px; margin-bottom: 6px; }
.receipt dl { margin: 0; display: flex; flex-direction: column; gap: 4px; }
.receipt dl > div { display: grid; grid-template-columns: 96px 1fr; gap: 8px; font-size: 12px; }
.receipt dt { color: var(--muted); }
.receipt dd { margin: 0; word-break: break-all; }
@media (max-width: 900px) { .audit-grid { grid-template-columns: 1fr; } }
</style>
