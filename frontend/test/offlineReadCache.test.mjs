import assert from "node:assert/strict";
import test from "node:test";

import {
  OFFLINE_READ_CACHE_KEY,
  OFFLINE_READ_CACHE_MAX_AGE_MS,
  OFFLINE_READ_CACHE_MAX_ANSWERS,
  clearReadCache,
  createSavedAnswer,
  isSnapshotWorthKeeping,
  loadDashboardSnapshot,
  loadKnowledgeAnswers,
  recordDashboardSnapshot,
  recordKnowledgeAnswer,
  staleLabel,
} from "../src/lib/offlineReadCache.mjs";

function memoryStorage() {
  const data = new Map();
  return {
    getItem: (key) => (data.has(key) ? data.get(key) : null),
    setItem: (key, value) => data.set(key, value),
    removeItem: (key) => data.delete(key),
  };
}

const NOW = 1_800_000_000_000;

function answerInput(question = "What is the leave policy?") {
  return {
    question,
    answer: "Annual leave is 20 days.",
    citations: [{ filename: "handbook.pdf", page_number: 3, snippet: "should be dropped", score: 0.9 }],
    model: "deepseek-flash",
    provider: "deepseek",
  };
}

function dashboardData(overrides = {}) {
  return {
    aiActive: true,
    aiLabel: "DeepSeek · deepseek-flash",
    departments: 2,
    people: 5,
    pendingInvites: 1,
    readyDocs: 10,
    agents: 1,
    processingDocs: 0,
    failedDocs: 0,
    usage: { summary: { questions: 4, errors: 0 }, daily: [] },
    openRequests: 1,
    reviewQueue: 2,
    activity: [],
    ...overrides,
  };
}

test("record + load round-trips a saved answer with sanitized citations", () => {
  const storage = memoryStorage();
  const saved = recordKnowledgeAnswer(storage, answerInput(), NOW);
  assert.equal(saved?.length, 1);
  assert.equal(saved[0].question, "What is the leave policy?");
  assert.equal(saved[0].answer, "Annual leave is 20 days.");
  // Snippets and scores are dropped; only filename + page are kept.
  assert.deepEqual(saved[0].citations, [{ filename: "handbook.pdf", page_number: 3 }]);
  assert.equal(loadKnowledgeAnswers(storage, NOW).length, 1);
  assert.equal(JSON.parse(storage.getItem(OFFLINE_READ_CACHE_KEY)).answers.length, 1);
});

test("empty question or answer is never recorded", () => {
  const storage = memoryStorage();
  assert.equal(recordKnowledgeAnswer(storage, { question: "  ", answer: "x" }, NOW), null);
  assert.equal(recordKnowledgeAnswer(storage, { question: "q", answer: "   " }, NOW), null);
  assert.equal(loadKnowledgeAnswers(storage, NOW).length, 0);
});

test("same question asked again replaces the older copy, newest first", () => {
  const storage = memoryStorage();
  recordKnowledgeAnswer(storage, answerInput(), NOW);
  recordKnowledgeAnswer(
    storage,
    { ...answerInput(), answer: "Annual leave is 21 days now." },
    NOW + 60_000,
  );
  const loaded = loadKnowledgeAnswers(storage, NOW + 60_000);
  assert.equal(loaded.length, 1);
  assert.equal(loaded[0].answer, "Annual leave is 21 days now.");
  // Case/whitespace differences are the same question.
  recordKnowledgeAnswer(
    storage,
    { ...answerInput("  WHAT is the leave policy? "), answer: "Third version." },
    NOW + 120_000,
  );
  assert.equal(loadKnowledgeAnswers(storage, NOW + 120_000).length, 1);
});

test("answers are capped at 20 and pruned after 7 days", () => {
  const storage = memoryStorage();
  for (let i = 0; i < OFFLINE_READ_CACHE_MAX_ANSWERS + 5; i++) {
    recordKnowledgeAnswer(storage, answerInput(`Question ${i}`), NOW + i * 1000);
  }
  const loaded = loadKnowledgeAnswers(storage, NOW + 30_000);
  assert.equal(loaded.length, OFFLINE_READ_CACHE_MAX_ANSWERS);
  assert.equal(loaded[0].question, "Question 24");
  const aged = loadKnowledgeAnswers(storage, NOW + OFFLINE_READ_CACHE_MAX_AGE_MS + 30_000);
  assert.equal(aged.length, 0);
});

