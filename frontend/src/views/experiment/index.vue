<template>
  <section class="page" data-module="experiment">
    <header class="page-head">
      <div><h2>分析实验</h2><p class="page-desc">维护实验方案，围绕方案编号、实验名称、分析数据集、样本范围做登记、筛选与状态流转。</p></div>
    </header>
    <div class="analysis-summary">
      <article v-for="m in summaryMetrics" :key="m.label" class="analysis-card" data-testid="kpi-card">
        <span style="font-size:12px;color:var(--muted)">{{ m.label }}</span>
        <strong style="font-size:22px;display:block">{{ m.value }}</strong>
        <span :class="m.trend > 0 ? 'trend-up' : 'trend-down'" style="font-size:12px">{{ m.trend > 0 ? '↑' : '↓' }}{{ Math.abs(m.trend) }}%</span>
      </article>
    </div>
    <form class="analysis-form" @submit.prevent="runAnalysis">
      <label v-for="field in paramFields" :key="field"><span>{{ field }}</span>
        <select v-if="field.includes('方法')||field.includes('策略')||field.includes('粒度')" v-model="params[field]"><option v-for="o in options[field]" :key="o" :value="o">{{ o }}</option></select>
        <input v-else v-model="params[field]" :placeholder="`请输入${field}`" />
      </label>
      <button class="btn primary" type="submit" data-testid="run-analysis">运行分析</button>
    </form>
    <div v-if="chartData.length" class="analysis-chart">
      <div v-for="(bar, i) in chartData" :key="i" class="analysis-bar" :style="{ height: barHeight(bar.value) + 'px' }"><span>{{ bar.label }}</span></div>
    </div>
    <table class="data-table" v-if="results.length">
      <thead><tr><th v-for="c in resultColumns" :key="c">{{ c }}</th></tr></thead>
      <tbody><tr v-for="row in results" :key="row.id"><td v-for="c in resultColumns" :key="c">{{ row[c] ?? '—' }}</td></tr></tbody>
    </table>
    <p v-if="!results.length && !loading" style="text-align:center;color:var(--muted)">请配置参数后运行分析</p>

    <!-- 显著阈值版本：切换版本查看与当前规则一致的显著性结论 -->
    <div class="threshold-panel" data-testid="threshold-panel">
      <div class="page-head">
        <div>
          <h3>显著性结论（按阈值版本重算）</h3>
          <p class="page-desc">阈值版本与基准区间一起参与结论计算；切换版本即按该版本的阈值和区间重算分组差异判定。</p>
        </div>
        <div class="page-actions">
          <button class="btn primary" type="button" data-testid="recalculate" :disabled="recalculating" @click="recalculate">
            {{ recalculating ? '重算中…' : '按当前阈值版本重算' }}
          </button>
          <button class="btn" type="button" data-testid="judgement-export" @click="exportJudgements">导出显著性结论</button>
        </div>
      </div>

      <form class="filter-bar" @submit.prevent="loadResults">
        <label class="filter-item">
          <span>处理范围</span>
          <select v-model="scope" data-testid="scope-select" @change="onScopeChange">
            <option v-for="s in scopes" :key="s" :value="s">{{ s }}</option>
          </select>
        </label>
        <label class="filter-item">
          <span>显著阈值版本</span>
          <select v-model.number="selectedVersionId" data-testid="version-select" @change="loadResults">
            <option v-for="v in versions" :key="v.id" :value="v.id">
              {{ v.版本编号 }}｜{{ v.版本名称 }}｜阈值 {{ v.显著阈值 }}｜区间 {{ v.基准区间 }}
            </option>
          </select>
        </label>
        <button class="btn" type="submit" data-testid="threshold-query">查询</button>
      </form>

      <div v-if="activeVersion" class="threshold-meta" data-testid="threshold-meta">
        <span>当前结论口径：<strong>{{ activeVersion.版本编号 }}</strong>（{{ activeVersion.版本名称 }}）</span>
        <span>显著阈值：<strong>{{ activeVersion.显著阈值 }}</strong></span>
        <span>基准区间：<strong>{{ activeVersion.基准区间 }}</strong></span>
        <span>处理范围：<strong>{{ scope }}</strong></span>
        <span>显著 {{ significantCount }} 组 / 不显著 {{ insignificantCount }} 组</span>
      </div>

      <table class="data-table" data-testid="judgement-table">
        <thead>
          <tr>
            <th v-for="c in judgementColumns" :key="c">{{ c }}</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in judgements" :key="row.id" :data-testid="`judgement-row-${row.id}`" :data-significant="String(row.是否显著)">
            <td v-for="c in judgementColumns" :key="c">
              <span v-if="c === '阈值判定'" :class="row.是否显著 ? 'tag tag-significant' : 'tag tag-insignificant'">{{ row[c] }}</span>
              <span v-else :title="c === '不显著原因' ? row[c] : ''">{{ row[c] || '—' }}</span>
            </td>
            <td><button class="link" type="button" :data-testid="`judgement-detail-${row.id}`" @click="openDetail(row.id)">查看判定记录</button></td>
          </tr>
          <tr v-if="!judgements.length">
            <td :colspan="judgementColumns.length + 1" class="empty-state">暂无判定记录，点击「按当前阈值版本重算」生成</td>
          </tr>
        </tbody>
      </table>
      <p v-if="thresholdError" class="error-text" data-testid="threshold-error">{{ thresholdError }}</p>
    </div>

    <div v-if="detail" class="modal-backdrop" data-testid="judgement-modal" @click.self="detail = null">
      <div class="modal">
        <h3>阈值判定记录详情</h3>
        <dl class="dag-detail">
          <template v-for="field in detailFields" :key="field">
            <dt>{{ field }}</dt>
            <dd :data-testid="`detail-${field}`">{{ detail[field] ?? '—' }}</dd>
          </template>
          <dt>业务输入</dt>
          <dd data-testid="detail-业务输入">{{ JSON.stringify(detail.业务输入) }}</dd>
        </dl>
        <div class="page-actions">
          <button class="btn primary" type="button" @click="detail = null">关闭</button>
        </div>
      </div>
    </div>
  </section>
