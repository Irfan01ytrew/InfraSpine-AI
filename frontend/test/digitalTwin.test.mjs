import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const twinFiles = [
  "src/digital-twin/TwinWorkspace.tsx",
  "src/digital-twin/api.ts",
  "src/digital-twin/types.ts",
];

// The twin page follows the agreed type scale: no text below 12px
// (text-xs). Anything smaller is invisible to many readers and was the
// original frontend complaint.
const SUB_12PX = [
  /text-\[10px\]/,
  /text-\[11px\]/,
  /text-\[11\.5px\]/,
  /text-2xs/,
  /text-\[9px\]/,
];

for (const file of twinFiles) {
  test(`digital twin: ${file} has no sub-12px text`, () => {
    const source = readFileSync(join(root, file), "utf8");
    for (const pattern of SUB_12PX) {
      assert.doesNotMatch(source, pattern, `${file} must not use ${pattern}`);
    }
  });
}

test("digital twin: all five scenario templates are offered", () => {
  const api = readFileSync(join(root, "src/digital-twin/api.ts"), "utf8");
  for (const id of ["key_employee_leaves", "server_down", "supplier_fails", "cyber_incident", "site_unavailable"]) {
    assert.ok(api.includes(`id: "${id}"`), `template ${id} is missing from the twin API`);
  }
  // Every template seed kind must have a graph-kind mapping, or the seed
  // dropdown would be empty for that scenario.
  for (const kind of ["person", "system", "service", "supplier", "site"]) {
    assert.ok(api.includes(`${kind}: [`), `no graph-kind mapping for twin kind ${kind}`);
  }
});

test("digital twin: workspace is mounted in the admin shell", () => {
  const workspaces = readFileSync(join(root, "src/components/ModuleWorkspaces.tsx"), "utf8");
  assert.ok(workspaces.includes('"digital-twin"'), "digital-twin missing from WorkspaceView");
  assert.ok(workspaces.includes("DigitalTwinWorkspace"), "TwinWorkspace not mounted");
  const adminConsole = readFileSync(join(root, "src/components/AdminConsole.tsx"), "utf8");
  assert.ok(adminConsole.includes('"digital-twin"'), "digital-twin missing from AdminConsole views");
  const sidebar = readFileSync(join(root, "src/components/AppSidebar.tsx"), "utf8");
  assert.ok(sidebar.includes('"digital-twin"'), "digital-twin missing from navigation");
});
