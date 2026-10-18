// Simplified Chinese messages for S6 (status and versions), merged into zh.ts. Keys must match versions.en.ts.
import type { VersionsKey } from './versions.en';

export const versionsZh: Record<VersionsKey, string> = {
  'ver.history.title': '这是历史版本',
  'ver.history.body': '链接打开的是它指向的确切版本，内容保持原样。当前版本是 {current}。',
  'ver.history.bodyNoCurrent': '链接打开的是它指向的确切版本，内容保持原样。',
  'ver.history.open': '打开当前版本',
  'ver.missing': '链接指向的版本在这里找不到。',
  'ver.stat.failed': '失败',
  'ver.node.checkFailed': '检查失败：产物已生成，但没有通过检查。请查看产物的检查结果，修改后重新运行。',
  'ver.delivery.failedSources': '检查失败：{list}',
  'as.runStatus.partial': '失败（有部分产出）',
  'collab.phase.check_failed': '检查失败',
};
