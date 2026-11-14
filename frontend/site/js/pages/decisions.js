import { api } from '../api.js';
import { ENUMS, can, verdictTone } from '../config.js';
import { versionLine } from '../analysis.js';
import {
  badge,
  drawer,
  errorBox,
  errorMessage,
  esc,
  field,
  fieldValue,
  filterBar,
  fmtP,
  fmtTime,
  num,
  pct,
  signedPct,
  table,
  toast,
} from '../ui.js';

const state = { verdict: '', q: '' };

function exportButtons(role) {
  if (!can(role, 'export:read')) {
    return '<span class="mono muted">只读角色不能导出（服务端 403）</span>';
  }
  return `<div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
    <button class="btn sm ghost" data-export="experiments">实验台账 CSV</button>
    <button class="btn sm ghost" data-export="assignments">分流样本 CSV</button>
    <button class="btn sm ghost" data-export="metrics">指标检验 CSV</button>
    <button class="btn sm ghost" data-export="decisions">决策清单 CSV</button>
  </div>
  <p class="mono muted" style="margin:6px 0 0">导出的 CSV 带「分析版本 v / 版本状态 / 可用曝光区间」列；默认只导出生效版本，失效版本需显式指定版本号追溯。</p>`;
}

function openDecision(view, ctx, experiments, allPending) {
  const pending = allPending.filter((item) => item.experiment.analysis && item.experiment.analysis.version_id);
  if (!pending.length) {
    toast('待决策实验还没有生效分析版本：先在「样本与分流」登记可用曝光区间并整套发布', 'err');
    return;
  }
  const first = pending[0];
  const { root, close } = drawer({
    title: '出决策',
    subtitle: `${first.experiment.code} · v${first.experiment.analysis?.revision || '—'} · ${first.primary.metric_name || '主指标'} q=${fmtP(first.primary.q_value)}`,
    body: `<form id="decision-form" style="display:grid;gap:12px">
      ${field({
        label: '实验（仅列未决策）',
        name: 'experiment_id',
        options: pending.map((item) => ({ value: item.experiment.id, label: `${item.experiment.code} · ${item.experiment.name}` })),
      })}
      ${field({
        label: '结论',
        name: 'verdict',
        value: first.suggested,
        options: Object.entries(ENUMS.verdict).map(([value, label]) => ({ value, label })),
      })}
      ${field({
        label: '理由（写进会议纪要）',
        name: 'rationale',
        placeholder: '主指标 lift 10.9%，BH 校正后 q=0.044 < 0.05，样本量已过功效线，上线。',
      })}
      ${field({
        label: '依据的分析版本',
        name: 'analysis_version_id',
        value: first.experiment.analysis?.version_id || '',
        options: pending.map((item) => ({
          value: item.experiment.analysis?.version_id || '',
          label: `${item.experiment.code} · v${item.experiment.analysis?.revision || '—'} · ${item.experiment.analysis?.interval_label || '无区间'}`,
        })),
      })}
      <div class="form-error" id="decision-error"></div>
      <button class="btn accent" type="submit">写入决策（绑定该分析版本）</button>
    </form>
    <div class="note" style="margin-top:10px">决策绑定到分析版本：区间一改旧版本立即失效，旧版本上的决策会被拒绝；同一版本只能有一条决策（409）。</div>`,
  });
  const form = root.querySelector('#decision-form');
  const syncSuggestion = () => {
    const picked = pending.find((item) => String(item.experiment.id) === String(fieldValue(form, 'experiment_id')));
    if (!picked) return;
    form.elements.namedItem('verdict').value = picked.suggested;
    form.elements.namedItem('rationale').value = picked.rationale;
    const versionControl = form.elements.namedItem('analysis_version_id');
    if (versionControl) versionControl.value = picked.experiment.analysis?.version_id || '';
  };
  form.elements.namedItem('experiment_id').addEventListener('change', syncSuggestion);
  form.elements.namedItem('rationale').value = first.rationale;
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const errorNode = root.querySelector('#decision-error');
    try {
      const payload = {
        verdict: fieldValue(form, 'verdict'),
        rationale: fieldValue(form, 'rationale', { trim: true }),
      };
      const versionValue = fieldValue(form, 'analysis_version_id');
      if (versionValue) payload.analysis_version_id = Number(versionValue);
      await api.post(`/api/experiments/${fieldValue(form, 'experiment_id')}/decisions`, payload);
      toast('决策已写入，已绑定分析版本');
      close();
      await decisionsPage.render(view, ctx);
    } catch (err) {
      errorNode.textContent =
        err.code === 'CONFLICT'
          ? err.detail?.reason || '该分析版本已有决策或版本已失效（409）'
          : err.code === 'NO_ANALYSIS_VERSION'
            ? '还没有生效分析版本（409）：先在「样本与分流」登记可用曝光区间'
            : errorMessage(err);
    }
  });
}

