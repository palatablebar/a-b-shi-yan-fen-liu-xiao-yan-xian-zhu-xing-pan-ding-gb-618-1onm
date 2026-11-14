/** 分析版本 UI：可用曝光区间编辑器 + 版本选择器 + 状态标记。 */
import { ENUMS } from './config.js';
import { badge, esc, field, fmtTime, pct, table } from './ui.js';

export function analysisStatusBadge(status) {
  const tone = { active: 'ok', draft: 'pending', superseded: 'stop' }[status] || 'info';
  return badge(tone, ENUMS.analysisStatus[status] || status);
}

export function intervalChips(segments) {
  if (!segments || !segments.length) return '<span class="mono muted">未登记区间</span>';
  return segments
    .map((item) => `<span class="badge info mono">${esc(item.start)} ~ ${esc(item.end)}</span>`)
    .join(' ');
}

export function versionLine(analysis) {
  if (!analysis || !analysis.revision) {
    return `<span class="mono muted">还没有分析版本 —— 先在「样本与分流」登记可用曝光区间并整套发布</span>`;
  }
  return `<span class="mono">分析版本 v${esc(analysis.revision)}${
    analysis.publication ? `.${esc(analysis.publication)} 次发布` : ''
  }</span> ${analysisStatusBadge(analysis.status)}
    <span class="mono muted">可用曝光区间 ${esc(analysis.interval_label)}</span>
    <span class="mono muted">发布 ${fmtTime(analysis.published_at)}</span>`;
}

/** 把区间数组格式化成每行一段的文本：2026-06-01~2026-06-09 */
export function segmentsToText(segments) {
  if (!segments || !segments.length) return '';
  return segments.map((item) => `${item.start}~${item.end}`).join('\n');
}

export function parseSegmentText(text) {
  const lines = String(text || '')
    .split(/\n|;|；/)
    .map((line) => line.trim())
    .filter(Boolean);
  const segments = [];
  for (const line of lines) {
    const match = line.match(/^(\d{4}-\d{2}-\d{2})\s*[~～至]\s*(\d{4}-\d{2}-\d{2})$/);
    if (!match) throw new Error(`区间格式不对：「${line}」，应为 2026-06-01~2026-06-09`);
    segments.push({ start: match[1], end: match[2] });
  }
  if (!segments.length) throw new Error('至少登记一段可用曝光区间');
  return segments;
}

/** 登记可用曝光区间表单：多段文本 + 备注 + 校正方法。 */
export function intervalForm({ segments = [], note = '', method = 'bh', submit = '登记区间并整套发布' } = {}) {
  return `<form id="analysis-form" style="display:grid;gap:12px">
    <div class="field">
      <label for="segments">可用曝光区间（每行一段，闭区间）</label>
      <textarea name="segments" id="segments" rows="4" placeholder="2026-06-01~2026-06-09&#10;2026-06-12~2026-06-15">${esc(segmentsToText(segments))}</textarea>
      <span class="hint">埋点故障 / 暂停 / 补数期间不要写进来；区间外的样本不会被任何指标累计。</span>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
      ${field({
        label: '多重校正方法',
        name: 'method',
        value: method,
        options: [
          { value: 'bh', label: 'BH-FDR' },
          { value: 'bonferroni', label: 'Bonferroni' },
        ],
      })}
      ${field({ label: '备注（写进审计）', name: 'note', value: note, placeholder: '06-10/06-11 埋点故障，不进本次分析' })}
    </div>
    <div class="form-error" id="analysis-error"></div>
    <button class="btn accent" type="submit">${esc(submit)}</button>
    <div class="note" style="margin:0">同一组区间重跑不新增版本；区间一改，旧版本立即失效，旧结论不能再拿去做决策。</div>
  </form>`;
}

/** 版本列表：生效 / 待发布 / 已失效，点击可追溯某版本的整套指标。 */
export function versionsTable(versions, { onPick } = {}) {
  return table({
    rows: (versions || []).map((item) => ({ ...item, id: `ver-${item.id}` })),
    rowClass: (row) => (row.status === 'active' ? 'is-primary' : ''),
    columns: [
      { key: 'revision', label: '版本', className: 'mono', render: (row) => `v${esc(row.revision)}` },
      { key: 'status', label: '状态', render: (row) => analysisStatusBadge(row.status) },
      {
        key: 'interval_label',
        label: '可用曝光区间',
        render: (row) => `<span class="mono">${esc(row.interval_label)}</span><div class="mono muted">${row.interval_days} 天</div>`,
      },
      {
        key: 'srm',
        label: 'SRM',
        render: (row) =>
          row.srm && row.srm.checked_at
            ? `${badge(row.srm.alarm ? 'srm' : 'ok', row.srm.alarm ? 'SRM 告警' : '均衡')}<div class="mono muted">p=${esc(row.srm.p_value)}</div>`
            : '—',
      },
      {
        key: 'metrics_count',
        label: '指标 / 显著',
        className: 'num',
        render: (row) => `${row.metrics_count} / ${row.significant_count}`,
      },
      { key: 'suggestion', label: '建议', render: (row) => esc(row.suggestion?.verdict || '—') },
      { key: 'published_at', label: '发布', render: (row) => fmtTime(row.published_at) },
      { key: 'failure_reason', label: '失败原因', render: (row) => (row.failure_reason ? `<span class="fail">${esc(row.failure_reason)}</span>` : '—') },
    ],
    empty: { title: '还没有分析版本', hint: '登记可用曝光区间后整套发布一次。' },
  });
}

export function versionSelect({ name = 'analysis_version_id', versions = [], value = '', includeAll = false } = {}) {
  const options = (includeAll ? [{ value: '', label: '全部版本' }] : []).concat(
    versions.map((item) => ({
      value: item.id,
      label: `v${item.revision} · ${ENUMS.analysisStatus[item.status] || item.status} · ${item.interval_label}`,
    })),
  );
  return field({ label: '分析版本', name, value, options });
}

export function summaryLine(srm, analysis) {
  if (!analysis || !analysis.revision) return '';
  const power = pct(srm?.power_at_current, 1);
  return `<span class="mono muted">v${analysis.revision} · 区间 ${analysis.interval_days} 天 · ${analysis.interval_label} · 功效 ${power}</span>`;
}
