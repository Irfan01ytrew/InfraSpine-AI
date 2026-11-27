import fs from 'node:fs/promises';
import path from 'node:path';
import { Presentation } from '@oai/artifact-tool';
import { installNode } from '@oai/granola/node';

const root = path.resolve('.');
const input = path.join(root, 'presentation_output', 'InfraSpine_HMS_Roman_Urdu_Visual_Guide.pptx');
const out = path.join(root, '.hms_ppt_build', 'final_render');
await fs.mkdir(out, { recursive: true });
installNode();
const p = await Presentation.load(input);
const summaries = [];
for (const [i, slide] of p.slides.items.entries()) {
  const blob = await p.export({ slide, format: 'png', scale: 1 });
  const file = path.join(out, `slide-${String(i + 1).padStart(2, '0')}.png`);
  await fs.writeFile(file, new Uint8Array(await blob.arrayBuffer()));
  summaries.push({ slide: i + 1, noteChars: (slide.speakerNotes.text || '').length, file });
}
const montage = await p.export({ format: 'webp', montage: true, scale: 0.25 });
await fs.writeFile(path.join(out, 'montage.webp'), new Uint8Array(await montage.arrayBuffer()));
await fs.writeFile(path.join(out, 'render-summary.json'), JSON.stringify({ count: p.slides.items.length, slides: summaries }, null, 2));
console.log(JSON.stringify({ count: p.slides.items.length, notes: summaries.map(x => x.noteChars), out }, null, 2));
