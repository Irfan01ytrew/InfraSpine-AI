import assert from "node:assert/strict";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import { remarkCitationLinks, safeAnswerUrl, webCitationTargets } from "../src/lib/answerMarkdown.mjs";

const render = (text) => renderToStaticMarkup(createElement(ReactMarkdown, {
  remarkPlugins: [remarkGfm, remarkCitationLinks],
  rehypePlugins: [[rehypeHighlight, { detect: false, ignoreMissing: true }]],
  urlTransform: safeAnswerUrl,
}, text));

test("fenced code keeps indentation, literal citations, HTML and code punctuation", () => {
  const html = render('```python\ndef add(a, b):\n    # [S1] is example data\n    return "<script>" + str(a + b)\n```');
  assert.match(html, /<pre><code class="hljs language-python">/);
  assert.match(html, /hljs-keyword/);
  assert.match(html, /    /);
  assert.match(html, /\[S1\]/);
  assert.doesNotMatch(html, /href=|<script>/);
});

test("streaming unfinished fences and unknown languages remain readable", () => {
  assert.match(render('```not-a-language\nanswer = 42'), /<pre><code class="(?:hljs )?language-not-a-language">answer = 42\n/);
});

test("tables, nested lists, emphasis and escaped pipes retain their structure", () => {
  const html = render('| Name | Value |\n| --- | ---: |\n| **A** | `x\\|y` |\n\n- Parent\n  - Child');
  assert.match(html, /<table>/);
  assert.match(html, /<strong>A<\/strong>/);
  assert.match(html, /text-align:right/);
  assert.match(html, /<code>x\|y<\/code>/);
  assert.equal((html.match(/<ul>/g) ?? []).length, 2);
});

test("citations become source controls only outside code and existing links", () => {
  const html = render('Claim [S1, S2; W3]. `literal [S4]` [link [S5]](https://example.com).');
  for (const tag of ["S1", "S2", "W3"]) assert.match(html, new RegExp(`href="#citation-${tag}"`));
  assert.doesNotMatch(html, /href="#citation-S[45]"/);
});

test("web citations support both standard and angle-bracket source URLs", () => {
  const sources = webCitationTargets('[W1] [Example](https://example.com/a)\n[W2] [Source](<https://example.org/b>)\n[W3] [Unsafe](https://name:secret@example.net/)');
  assert.equal(sources.get("W1").url, "https://example.com/a");
  assert.equal(sources.get("W2").url, "https://example.org/b");
  assert.equal(sources.has("W3"), false);
});

test("web citation targets keep published and retrieved timestamps, never inventing a missing date", () => {
  const sources = webCitationTargets(
    "- [W1] [Official score page](<https://scores.example.com/live>) · Published: 2026-11-21T05:15:00Z\n"
    + "- [W2] [Live results](<https://scores.example.com/results>) · Retrieved: 2026-11-21T05:16:00Z\n"
    + "- [W3] [Undated page](<https://example.com/undated>)",
  );
  assert.equal(sources.get("W1").published, "2026-11-21T05:15:00Z");
  assert.equal(sources.get("W1").retrieved, null);
  assert.equal(sources.get("W2").published, null);
  assert.equal(sources.get("W2").retrieved, "2026-11-21T05:16:00Z");
  assert.equal(sources.get("W3").published, null);
  assert.equal(sources.get("W3").retrieved, null);
});

test("unsafe destinations, tracking images and raw HTML cannot execute", () => {
  for (const url of ["javascript:alert(1)", "data:text/html,x", "//evil.test/x", "/\\evil.test", "/api/%5cevil", "https://a:b@example.com", "https://example.com\n"]) {
    assert.equal(safeAnswerUrl(url), "", url);
  }
  assert.equal(safeAnswerUrl("https://example.com/pixel.png", "src"), "");
  assert.equal(safeAnswerUrl("/api/chat-images/abc123.png", "src"), "/api/chat-images/abc123.png");
  assert.equal(safeAnswerUrl("/api/exports/download/abc.docx?format=docx"), "/api/exports/download/abc.docx?format=docx");
  const html = render('<script>alert(1)</script>\n\n[bad](javascript:alert)\n\n<img src=x onerror=alert(1)>');
  assert.doesNotMatch(html, /<script>|<img|href="javascript/);
});
