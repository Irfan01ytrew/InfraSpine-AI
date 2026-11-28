import { Children, isValidElement, useMemo, type ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import { remarkCitationLinks, safeAnswerUrl, webCitationTargets } from "../lib/answerMarkdown.mjs";
import { CopyButton } from "./CopyButton";

function plainText(children: ReactNode): string {
  return Children.toArray(children).map((child): string => {
    if (typeof child === "string" || typeof child === "number") return String(child);
    return isValidElement<{ children?: ReactNode }>(child) ? plainText(child.props.children) : "";
  }).join("");
}

function CodeBlock({ children }: { children?: ReactNode }) {
  const element = Children.toArray(children).find((child) => isValidElement(child));
  const className = isValidElement<{ className?: string }>(element) ? element.props.className : "";
  const language = className?.match(/language-([\w+-]+)/)?.[1] ?? "text";
  return (
    <figure className="answer-code overflow-hidden rounded-xl border border-border bg-surface-2" dir="ltr">
      <figcaption className="flex items-center justify-between border-b border-border px-3 py-1 text-xs text-muted">
        <span className="font-mono">{language}</span><CopyButton text={plainText(children)} label="Copy code" />
      </figcaption>
      <pre tabIndex={0} aria-label={`${language} code`} className="scrollbar-thin max-w-full overflow-x-auto p-4 text-[12.5px] leading-relaxed">{children}</pre>
    </figure>
  );
}

/** CommonMark + GFM, rendered as React nodes. Never execute raw HTML or model-provided code. */
export function Markdown({ text, renderCitation }: { text: string; renderCitation?: (tag: string, key: string) => ReactNode }) {
  const webSources = useMemo(() => webCitationTargets(text), [text]);
  return (
    <div className="markdown min-w-0 break-words" dir="auto">
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkCitationLinks]}
        rehypePlugins={[[rehypeHighlight, { detect: false, ignoreMissing: true }]]}
        urlTransform={safeAnswerUrl}
        components={{
          pre: ({ children }) => <CodeBlock>{children}</CodeBlock>,
          table: ({ children }) => <div className="scrollbar-thin max-w-full overflow-x-auto rounded-lg border border-border" tabIndex={0} role="region" aria-label="Answer table"><table>{children}</table></div>,
          a: ({ href, children }) => {
            const tag = href?.match(/^#citation-([SW]\d+)$/)?.[1];
            if (tag) {
              if (tag.startsWith("S")) return renderCitation?.(tag, tag) ?? <span className="source-tag">[{tag}]</span>;
              const source = webSources.get(tag);
              if (source) {
                // Step P slice 3: the live-source chip shows when the source
                // was published and when it was retrieved. A missing
                // publication date is shown as missing, never invented.
                const stamp = [
                  `Published: ${source.published ?? "not available"}`,
                  ...(source.retrieved ? [`Retrieved: ${source.retrieved}`] : []),
                ].join(" · ");
                return <a href={source.url} target="_blank" rel="noopener noreferrer" title={`${source.title} — ${stamp}`} className="answer-web-citation">{tag.slice(1)}</a>;
              }
              return <span className="source-tag">[{tag}]</span>;
            }
            if (!href) return <span>{children}</span>;
            const external = href.startsWith("https:");
            const download = /^\/api\/(?:exports\/download|chat-images)\//.test(href) && /[?&](?:download=true|format=)/.test(href);
            return <a href={href} target={external ? "_blank" : undefined} rel={external ? "noopener noreferrer" : undefined} download={download || undefined}>{children}</a>;
          },
          img: ({ src, alt }) => src ? <img src={src} alt={alt || "Chat image"} loading="lazy" decoding="async" referrerPolicy="no-referrer" className="my-2 max-h-[640px] max-w-full rounded-xl border border-border object-contain" /> : <span className="text-muted">{alt || "External image omitted"}</span>,
        }}
      >{text}</ReactMarkdown>
    </div>
  );
}
