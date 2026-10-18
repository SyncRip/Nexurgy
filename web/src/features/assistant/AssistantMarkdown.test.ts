import { createElement as h } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { AssistantMarkdown } from './AssistantMarkdown';

const render = (text: string) => renderToStaticMarkup(h(AssistantMarkdown, { text }));

describe('assistant answer Markdown', () => {
  it('renders the saved Chinese delivery hierarchy without losing dimensions, versions or punctuation', () => {
    const html = render(`已为您完成简易四轮小车的全流程设计与建模装配，相关总成、零件、图纸及交付包已就绪：

1. **总成与子结构**：
   - **四轮简易小车总成（SP9-V1 A1）**：包含底板及前后两套轮轴子总成。
   - **轮轴总成（SP9-V1-D-02 A1）**：由车轴及双侧行走车轮组装而成。

2. **零件清单**：
   - **车架底板（SP9-V1-D-01 R1）**：160×80×10 mm，预留两对 Ø8 通孔，材质为 6061-T6 铝合金。
   - **车轴（SP9-V1-D-03 R1）**：Ø8×120 mm 光轴，材质为 304 不锈钢，共 2 件。
   - **行走车轮（SP9-V1-D-04 R1）**：外径 Ø50 mm、厚度 12 mm，内孔 Ø8 mm，POM 耐磨材质，共 4 件。

3. **图纸与交付包**：
   - 已生成总装与各零部件的工程图纸（DWG）。
   - 交付清单 DL-8 已打包完毕，包含全部 AP203 协议 STEP 模型与图纸文件。`);
    expect(html).not.toContain('**');
    expect(html.match(/<ol>/g)).toHaveLength(1);
    expect(html.match(/<ul>/g)).toHaveLength(3);
    expect(html.match(/<li>/g)).toHaveLength(10);
    expect(html.match(/<strong>/g)).toHaveLength(8);
    expect(html).toContain('<strong>总成与子结构</strong>：');
    expect(html).toContain('<strong>四轮简易小车总成（SP9-V1 A1）</strong>：包含底板及前后两套轮轴子总成。');
    expect(html).toContain('<strong>车架底板（SP9-V1-D-01 R1）</strong>：160×80×10 mm，预留两对 Ø8 通孔，材质为 6061-T6 铝合金。');
    expect(html).toContain('Ø8×120 mm 光轴，材质为 304 不锈钢，共 2 件。');
    expect(html).toContain('外径 Ø50 mm、厚度 12 mm，内孔 Ø8 mm，POM 耐磨材质，共 4 件。');
    expect(html).toContain('交付清单 DL-8 已打包完毕，包含全部 AP203 协议 STEP 模型与图纸文件。');
  });

  it('retains plain paragraphs, literal line breaks, escaped emphasis and unfinished Markdown', () => {
    const html = render('第一行\n第二行\n\n\\*\\*保留符号\\*\\*，还有 **未结束');
    expect(html).toContain('<p>第一行\n第二行</p>');
    expect(html).toContain('<p>**保留符号**，还有 **未结束</p>');
    expect(html).not.toContain('<strong>');
  });

  it('keeps code literal, including HTML and Markdown punctuation', () => {
    const html = render('`**literal**`\n\n```html\n<img src="https://example.com/private" onerror="alert(1)">\n**not bold**\n```');
    expect(html).toContain('<code>**literal**</code>');
    expect(html).toContain('<pre tabindex="0"><code class="language-html">');
    expect(html).toContain('&lt;img src=&quot;https://example.com/private&quot; onerror=&quot;alert(1)&quot;&gt;');
    expect(html).toContain('**not bold**');
    expect(html).not.toContain('<img');
    expect(html).not.toContain('<strong>');
  });

  it('renders headings, blockquotes, GFM tables and read-only task lists with scrollable wide blocks', () => {
    const wide = 'dimension_'.repeat(80);
    const html = render(`# 交付资料\n\n### 校验状态\n\n> Turbo 草稿，尚未经工程审核。\n\n| 零件 | 说明 |\n| --- | --- |\n| 底板 | ${wide} |\n\n- [x] 保存模型\n- [ ] 工程审核\n\n\`\`\`text\n${wide}\n\`\`\``);
    expect(html).toContain('<h1>交付资料</h1>');
    expect(html).toContain('<h3>校验状态</h3>');
    expect(html).toContain('<blockquote>');
    expect(html).toContain('Turbo 草稿，尚未经工程审核。');
    expect(html).toContain('<div class="as-markdown-table" tabindex="0"><table>');
    expect(html).toContain(`<td>${wide}</td>`);
    expect(html).toContain('<pre tabindex="0"><code class="language-text">');
    expect(html.match(/type="checkbox" disabled=""/g)).toHaveLength(2);
  });

  it('preserves raw HTML as inert text, never as executable elements', () => {
    const html = render('<script>alert("x")</script>\n\n<img src="https://example.com/track" onerror="alert(1)">\n\n<iframe src="https://example.com"></iframe>');
    expect(html).toContain('&lt;script&gt;');
    expect(html).toContain('&lt;img');
    expect(html).not.toMatch(/<(script|img|iframe)\b/);
    expect(html).not.toContain('<link');
  });

  it.each(['javascript:alert%281%29', 'JaVaScRiPt:alert%281%29', 'vbscript:msgbox%281%29',
    'data:text/html;base64,PHNjcmlwdD4=', 'file:///etc/passwd', '//example.com/path', 'javascript&#58;alert%281%29'])
  ('keeps the label but removes the unsafe link target %s', (url) => {
    const html = render(`[阅读资料](${url})`);
    expect(html).toContain('阅读资料');
    expect(html).not.toContain('href=');
    expect(html).not.toContain('<a');
  });

  it('preserves safe external, email and exact-version app links', () => {
    const html = render('[资料](https://example.com/spec?q=1#details) [本地](http://127.0.0.1:5192/#/parts/p?rev=r) [版本](#/parts/p?rev=r) [文件](/files/a?version=1) [邮箱](mailto:engineer@example.com)');
    expect(html).toContain('href="https://example.com/spec?q=1#details" target="_blank" rel="noopener noreferrer"');
    expect(html).toContain('href="http://127.0.0.1:5192/#/parts/p?rev=r"');
    expect(html).toContain('<a href="#/parts/p?rev=r">版本</a>');
    expect(html).toContain('<a href="/files/a?version=1">文件</a>');
    expect(html).toContain('<a href="mailto:engineer@example.com">邮箱</a>');
  });

  it('shows image descriptions without loading remote, relative or inline image sources', () => {
    const html = render('![总装预览](https://example.com/private.png) ![图纸](/private.svg) ![内嵌图](data:image/svg+xml;base64,PHN2Zz4=)');
    for (const alt of ['总装预览', '图纸', '内嵌图']) expect(html).toContain(alt);
    expect(html).not.toMatch(/<(img|link)\b/);
    expect(html).not.toContain('src=');
    expect(html).not.toContain('https://example.com');
  });
});
