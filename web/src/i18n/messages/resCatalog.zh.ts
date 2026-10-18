// Simplified Chinese messages for the resources and knowledge catalogue (business types, creation methods, online viewing and status, shared by lists and details). Keys must match resCatalog.en.ts.
import type { ResCatalogKey } from './resCatalog.en';

export const resCatalogZh: Record<ResCatalogKey, string> = {
  'rk.type.model': '三维模型',
  'rk.type.drawing': '图纸',
  'rk.type.spec': '工程规格',
  'rk.type.process_plan': '工艺方案',
  'rk.type.quote': '报价',
  'rk.type.document': '资料文档',
  'rk.type.process_module': '工艺模块',
  'rk.type.intake': '图纸整理结果',
  'rk.type.delivery': '交付包',

  'rk.creation.upload': '上传文件',
  'rk.creation.manual': '手动创建',
  'rk.creation.task': '任务产出',
  'rk.creation.unknown': '待识别',

  'rk.preview.preview': '可预览',
  'rk.preview.page': '在专业页面查看',
  'rk.preview.convert': '需转换',
  'rk.preview.download': '仅下载',
  'rk.preview.failed': '转换失败',
  'rk.preview.none': '暂无内容',

  'rk.status.deprecated': '当前版本已停用',
  'rk.status.upstream_newer': '上游存在新版',
  'rk.status.published': '已发布',
  'rk.status.failed': '生成失败',

  'rk.subtype.file': '文件（未登记目录）',
  'rk.subtype.engineering_spec': '工程规格',
};
