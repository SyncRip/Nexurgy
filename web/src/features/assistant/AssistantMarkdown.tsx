import Markdown, { type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import './assistantMarkdown.css';

const plugins = [remarkGfm];

/** Answers are saved text, not trusted HTML or instructions to load external resources. */
function answerUrl(url: string): string | undefined {
  const value = url.trim();
  if (!value || /[\u0000-\u0020\u007f\\]/.test(value) || value.startsWith('//')) return undefined;
  const scheme = /^[a-z][a-z\d+.-]*:/i.exec(value);
  return scheme && !/^(https?:|mailto:)$/i.test(scheme[0]) ? undefined : value;
}

const components: Components = {
  a: ({ href, title, children }) => {
    if (!href) return <span>{children}</span>;
    const external = /^https?:/i.test(href);
    return <a href={href} title={title} target={external ? '_blank' : undefined} rel={external ? 'noopener noreferrer' : undefined}>{children}</a>;
  },
  // Saved result cards own authenticated model/drawing previews. Markdown may describe an image, never load it.
  img: ({ alt, title }) => <span className="as-markdown-image">{alt || title}</span>,
  pre: ({ children }) => <pre tabIndex={0}>{children}</pre>,
  table: ({ children }) => <div className="as-markdown-table" tabIndex={0}><table>{children}</table></div>,
};

export function AssistantMarkdown({ text }: { text: string }) {
  return <div className="as-msg-text as-markdown">
    <Markdown remarkPlugins={plugins} components={components} urlTransform={answerUrl}>{text}</Markdown>
  </div>;
}