</template>
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { request } from '@/api/client'
const ENDPOINT = '/api/experiment'
const summaryMetrics = ref<{label:string;value:number;trend:number}[]>([])
const results = ref<any[]>([]); const chartData = ref<{label:string;value:number}[]>([])
const loading = ref(false)
const params = ref<Record<string,string>>({})
const paramFields = ["方案编号", "实验名称", "分析数据集", "样本范围", "分组字段", "目标度量", "基准区间", "显著阈值"].filter((f: string) => !['编号','名称','版本'].includes(f)).slice(0, 4)
const options: Record<string,string[]> = { '统计粒度': ['按日','按周','按月'], '分组字段': ['全量','按类型','按区域'], '比较方法': ['环比','同比','绝对值'], '时间范围': ['最近7天','最近30天','最近90天'] }
const resultColumns = computed(() => ["方案编号", "实验名称", "分析数据集", "样本范围", "分组字段", "目标度量", "基准区间", "显著阈值"].slice(0, 5))
function barHeight(v: number) { const max = Math.max(...chartData.value.map(d => d.value), 1); return Math.max(10, (v / max) * 140) }
async function fetchSummary() { try { const res = await request(`${ENDPOINT}/summary`); summaryMetrics.value = (await res.json()).metrics || [] } catch(e) {} }
async function runAnalysis() {
  loading.value = true
  try { const res = await request(`${ENDPOINT}/run`, { method: 'POST', body: JSON.stringify({ values: params.value }) }); const data = await res.json(); results.value = data.items || []; chartData.value = (data.items || []).map((r: any) => ({ label: Object.values(r)[0] as string, value: Object.values(r).filter(v => typeof v === 'number')[0] as number || 0 })).slice(0, 6) } catch(e) { results.value = [] }
  loading.value = false
}

