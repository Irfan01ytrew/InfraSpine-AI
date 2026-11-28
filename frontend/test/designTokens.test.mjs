import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const styles = readFileSync(join(root, "src", "styles.css"), "utf8");
const indexCss = readFileSync(join(root, "src", "index.css"), "utf8");
const companySetup = readFileSync(join(root, "src", "components", "CompanySetupPage.tsx"), "utf8");

test("design tokens: every radius used by the legacy stylesheet is defined", () => {
  for (const token of ["--radius-sm", "--radius-md", "--radius-lg", "--radius-xl"]) {
    assert.match(styles, new RegExp(`--${token.slice(2)}\\s*:`), `${token} must be defined in styles.css`);
  }
  // The one radius scale is shared: Tailwind utilities use the same sizes.
  for (const size of ["--radius-sm: 8px", "--radius-md: 12px", "--radius-lg: 16px", "--radius-xl: 20px"]) {
    assert.ok(indexCss.includes(size), `index.css must map ${size}`);
  }
});

test("design tokens: brand surfaces derive from --primary, never a hardcoded blue", () => {
  // The old blue-teal gradients (#3f7893 / #254e67 / #142b3a) clashed with the
  // green brand and made the login screen look like a different product.
  for (const hex of ["#3f7893", "#254e67", "#142b3a", "#1d4052", "#245569"]) {
    assert.ok(!styles.includes(hex), `styles.css must not contain hardcoded ${hex}`);
  }
  assert.match(styles, /\.brand-mark \{[^}]*var\(--primary\)/s, "brand mark must derive from --primary");
  assert.match(styles, /\.login-hero \{[^}]*var\(--primary\)/s, "login hero must derive from --primary");
});

test("design tokens: default brand colour is the green primary everywhere", () => {
  assert.ok(styles.includes("--primary: var(--brand, #126b4e)"), "default brand must be #126b4e");
  assert.ok(companySetup.includes('"#126b4e"'), "CompanySetup default brand must be #126b4e");
  assert.ok(!companySetup.includes("#146B61"), "stale teal default #146B61 must be gone");
});

test("design tokens: base type is readable (14px, not the old 13px)", () => {
  assert.match(styles, /font:\s*14px\/1\.55 var\(--font\)/, "body font must be 14px/1.55");
});