test("long answers are truncated, not dropped", () => {
  const long = "x".repeat(25_000);
  const entry = createSavedAnswer({ question: "long", answer: long }, NOW);
  assert.ok(entry.answer.length < long.length);
  assert.match(entry.answer, /truncated/);
});

test("corrupt or wrong-shaped storage loads as empty, never throws", () => {
  const storage = memoryStorage();
  storage.setItem(OFFLINE_READ_CACHE_KEY, "{not json");
  assert.deepEqual(loadKnowledgeAnswers(storage, NOW), []);
  assert.equal(loadDashboardSnapshot(storage, NOW), null);
  storage.setItem(
    OFFLINE_READ_CACHE_KEY,
    JSON.stringify({ answers: [{ question: "no id" }], snapshot: { savedAt: NOW, data: { nope: 1 } } }),
  );
  assert.deepEqual(loadKnowledgeAnswers(storage, NOW), []);
  assert.equal(loadDashboardSnapshot(storage, NOW), null);
});

test("staleLabel mirrors the app's relative-time thresholds", () => {
  assert.equal(staleLabel(NOW, NOW), "Saved just now");
  assert.equal(staleLabel(NOW - 44_000, NOW), "Saved just now");
  assert.equal(staleLabel(NOW - 90_000, NOW), "Saved 2 minutes ago");
  assert.equal(staleLabel(NOW - 60_000, NOW), "Saved 1 minute ago");
  assert.equal(staleLabel(NOW - 2 * 3600_000, NOW), "Saved 2 hours ago");
  assert.equal(staleLabel(NOW - 3 * 86400_000, NOW), "Saved 3 days ago");
  // A clock-skewed future timestamp reads as just now, never negative.
  assert.equal(staleLabel(NOW + 60_000, NOW), "Saved just now");
});

test("an all-empty dashboard load is not worth keeping; a partial one is", () => {
  const empty = dashboardData({
    aiActive: false,
    aiLabel: "Not connected",
    departments: 0,
    people: 0,
    readyDocs: 0,
    usage: null,
    activity: null,
  });
  assert.equal(isSnapshotWorthKeeping(empty), false);
  assert.equal(recordDashboardSnapshot(memoryStorage(), empty, NOW), null);
  assert.equal(isSnapshotWorthKeeping(dashboardData({ usage: null, activity: null })), true);
  assert.equal(isSnapshotWorthKeeping(dashboardData({ departments: 0, people: 0, readyDocs: 0 })), true);
  assert.equal(isSnapshotWorthKeeping({ departments: 1 }), false);
  assert.equal(isSnapshotWorthKeeping(null), false);
});

test("dashboard snapshot round-trips and expires after 7 days", () => {
  const storage = memoryStorage();
  const snapshot = recordDashboardSnapshot(storage, dashboardData(), NOW);
  assert.equal(snapshot.savedAt, NOW);
  assert.equal(loadDashboardSnapshot(storage, NOW).data.departments, 2);
  assert.equal(loadDashboardSnapshot(storage, NOW + OFFLINE_READ_CACHE_MAX_AGE_MS + 1), null);
});

test("a later snapshot overwrites the earlier one; an empty load never does", () => {
  const storage = memoryStorage();
  recordDashboardSnapshot(storage, dashboardData(), NOW);
  const empty = dashboardData({
    aiActive: false,
    aiLabel: "Not connected",
    departments: 0,
    people: 0,
    readyDocs: 0,
    usage: null,
    activity: null,
  });
  assert.equal(recordDashboardSnapshot(storage, empty, NOW + 1000), null);
  assert.equal(loadDashboardSnapshot(storage, NOW + 1000).savedAt, NOW);
  recordDashboardSnapshot(storage, dashboardData({ departments: 4 }), NOW + 2000);
  assert.equal(loadDashboardSnapshot(storage, NOW + 2000).data.departments, 4);
});

test("clearReadCache removes answers and snapshot", () => {
  const storage = memoryStorage();
  recordKnowledgeAnswer(storage, answerInput(), NOW);
  recordDashboardSnapshot(storage, dashboardData(), NOW);
  clearReadCache(storage);
  assert.deepEqual(loadKnowledgeAnswers(storage, NOW), []);
  assert.equal(loadDashboardSnapshot(storage, NOW), null);
});
