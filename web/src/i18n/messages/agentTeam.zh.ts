import type { agentTeamEn } from './agentTeam.en';

export const agentTeamZh: Record<keyof typeof agentTeamEn, string> = {
  'team.entry': 'AI 项目经理',
  'team.title': 'AI 员工 · 查看分工',
  'team.note': '这些员工由 AI 担任。进度与成果以实际工作记录为准，审批仍由你完成。',
  'team.role.manager': '项目经理',
  'team.role.manager.description': '理解你的目标，协调各项工程工作，汇总进度与交付成果。',
  'team.role.design': '需求设计工程师',
  'team.role.design.description': '整理需求与来源依据，规划概念方案，帮助定义设计参数。',
  'team.role.modeling': '3D 建模工程师',
  'team.role.modeling.description': '创建和修改零件几何，生成模型版本与预览。',
  'team.role.assembly': '装配工程师',
  'team.role.assembly.description': '组织零件与子装配体，处理实例、位置及装配关系。',
  'team.role.drawing': '工程制图工程师',
  'team.role.drawing.description': '解读图纸，生成工程图，规划二维图纸的修改。',
  'team.role.process': '工艺拆解工程师',
  'team.role.process.description': '依据已保存的工程输入，拆解制造路线与加工工序。',
  'team.role.quote': '报价工程师',
  'team.role.quote.description': '依据工艺计划与价格规则生成估算报价草稿，保留未知费用。',
};
