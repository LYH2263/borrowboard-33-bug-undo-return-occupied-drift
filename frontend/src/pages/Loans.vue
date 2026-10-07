<template>
  <div style="padding:16px">
    <h1>借还记录 · 邻里互借</h1>
    <h3>逾期</h3>
    <div v-for="l in data.overdue" :key="'o'+l.id" class="item overdue">
      {{ l.title }} · {{ l.borrower }}
      <div class="muted">应还 {{ l.due_date }} · 逾期</div>
    </div>
    <h3>在借</h3>
    <div v-for="l in data.active" :key="'a'+l.id" class="item">
      {{ l.title }} · {{ l.borrower }}
      <div class="muted">应还 {{ l.due_date }}</div>
      <div v-if="l.unreturn_reason" class="muted">撤销归还恢复：{{ l.unreturn_reason }}</div>
    </div>
    <h3>已还</h3>
    <div v-for="l in data.returned" :key="'r'+l.id" class="item">
      <strong>{{ l.title }}</strong> · {{ l.borrower }}
      <div class="muted">应还 {{ l.due_date }} · 已还 {{ fmt(l.returned_at) }}</div>
      <template v-if="l.id === latestId">
        <input v-model="form.reason" placeholder="撤销原因（必填）" />
        <div v-if="preview && !preview.can_unreturn" class="muted warn">
          {{ ERR_MSG[preview.reason_code] || '当前不可撤销' }}
        </div>
        <div v-else-if="preview && preview.conflict" class="muted warn">
          该物归还后已被「{{ conflictBorrowers }}」借出，请选择冲突处置：
          <label class="opt"><input type="radio" value="fail" v-model="form.on_conflict" />
            整单失败保持现况</label>
          <label class="opt"><input type="radio" value="bump" v-model="form.on_conflict" />
            挤掉新借，原笔回到在借</label>
        </div>
        <div><button @click="unreturn(l)">撤销归还</button></div>
      </template>
      <div v-else class="muted">历史已还，不可撤销（仅最近一笔可撤）</div>
    </div>
    <h3 v-if="data.cancelled.length">被挤掉的新借</h3>
    <div v-for="l in data.cancelled" :key="'c'+l.id" class="item bumped">
      {{ l.title }} · {{ l.borrower }}
      <div class="muted">撤销归还冲突处置：挤掉此笔新借 · {{ l.unreturn_reason }}</div>
    </div>
    <div v-if="notice" class="ok">{{ notice }}</div>
    <div v-if="error" class="err">{{ error }}</div>
  </div>
</template>
<script setup>
import { ref, reactive, computed, inject } from 'vue'
import { api } from '../api'
const data = ref({ active: [], overdue: [], returned: [], cancelled: [] })
const preview = ref(null)
const reloadBoard = inject('reloadBoard')
const form = reactive({ reason: '', on_conflict: 'fail' })
const error = ref('')
const notice = ref('')

const ERR_MSG = {
  reason_required: '撤销失败：必须填写撤销原因',
  not_latest_return: '撤销失败：只能撤销最近一笔已还',
  not_returned: '撤销失败：该笔不是已还状态',
  item_relent: '撤销失败：该物已被别人借出',
}

// 最近一笔成功归还以后端 returned_at 为准（id 序在撤销/挤掉后不可靠）
const latestId = computed(() => {
  const rs = data.value.returned || []
  if (!rs.length) return null
  return rs.reduce((a, b) => ((b.returned_at || '') > (a.returned_at || '') ? b : a)).id
})
const conflictBorrowers = computed(() =>
  (preview.value?.active_loans || []).map(x => x.borrower).join('、'))

async function load() {
  data.value = await api('/loans')
  preview.value = null
  if (latestId.value != null) {
    // 只读预览：不写任何集合，不影响可借栏/在借栏/顶细条数字
    preview.value = await api('/loans/' + latestId.value + '/unreturn/preview')
  }
}
function fmt(ts) { return ts ? ts.replace('T', ' ').slice(0, 16) : '—' }
async function unreturn(l) {
  error.value = ''; notice.value = ''
  try {
    const r = await api('/loans/' + l.id + '/unreturn', {
      method: 'POST',
      body: JSON.stringify({ reason: form.reason, on_conflict: form.on_conflict }),
    })
    form.reason = ''
    notice.value = r.conflict
      ? `已撤销：原笔回到在借，新借 #${r.bumped_loan_ids.join(', #')} 已被挤掉`
      : '已撤销归还，该笔回到在借栏'
  } catch (e) {
    // 失败（含缺原因/已被借出 fail）后端整单回滚：仅以服务端权威数据重刷
    error.value = ERR_MSG[e.message] || ('撤销失败：' + e.message)
  } finally {
    await Promise.all([load(), reloadBoard()])
  }
}
load()
</script>
