import { marked } from 'marked';

marked.setOptions({
  gfm: true,
  breaks: false,
});

export function renderMarkdown(md: string): string {
  const html = marked.parse(md || '') as string;
  // Wrap tables in responsive scroll containers
  return html
    .replace(/<table>/g, '<div class="table-wrap"><table>')
    .replace(/<\/table>/g, '</table></div>');
}

export function renderInline(md: string): string {
  return (marked.parseInline(md || '') as string) || '';
}
