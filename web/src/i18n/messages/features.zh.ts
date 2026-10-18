import type { featuresEn } from './features.en';
export const featuresZh: Record<keyof typeof featuresEn, string> = {
  'features.homePlaceholder': '一句话描述你的目标，AI 帮你规划，需要你选择时会给出选项…',
  'features.homeHint': '从需求描述、图纸或已有工程成果开始。',
  'as.block.task_frozen': '任务工作已停用，请在高级选项中解除任务绑定后再发送新请求。',
  'as.block.feature_disabled': '此操作已停用，请选择其他操作。',
  'features.collaboration': '协作',
  'features.tasks': '任务',
  'features.stages': '项目阶段',
  'features.process_modules': '工艺模块',
  'features.template_library': '公共模板库',
  'features.turbo_choice': '执行策略选择',
  'features.model_choice': '模型选择',
  'features.disabled': '{feature}已停用',
  'features.disabledBody': '现有记录仍可通过详情链接查看，此功能暂不支持开展新工作。',
  'features.historyOnly': '历史记录：已停止新修改，符合条件的在途工作仍可继续完成。',
  'error.feature_disabled': '此功能已停用，现有记录仍可查看。',
};
