import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  DEFAULT_LANGUAGE,
  LANGUAGES,
  NAV_STRINGS,
  applyLanguageToDocument,
  getLanguage,
  isRtlLanguage,
  localeForLanguage,
  normalizeLanguage,
  restoreLanguage,
  setLanguage,
  tNav,
  translateNavGroups,
} from "../src/i18n.mjs";

test("every nav string has all three languages, non-empty", () => {
  for (const [key, entry] of Object.entries(NAV_STRINGS)) {
    for (const option of LANGUAGES) {
      assert.equal(typeof entry[option.id], "string", `${key} missing ${option.id}`);
      assert.ok(entry[option.id].trim().length > 0, `${key} empty ${option.id}`);
    }
    assert.equal(entry.en, key, `${key} must be keyed by its English text`);
  }
});

test("tNav falls back to the English label for unknown labels and languages", () => {
  assert.equal(tNav("Knowledge", "roman_urdu"), "Knowledge");
  assert.equal(tNav("Knowledge", "urdu"), "علم");
  assert.equal(tNav("My work", "roman_urdu"), "Mera kaam");
  assert.equal(tNav("My work", "urdu"), "میرا کام");
  assert.equal(tNav("Something new", "urdu"), "Something new");
  assert.equal(tNav("Knowledge", "xx"), "Knowledge");
  assert.equal(tNav("Knowledge", undefined), "Knowledge");
});

test("only Urdu script is right-to-left", () => {
  assert.equal(isRtlLanguage("urdu"), true);
  assert.equal(isRtlLanguage("roman_urdu"), false);
  assert.equal(isRtlLanguage("en"), false);
  assert.equal(isRtlLanguage("xx"), false);
});

test("locale mapping keeps Latin digits for Roman Urdu", () => {
  assert.equal(localeForLanguage("en"), "en-US");
  assert.equal(localeForLanguage("roman_urdu"), "ur-PK-u-nu-latn");
  assert.equal(localeForLanguage("urdu"), "ur-PK");
  assert.equal(localeForLanguage("xx"), "en-US");
});

test("normalizeLanguage defaults unknown values to English", () => {
  assert.equal(normalizeLanguage("urdu"), "urdu");
  assert.equal(normalizeLanguage("nope"), DEFAULT_LANGUAGE);
  assert.equal(normalizeLanguage(undefined), DEFAULT_LANGUAGE);
  assert.equal(getLanguage(), DEFAULT_LANGUAGE);
});

test("translateNavGroups translates labels and preserves everything else", () => {
  const groups = [
    { id: "workspace", label: "Workspace", items: [
      { view: "knowledge", label: "Knowledge", icon: "book", keywords: "chat ask" },
      { view: "mystery", label: "Not yet translated", icon: "box" },
    ] },
    { id: "ops", label: "Operations", items: [] },
  ];
  const translated = translateNavGroups(groups, "urdu");
  assert.equal(translated[0].label, "ورک اسپیس");
  assert.equal(translated[0].items[0].label, "علم");
  assert.equal(translated[0].items[0].icon, "book");
  assert.equal(translated[0].items[0].keywords, "chat ask");
  assert.equal(translated[0].items[1].label, "Not yet translated");
  assert.equal(translated[1].label, "آپریشنز");
  // The input is not mutated.
  assert.equal(groups[0].label, "Workspace");
  assert.deepEqual(translateNavGroups(null, "urdu"), []);
});

test("applyLanguageToDocument is a no-op without a document", () => {
  assert.equal(typeof document, "undefined");
  applyLanguageToDocument("urdu");
  setLanguage("urdu");
  assert.equal(getLanguage(), "urdu");
  // restoreLanguage (the sign-in load path) also works without a document
  // and normalizes unknown values.
  restoreLanguage("roman_urdu");
  assert.equal(getLanguage(), "roman_urdu");
  restoreLanguage("xx");
  assert.equal(getLanguage(), DEFAULT_LANGUAGE);
  setLanguage("en");
  assert.equal(getLanguage(), "en");
});

test("every label in buildNav is covered by the translation table", () => {
  const sidebar = readFileSync(new URL("../src/components/AppSidebar.tsx", import.meta.url), "utf8");
  const start = sidebar.indexOf("export function buildNav");
  const end = sidebar.indexOf("return groups;", start);
  assert.ok(start >= 0 && end > start, "buildNav not found");
  const body = sidebar.slice(start, end);
  const labels = [...body.matchAll(/label:\s*"([^"]+)"/g)].map((m) => m[1]);
  assert.ok(labels.length > 40, `expected many nav labels, found ${labels.length}`);
  const missing = [...new Set(labels)].filter((label) => !(label in NAV_STRINGS));
  assert.deepEqual(missing, []);
});
