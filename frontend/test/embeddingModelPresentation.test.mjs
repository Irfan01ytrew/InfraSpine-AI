import test from "node:test";
import assert from "node:assert/strict";
import { availabilityLabel, loadLabel, ocrTestLabel, parserAvailabilityLabel, parserTestLabel } from "../src/embeddingModelPresentation.js";

test("embedding model status labels distinguish cached, loaded, and unavailable states", () => {
  const available = {
    model_name: "sentence-transformers/all-MiniLM-L6-v2",
    dimensions: 384,
    device: "cpu",
    available_locally: true,
    loaded: false,
    status: "not_loaded",
  };

  assert.equal(availabilityLabel(available), "Available locally");
  assert.equal(loadLabel(available), "Not loaded");
  assert.equal(loadLabel({ ...available, loaded: true }), "Loaded in memory");
  assert.equal(availabilityLabel({ ...available, available_locally: false }), "Unavailable locally");
});

test("document parser labels expose availability and the parser used by health tests", () => {
  assert.equal(parserAvailabilityLabel(true), "Available");
  assert.equal(parserAvailabilityLabel(false), "Unavailable");
  assert.equal(parserTestLabel({ ok: true, parser_used: "Docling" }), "Docling passed");
  assert.equal(parserTestLabel({ ok: false, parser_used: "Built-in PDF parser" }), "Built-in PDF parser failed");
});

test("OCR health labels distinguish successful recognition from a safe failure", () => {
  assert.equal(ocrTestLabel({ ok: true }), "OCR health test passed");
  assert.equal(ocrTestLabel({ ok: false }), "OCR health test failed");
});
