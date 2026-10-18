// Simplified Chinese messages for the resources and knowledge redesign (resExperience): the experience view (all-project list and shared global empty state), and the target project and usage mode for referencing or adopting experience.
// Keys must match resExperience.en.ts.
import type { ResExperienceKey } from './resExperience.en';

export const resExperienceZh: Record<ResExperienceKey, string> = {
  'rx.all.noProjects': '你还没有可访问的项目。',
  'rx.all.failedAll': '所有项目的经验都加载失败',
  'rx.all.empty.title': '还没有归档的经验',
  'rx.all.empty.body': '工作流完成且所有节点通过验收后，会归档到各自的项目，并在这里标明来源项目。',

  'rx.load.progress': '正在加载各项目的经验：{done}/{total}',
  'rx.load.partial': '已加载 {loaded}/{total} 个项目的经验，其余项目加载失败。',
  'rx.load.failed_one': '{count} 个项目的经验加载失败',
  'rx.load.failed_other': '{count} 个项目的经验加载失败',
  'rx.load.retryProject': '重试 {project}',
  'rx.load.truncated': '{project}：只显示最近 {shown} 个（共 {total} 个），可用搜索缩小范围。',

  'rx.detail.project': '来源项目',

  'rx.adopt.title': '参考或采用经验',
  'rx.adopt.source': '来源项目',
  'rx.adopt.targetProject': '目标项目',
  'rx.adopt.targetProjectHint': '只列出你有工作流权限的项目，默认是经验的来源项目。',
  'rx.adopt.targetProjectPlaceholder': '选择目标项目…',
  'rx.adopt.sourceMark': '来源项目',
  'rx.adopt.errProject': '请选择目标项目。',
  'rx.adopt.noTasks': '这个项目没有未完成的任务。',
  'rx.adopt.noTargets': '你在可访问的项目中都没有工作流权限，不能参考或采用经验。',
  'rx.adopt.node': '工作流节点',
  'rx.adopt.usage': '使用方式',
  'rx.adopt.usageReference': '参考经验',
  'rx.adopt.usageReferenceHint': '只登记参考关系，不改变目标任务的输入。',
  'rx.adopt.usageBaseline': '采用为任务输入',
  'rx.adopt.usageBaselineHint': '把这个固定版本填入目标任务的输入槽位，按目标任务的采用模式确认后生效。',
  'rx.adopt.noInherit': '目标任务按自己的流程重新验收，不继承原任务的验收或审核。',
  'rx.adopt.noInheritCross': '经验来自“{source}”，用于“{target}”：目标任务按自己的流程重新验收，不继承来源项目的验收或审核。',
  'rx.adopt.submitReference': '登记参考',
  'rx.adopt.into': '目标项目：{project}',
  'rx.adopt.referencedToast': '已在“{project}”登记参考 {label}。',
  'rx.adopt.proposedToast': '已在“{project}”提出采用 {label} 的申请，等待{who}确认。',
  'rx.adopt.adoptedToast': '已在“{project}”采用 {label}，目标任务仍须单独验收。',
};