export const decisionsPage = {
  async render(view, ctx) {
    let decisions;
    let experiments;
    let audit = null;
    try {
      const tasks = [
        api.get('/api/decisions', { limit: 50 }),
        api.get('/api/experiments', { limit: 50 }),
      ];
      if (can(ctx.user.role, 'audit:read')) tasks.push(api.get('/api/audit', { limit: 20 }));
      const [decisionData, experimentData, auditData] = await Promise.all(tasks);
      decisions = decisionData;
      experiments = experimentData;
      audit = auditData || null;
    } catch (err) {
      view.innerHTML = errorBox(errorMessage(err));
      view.querySelector('#retry-btn')?.addEventListener('click', () => decisionsPage.render(view, ctx));
      return;
    }

    // 只有「当前生效版本上已有决策」的实验算已决策；失效版本上的旧决策不算
    const decidedIds = new Set(decisions.items.filter((item) => !item.stale).map((item) => String(item.experiment_id)));
    const pendingExperiments = experiments.items.filter((item) => !decidedIds.has(String(item.id)));
    const details = await Promise.all(
      pendingExperiments.map((item) => api.get(`/api/experiments/${item.id}`).catch(() => item)),
    );
    const pending = details
      .map((experiment) => {
        const primary = (experiment.metrics || []).find((metric) => metric.is_primary) || {};
        const analysis = experiment.analysis || {};
        const staleDecision = (experiment.decision || null) === null && (experiment.decision_history || []).some((item) => item.stale);
        const suggested = primary.significant ? (primary.lift >= 0 ? 'ship' : 'stop') : 'continue';
        const rationale = primary.metric_name
          ? `${primary.metric_name}：lift ${signedPct(primary.lift, 1)}，p=${fmtP(primary.p_value)}，q=${fmtP(primary.q_value)}；${
              primary.significant ? '校正后显著' : '校正后不显著，样本或效应不足'
            }，建议${ENUMS.verdict[suggested]}。`
          : '还没有检验结果，建议继续观察。';
        return { experiment, primary, suggested, rationale, staleDecision };
      });

    view.innerHTML = `
      <section class="panel">
        <div class="panel-head">
          <h2>决策与导出</h2>
          <div style="display:flex;gap:8px;align-items:center">
            <span class="meta">已决策 ${decisions.total} / ${experiments.total}</span>
            ${
              can(ctx.user.role, 'decision:write')
                ? `<button class="btn sm accent" id="new-decision" ${pending.some((item) => item.experiment.analysis && item.experiment.analysis.version_id) ? '' : 'disabled'}>出决策</button>`
                : '<span class="mono muted">决策需实验负责人</span>'
            }
          </div>
        </div>
        <div class="panel-body">
          ${filterBar({
            id: 'decision-filters',
            fields: [
              field({
                label: '结论',
                name: 'verdict',
                value: state.verdict,
                options: [{ value: '', label: '全部' }, ...Object.entries(ENUMS.verdict).map(([value, label]) => ({ value, label }))],
              }),
              field({ label: '搜索理由', name: 'q', value: state.q, placeholder: '理由 / 决策人', grow: true }),
            ],
          })}
        </div>
        <div class="panel-body" style="padding-top:0">${exportButtons(ctx.user.role)}</div>
        ${table({
          rows: decisions.items.filter(
            (row) =>
              (!state.verdict || row.verdict === state.verdict) &&
              (!state.q || `${row.rationale}${row.decided_by}${row.experiment_code}`.includes(state.q)),
          ),
          columns: [
            { key: 'experiment_code', label: '实验', className: 'mono nowrap' },
            { key: 'experiment_name', label: '名称' },
            {
              key: 'analysis_revision',
              label: '分析版本',
              render: (row) =>
                `v${esc(row.analysis_revision)}${row.analysis_version_publication ? `.${esc(row.analysis_version_publication)}` : ''}${
                  row.stale ? ' ' + badge('stop', '已失效') : ''
                }<div class="mono muted">${esc(row.interval_label)}</div>`,
            },
            { key: 'verdict', label: '结论', render: (row) => badge(verdictTone(row.verdict), row.verdict_label) },
            { key: 'rationale', label: '理由', render: (row) => `<span>${esc(row.rationale)}</span>` },
            { key: 'decided_by', label: '决策人', className: 'mono nowrap' },
            { key: 'decided_at', label: '时间', render: (row) => fmtTime(row.decided_at) },
          ],
          empty: { title: '还没有决策', hint: '选一个已跑完检验的实验出结论。' },
        })}
      </section>

      ${
        pending.length
          ? `<div class="grid-2">
              ${pending
                .slice(0, 2)
                .map(
                  (item) => `<section class="panel">
                    <div class="panel-head">
                      <h2>${esc(item.experiment.code)} 待决策</h2>
                      ${badge(verdictTone(item.suggested), `建议 ${ENUMS.verdict[item.suggested]}`)}
                    </div>
                    <div class="panel-body" style="display:grid;gap:6px">
                      <div><b>${esc(item.experiment.name)}</b></div>
                      <div class="mono muted">${esc(item.experiment.primary_metric_label)} · SRM ${esc(item.experiment.srm.status_label)} · p=${fmtP(
                        item.experiment.srm.p_value,
                      )}</div>
                      <div>${item.experiment.analysis?.version_id
                        ? versionLine({ revision: item.experiment.analysis.revision, status: 'active', interval_label: item.experiment.analysis.interval_label, published_at: item.experiment.analysis.published_at })
                        : '<span class="fail">还没有生效分析版本：先在「样本与分流」登记可用曝光区间</span>'}</div>
                      ${item.staleDecision ? '<p class="fail" style="margin:0">区间调整后旧版本的决策已失效，需要按当前版本重新出结论。</p>' : ''}
                      ${item.primary.metric_name
                        ? `<div class="checkline">
                            <div class="item"><span class="k">lift</span><span class="v">${signedPct(item.primary.lift, 1)}</span></div>
                            <div class="item"><span class="k">p</span><span class="v">${fmtP(item.primary.p_value)}</span></div>
                            <div class="item"><span class="k">q</span><span class="v ${item.primary.significant ? 'pass' : 'fail'}">${fmtP(item.primary.q_value)}</span></div>
                          </div>`
                        : '<p class="muted">还没有检验结果。</p>'}
                      <p class="muted" style="margin:0">${esc(item.rationale)}</p>
                    </div>
                  </section>`,
                )
                .join('')}
            </div>`
          : ''
      }

      ${
        audit
          ? `<section class="panel">
              <div class="panel-head"><h2>审计日志</h2><span class="meta">${audit.total} 条 · 最近 ${audit.items.length} 条</span></div>
              ${table({
                rows: audit.items,
                columns: [
                  { key: 'created_at', label: '时间', render: (row) => fmtTime(row.created_at) },
                  { key: 'actor_email', label: '操作人', className: 'mono' },
                  { key: 'actor_role', label: '角色' },
                  { key: 'action_label', label: '动作' },
                  { key: 'entity', label: '对象' },
                  { key: 'layer', label: '层', render: (row) => badge('', row.layer) },
                  { key: 'detail', label: '详情', render: (row) => `<span class="mono muted">${esc(JSON.stringify(row.detail || {}))}</span>` },
                ],
                empty: { title: '没有审计记录', hint: '登录、登记、校验、导出都会留痕。' },
              })}
            </section>`
          : ''
      }
    `;

    const filters = view.querySelector('#decision-filters');
    filters?.addEventListener('change', () => {
      state.verdict = filters.querySelector('[name=verdict]').value;
      state.q = filters.querySelector('[name=q]').value.trim();
      decisionsPage.render(view, ctx);
    });
    view.querySelector('#new-decision')?.addEventListener('click', () => openDecision(view, ctx, experiments.items, pending));
    view.querySelectorAll('[data-export]').forEach((button) => {
      button.addEventListener('click', async () => {
        const kind = button.dataset.export;
        button.disabled = true;
        try {
          await api.download(`/api/exports/${kind}.csv`, `${kind}.csv`);
          toast(`已下载 ${kind}.csv（含分析版本与区间列）`);
        } catch (err) {
          toast(errorMessage(err), 'err');
        } finally {
          button.disabled = false;
        }
      });
    });
  },
};

export default decisionsPage;
