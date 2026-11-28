// Model output is untrusted. Keep remote images disabled and links on explicit schemes.
export function safeAnswerUrl(value, key = "href") {
  if (key === "src") return /^\/api\/chat-images\/[0-9a-f]+\.(?:png|jpe?g|webp)$/i.test(value) ? value : "";
  if (/^#citation-[SW]\d+$/.test(value)) return value;
  if (/[\u0000-\u0020\u007f\\]/.test(value)) return "";
  if (value.startsWith("/api/") && !/%(?:2f|5c|0[0-9a-f]|1[0-9a-f])/i.test(value)) return value;
  try {
    const url = new URL(value);
    return url.protocol === "https:" && !url.username && !url.password ? value : "";
  } catch {
    return "";
  }
}

export function webCitationTargets(text) {
  const targets = new Map();
  for (const line of text.split(/\r?\n/)) {
    const tag = line.match(/\[(W\d+)\]/)?.[1];
    if (!tag) continue;
    const matches = [...line.matchAll(/\[([^\]]+)\]\((?:<(https:\/\/[^>\s]+)>|(https:\/\/[^)\s]+))\)/g)];
    const link = matches.find((match) => match[1] !== tag) ?? matches[0];
    const url = link?.[2] ?? link?.[3];
    // Step P slice 3: keep the source's timestamps with the citation so the
    // chip can show them. A missing publication date stays null — it is
    // shown as missing, never invented.
    const published = line.match(/Published:\s*([^·\n]+)/)?.[1]?.trim() || null;
    const retrieved = line.match(/Retrieved:\s*([^·\n]+)/)?.[1]?.trim() || null;
    if (url && safeAnswerUrl(url)) targets.set(tag, { title: link[1], url, published, retrieved });
  }
  return targets;
}

// Operate on Markdown text nodes only: a citation in code, a URL or an image stays literal.
export function remarkCitationLinks() {
  return function transform(tree) {
    function walk(parent) {
      if (!parent.children || ["link", "linkReference", "image", "code", "inlineCode"].includes(parent.type)) return;
      parent.children = parent.children.flatMap((node) => {
        if (node.type !== "text") { walk(node); return [node]; }
        const parts = [];
        let offset = 0;
        for (const match of node.value.matchAll(/\[[SW]\d+(?:\s*[,;]\s*[SW]\d+)*\]/g)) {
          if (match.index > offset) parts.push({ type: "text", value: node.value.slice(offset, match.index) });
          const tags = match[0].match(/[SW]\d+/g);
          tags.forEach((tag, index) => {
            if (index) parts.push({ type: "text", value: " " });
            parts.push({ type: "link", url: `#citation-${tag}`, children: [{ type: "text", value: `[${tag}]` }] });
          });
          offset = match.index + match[0].length;
        }
        if (offset < node.value.length) parts.push({ type: "text", value: node.value.slice(offset) });
        return parts.length ? parts : [node];
      });
    }
    walk(tree);
  };
}
