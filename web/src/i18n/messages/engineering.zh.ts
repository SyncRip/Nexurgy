// Simplified Chinese status labels shared by the general mechanical engineering work packages, merged into zh.ts. Keys must match engineering.en.ts.
import type { EngineeringKey } from './engineering.en';

export const engineeringZh: Record<EngineeringKey, string> = {
  'me.policy.standard': '正常',
  'me.policy.standardHint': '照常执行工程检查、复核、审批和验收。',
  'me.policy.turbo': 'Turbo',
  'me.policy.turboHint': '直接运行到交付：缺失或冲突的值自动假设并记录；跳过工程检查、复核、审批和验收。',
  'me.unverified': '完成 · 未经校验',
  'me.unverified.hint': '结果已生成，但尚未完成全部校验或复核。请查看这个确切版本的检查结果，并复核未确认的设计取值。',
  'me.check.not_requested': '未检查',
  'me.check.skipped_turbo': '已跳过（Turbo）',
  'me.check.pending': '检查中',
  'me.check.passed': '通过',
  'me.check.failed': '检查失败',
  'me.check.unknown': '未知',
  'me.accept.not_requested': '无需验收',
  'me.accept.pending': '待验收',
  'me.accept.accepted': '已验收',
  'me.accept.rejected': '已驳回',
  'me.stale': '已过期',
  'me.stale.hint': '基于旧版本生成，需要按当前版本重新生成。旧版本仍可查看。',
  'me.rep.nominal': '名义表示',
  'me.rep.nominal.hint': '名义形状：外廓和接口正确，未建模制造细节。',
  'me.rep.simplified': '简化表示',
  'me.rep.simplified.hint': '简化表示（如未建真实螺纹）；真实零件以规格为准。',
  'me.soon.title': '开发中',
  'me.soon.body': '这个工作区属于通用机械工程这一轮，尚未接通。',
};
