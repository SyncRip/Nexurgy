// Simplified Chinese messages for S5 (consistent execution policies), merged into zh.ts. Only S5 adds keys here; keys must match policy.en.ts.
import type { PolicyKey } from './policy.en';

export const policyZh: Record<PolicyKey, string> = {
  // Policy source (explicit choice > inherited source version > project default > standard)
  'pol.source.label': '策略来源',
  'pol.source.explicit': '本次请求选择',
  'pol.source.inherited': '继承自 {label}',
  'pol.source.project_default': '项目默认：Turbo',
  'pol.source.default': '默认',
  'pol.source.short.explicit': '显式选择',
  'pol.source.short.inherited': '继承',
  'pol.source.short.project_default': '项目默认',
  'pol.source.short.default': '默认',
  'pol.source.of': '{label} 用的是{policy}。',
  'pol.follow': '跟随来源',
  'pol.loading': '正在确定执行策略…',

  // Effects of an explicit switch
  'pol.cons.confirm_required': '标准模式会要求先确认上游的 {n} 个假设。',
  'pol.cons.confirm_upstream': '正常模式：上游有 {n} 个假设尚未确认，确认之前结果仍未经校验。',
  'pol.cons.corrected': '上游有 {n} 个值已被人更正：请先用更正后的值生成新版本，再用正常模式运行。',
  'pol.cons.turbo_over_standard': '来源是正常模式；选 Turbo 会跳过检查，结果标记为未经校验。',

  // Confirm these assumptions (one list, one confirmation)
  'pol.confirm.open': '确认这些假设',
  'pol.confirm.title': '确认上游假设',
  'pol.confirm.hint': '这些值由 AI 或规则采用，没有人给出过。只确认你核对过的值：确认人记为你。值不对属于工程变更（生成新版本），不在这里改。',
  'pol.confirm.counts': '待确认 {pending} · 已确认 {confirmed} · 已更正 {corrected}',
  'pol.confirm.all': '全部确认（{n}）',
  'pol.confirm.one': '确认',
  'pol.confirm.running': '确认中…',
  'pol.confirm.done': '已确认 {n} 个假设',
  'pol.confirm.none': '上游没有待确认的假设。',
  'pol.confirm.blocked': '有值已被更正，不能直接用正常模式接手：请先用更正后的值生成新版本。',
  'pol.confirm.retryHint': '确认后重试这一步：它会以正常模式继续，并把这些值当作已确认的事实。',
  'pol.confirm.state.unconfirmed': '待确认',
  'pol.confirm.state.confirmed': '已确认',
  'pol.confirm.state.corrected': '已更正',
  'pol.confirm.by': '{name} 确认',
  'pol.confirm.correctedTo': '已更正为 {value}：需新版本',
  'pol.confirm.owner.engineering_spec_version': '规格 {label}',
  'pol.confirm.owner.assembly_revision': '装配 {label}',
  'pol.confirm.owner.model_revision': '零件 {label}',
  'pol.confirm.owner.other': '{label}',

  // Assembly revision: confirm assumptions
  'pol.asm.title': '待确认的假设',
  'pol.asm.hint': '这个修订依据的、由 AI 或规则采用的值。只有全部检查通过、且每个值（它自己的、规格的、零件的）都经人确认后，才算已校验。',
  'pol.asm.pending': '尚待通过的检查：{list}',
  'pol.asm.upstream': '组成它的 {n} 个零件 / 子装配修订尚未校验。',
  'pol.asm.ofSpec': '在规格上确认',
  'pol.asm.verified': '已校验：检查全部通过，所有值都已确认。',

  // Project settings: default Turbo
  'pol.project.title': '默认 Turbo',
  'pol.project.hint':
    '新请求使用当前项目默认，包括从旧结果重新运行或重新提取。Turbo 会把缺失值记为假设，跳过工程检查和验收，结果标记为未经校验。已有运行、重试和子操作保留启动时的设置。',
  'pol.project.on': '已开启：新请求使用 Turbo',
  'pol.project.off': '已关闭：新请求使用正常模式',

  // Server codes
  'error.upstream_assumptions_unconfirmed': '这份 Turbo 规格含 AI 采用的值：需要有人先确认，才能用于正常模式。',
  'error.upstream_assumptions_corrected': '这份 Turbo 规格有值已被人更正：请在规格页回答（生成新版本），再用新版本运行。',
  'error.turbo_clarification_refused': 'Turbo 运行中有步骤要求澄清；Turbo 不会停下来等回答。',
  'error.invalid_source_type': '未知的来源类型。',
  'wf.nerr.upstream_assumptions_unconfirmed': '这份 Turbo 规格含 AI 采用的值：确认这些假设后，重试这一步即可按正常模式继续。',
  'wf.nerr.upstream_assumptions_corrected': '这份 Turbo 规格有值已被人更正：请用更正后的值生成新版本，再从新版本运行。',
  'wf.nerr.turbo_clarification_refused': 'Turbo 运行中有步骤要求澄清（协议错误）；上游采用的值是已知事实。',
};