// ---- 显著阈值版本与阈值判定记录 ----
type VersionRow = { id: number; 版本编号: string; 版本名称: string; 显著阈值: number; 基准区间: string; 版本来源: string; 生效状态: string }
type JudgementRow = Record<string, any>
const scopes = ['运行期生效值', '设计冻结值']
const scope = ref('运行期生效值')
const versions = ref<VersionRow[]>([])
const selectedVersionId = ref<number | null>(null)
const judgements = ref<JudgementRow[]>([])
const detail = ref<JudgementRow | null>(null)
const recalculating = ref(false)
const thresholdError = ref('')
const judgementColumns = ['方案编号', '分组', '版本编号', '显著阈值', '基准区间', '分组差异', 'p值', '阈值判定', '不显著原因', '重算时间']
const detailFields = ['方案编号', '分组', '版本编号', '版本名称', '处理范围', '显著阈值', '基准区间', '实验组样本量', '实验组转化数', '实验组指标值', '对照组样本量', '对照组转化数', '对照组指标值', '分组差异', 'z统计量', 'p值', '阈值判定', '不显著原因', '判定条件', '重算时间']
const activeVersion = computed(() => versions.value.find(v => v.id === selectedVersionId.value) ?? null)
const significantCount = computed(() => judgements.value.filter(r => r.是否显著).length)
const insignificantCount = computed(() => judgements.value.filter(r => !r.是否显著).length)

async function fetchVersions() {
  const res = await request(`${ENDPOINT}/threshold-versions`)
  const data = await res.json()
  versions.value = data.items || []
  if (!selectedVersionId.value && versions.value.length) {
    const fallback = scope.value === '设计冻结值'
      ? versions.value.find(v => v.版本来源 === '设计冻结')
      : versions.value.find(v => v.生效状态 === '生效中')
    selectedVersionId.value = (fallback ?? versions.value[0]).id
  }
}

function onScopeChange() {
  const fallback = scope.value === '设计冻结值'
    ? versions.value.find(v => v.版本来源 === '设计冻结')
    : versions.value.find(v => v.生效状态 === '生效中')
  if (fallback) selectedVersionId.value = fallback.id
  void loadResults()
}

async function loadResults() {
  thresholdError.value = ''
  if (selectedVersionId.value == null) return
  try {
    const query = new URLSearchParams({ scope: scope.value, version_id: String(selectedVersionId.value) })
    const res = await request(`${ENDPOINT}/results?${query.toString()}`)
    if (!res.ok) {
      const payload = await res.json().catch(() => ({}))
      throw new Error(payload.detail || '显著性结论读取失败')
    }
    const data = await res.json()
    judgements.value = data.items || []
  } catch (error) {
    thresholdError.value = error instanceof Error ? error.message : '显著性结论读取失败'
    judgements.value = []
  }
}

async function recalculate() {
  if (selectedVersionId.value == null) return
  recalculating.value = true
  thresholdError.value = ''
  try {
    const query = new URLSearchParams({ scope: scope.value, version_id: String(selectedVersionId.value) })
    const res = await request(`${ENDPOINT}/recalculate?${query.toString()}`, { method: 'POST' })
    const payload = await res.json()
    if (!res.ok || !payload.ok) throw new Error(payload.detail || payload.message || '重算失败')
    await loadResults()
  } catch (error) {
    thresholdError.value = error instanceof Error ? error.message : '重算失败'
  } finally {
    recalculating.value = false
  }
}

async function exportJudgements() {
  if (selectedVersionId.value == null) return
  try {
    const query = new URLSearchParams({ scope: scope.value, version_id: String(selectedVersionId.value) })
    const res = await request(`${ENDPOINT}/export?${query.toString()}`)
    if (!res.ok) throw new Error('导出失败，请稍后重试')
    const url = URL.createObjectURL(await res.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = 'experiment_judgement.csv'
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    thresholdError.value = error instanceof Error ? error.message : '导出失败'
  }
}

async function openDetail(judgementId: number) {
  thresholdError.value = ''
  try {
    const res = await request(`${ENDPOINT}/judgements/${judgementId}`)
    if (!res.ok) throw new Error('阈值判定记录读取失败')
    detail.value = await res.json()
  } catch (error) {
    thresholdError.value = error instanceof Error ? error.message : '阈值判定记录读取失败'
  }
}

onMounted(async () => {
  await fetchSummary()
  await fetchVersions()
  await loadResults()
})
</script>
<style scoped>
.threshold-panel { margin-top: 24px; background: var(--bg, #f6f8fb); border: 1px solid var(--border); border-radius: 10px; padding: 16px; }
.threshold-meta { display: flex; flex-wrap: wrap; gap: 16px; background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px; margin: 10px 0; font-size: 13px; }
.tag { display: inline-block; padding: 2px 10px; border-radius: 999px; font-size: 12px; }
.tag-significant { background: #dcfce7; color: #15803d; }
.tag-insignificant { background: #fee2e2; color: #b91c1c; }
</style>
