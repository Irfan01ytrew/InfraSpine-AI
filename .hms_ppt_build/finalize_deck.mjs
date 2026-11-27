import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const WORKSPACE = path.dirname(HERE);
const SKILL_DIR = 'C:\\Users\\10\\.codex\\plugins\\cache\\openai-primary-runtime\\presentations\\26.1112.11217\\skills\\presentations';
const { finalizePresentation, resolvePresentationFont } = await import(pathToFileURL(path.join(SKILL_DIR, 'container_tools', 'artifact_tool_utils.mjs')).href);
const finalPath = path.join(WORKSPACE, 'presentation_output', 'InfraSpine_HMS_Roman_Urdu_Visual_Guide.pptx');
const result = await finalizePresentation({
  explicitTotalSlideCount: 25,
  workspaceDir: WORKSPACE,
  candidatePath: path.join(HERE, 'candidate.pptx'),
  finalPath,
  pythonExecutable: 'C:\\Program Files\\WindowsApps\\OpenAI.CodexPrimaryRuntime.v26-1112-737-0_26.1112.737.0_x64__3k8sg7r9htsxt\\dependencies\\python\\python.exe',
  integrityValidatorPath: path.join(SKILL_DIR, 'container_tools', 'inspect_presentation_package_integrity.py'),
  layoutValidatorPath: path.join(SKILL_DIR, 'container_tools', 'inspect_presentation_layout_geometry.py'),
  layoutArgs: ['--expected-slide-size-emu', '12192000,6858000', '--validate-bullet-geometry', '--validate-heading-fit'],
  fontPolicy: { basis: 'design', families: [resolvePresentationFont({ fontFamily: 'Arial' })] },
  verifyArtifactToolImport: true,
  receiptPath: path.join(HERE, 'validation', 'validation-canonical.json'),
});
console.log(JSON.stringify({ finalPath, result }, null, 2));
