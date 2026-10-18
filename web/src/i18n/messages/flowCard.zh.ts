// Simplified Chinese messages for conversation flow cards (.trellis/tasks/archive/2026-10/10-12-ai-work-panel), merged into zh.ts. Keys must match flowCard.en.ts.
import type { FlowCardKey } from './flowCard.en';

export const flowCardZh: Record<FlowCardKey, string> = {
  'fc.open': '打开工作面板',
  'fc.loading': '正在加载…',
  'fc.view': '查看',

  // Run cards
  'fc.run.kind': '运行',
  'fc.run.workflow': '流程 {name} · v{v}',
  'fc.run.workflowVersion': '流程 v{v}',
  'fc.run.started': '已开始运行：{title}',
  'fc.run.askSummary': '让 AI 总结',
  'fc.run.askSummaryHint': '把这次运行加为参考并预填问题；你发送后才会提问。',
  'fc.run.askSummaryText': '总结运行「{title}」：产出了什么、检查结果如何、还有哪些没解决的问题。',
  'fc.digest.succeeded': '{name}已完成',
  'fc.digest.waiting_human': '{name}等待处理',
  'fc.digest.running': '{name}进行中',
  'fc.digest.ready': '{name}排队中',
  'fc.digest.failed': '{name}失败',
  'fc.digest.pending': '{name}未开始',
  'fc.digest.cancelled': '{name}已取消',
  'fc.digest.skipped': '{name}已跳过',
  'fc.digest.sep': '；',
  'fc.digest.more': '另有 {n} 个步骤…',
  'fc.digest.allDone': '全部 {n} 个步骤已完成',
  'fc.digest.empty': '还没有步骤',
  'fc.outcome.succeeded': '运行已完成：完成 {done}/{total} 个步骤，产出 {results} 个结果',
  'fc.outcome.failed': '运行失败：{failed} 个步骤失败，完成 {done}/{total} 个步骤',
  'fc.outcome.cancelled': '运行已取消：完成 {done}/{total} 个步骤',
  'fc.outcome.took': ' · 用时 {d}',
  'fc.outcome.note': '运行完成不等于验收通过，以审核和任务规则为准。',

  // Blocking issues
  'fc.pending.title': '需要你处理（{n}）',
  'fc.clar.title': '{name}需要补充信息',
  'fc.clar.submit': '提交并继续',
  'fc.approval.title': '{name}等待审核',
  'fc.approval.basis': '查看依据',
  'fc.approval.approve': '通过',
  'fc.approval.sendBack': '退回',
  'fc.approval.comment': '意见',
  'fc.approval.commentHint': '通过时可不填，退回时必填',
  'fc.approval.needComment': '填写意见后才能退回',
  'fc.manual.title': '{name}需要人工录入',
  'fc.manual.body': '图纸信息需要人工录入，表单在工作面板里。',
  'fc.manual.handle': '在面板中处理',
  'fc.waiting.title': '{name}等待人工处理',
  'fc.failed.title': '{name}失败',
  'fc.failed.attempts': '第 {a}/{m} 次尝试',
  'fc.failed.retry': '重试',
  'fc.failed.noPermission': '重试需要本项目的流程权限。',

  // Artifacts
  'fc.results.title': '产物（{n}）',
  'fc.results.missing': '该版本已不存在',

  // Flow cards
  'fc.wf.kind': '流程',
  'fc.wf.nodes': '{n} 个节点',
  'fc.wf.ready': '可以运行',
  'fc.wf.problems': '运行前需处理 {n} 个问题',
  'fc.wf.problemsMore': '另有 {n} 个…',
  'fc.wf.archived': '已归档',
  'fc.wf.openPanel': '在面板中打开',
  'fc.wf.run': '运行',
  'fc.wf.starting': '正在启动…',
  'fc.wf.runBlocked': '请先在面板中处理这些问题',
  'fc.wf.note': '运行使用当前版本；之后的修改只影响之后的运行。',

  // New workflow plan
  'fc.create.pending': '新流程 · {n} 个步骤',
  'fc.create.created': '已创建 {name} v{v}',
  'fc.create.part': '零件 {part}',
  'fc.create.create': '创建流程',
  'fc.create.creating': '正在创建…',
  'fc.create.preview': '在面板中预览',
  'fc.create.open': '打开',
  'fc.create.added': '新增',
  'fc.create.note': '点击「创建流程」后才会创建，成为该零件自己的流程。',
  'fc.create.appliedNote': '{when}已创建。',

  // Edit workflow plan
  'fc.edit.viewInPanel': '在面板中查看变更',
  'fc.edit.focus': '针对节点：{name}',
  'fc.edit.fromRun': '修改的是下一次运行使用的流程（v{v}）；本次运行不变。',
  'fc.warn.run_unchanged': '本次运行不变，只有下一次运行使用这些修改。',
  'fc.warn.movedOnRun': '这次运行用的是 v{v}；方案基于当前的 v{base}。',
  'fc.warn.focus_node_missing': '选中的节点已不在当前流程里，方案按整个流程处理。',

  // Composer
  'fc.composer.unsaved': '工作面板里有未保存的修改，发送时会先保存。',
  'fc.composer.saveAndSend': '保存后发送',
};
