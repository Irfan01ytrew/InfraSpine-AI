#!/usr/bin/env node
/** Exercise selected-document ACLs across chat, research, training and agent routes in a synthetic app. */
import { readFileSync } from "node:fs";

const base = new URL(process.env.INFRASPINE_CANDIDATE_URL ?? "http://127.0.0.1:18082");
const fixturePath = process.env.INFRASPINE_QA_USERS_FILE;
if (!fixturePath) throw new Error("Set INFRASPINE_QA_USERS_FILE to the protected synthetic fixture.");
const accounts = JSON.parse(readFileSync(fixturePath, "utf8").replace(/^\uFEFF/, ""));
let cookie = "";

async function login(role) {
  const response = await fetch(new URL("/api/auth/login", base), {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest" },
    body: JSON.stringify({ email: accounts[role].email, password: accounts[role].password }),
  });
  if (!response.ok) throw new Error(`${role} synthetic login failed with HTTP ${response.status}.`);
  const values = response.headers.getSetCookie?.() ?? [response.headers.get("set-cookie") ?? ""];
  cookie = values.map((value) => value.split(";", 1)[0]).filter(Boolean).join("; ");
  if (!cookie) throw new Error(`${role} session cookie was not set.`);
}

async function request(method, route, body, expected) {
  const response = await fetch(new URL(route, base), {
    method,
    headers: {
      Cookie: cookie,
      "X-Requested-With": "XMLHttpRequest",
      ...(body === undefined ? {} : { "Content-Type": "application/json" }),
    },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  let payload = null;
  try { payload = await response.json(); } catch {}
  if (expected !== undefined && response.status !== expected) {
    throw new Error(`${method} ${route} returned HTTP ${response.status}; expected ${expected}.`);
  }
  return { status: response.status, payload };
}

function assertRefusal(result, label) {
  const payload = result.payload;
  const body = JSON.stringify(payload ?? {});
  const clean = !body.includes("83,715") && !body.includes("FINANCE_ONLY_CANDIDATE_83715");
  if (!payload || !clean) throw new Error(`${label} included a restricted synthetic value.`);
  return clean;
}

await login("admin");
const docs = (await request("GET", "/api/rag/documents", undefined, 200)).payload.documents;
const finance = docs.find((doc) => doc.filename === "qa-finance.pdf" && doc.status === "ready");
const operations = docs.find((doc) => doc.filename === "qa-operations.pdf" && doc.status === "ready");
const users = (await request("GET", "/api/admin/users", undefined, 200)).payload.users;
const employee = users.find((user) => user.email === accounts.employee.email && user.role === "employee");
const profiles = (await request("GET", "/api/agents", undefined, 200)).payload.agents;
const profile = profiles.find((item) => item.enabled && item.name.startsWith("QA department researcher"));
if (!finance || !operations || !employee || !profile) {
  throw new Error(`Synthetic fixture is incomplete (finance=${Boolean(finance)}, operations=${Boolean(operations)}, employee=${Boolean(employee)}, scoped_agent=${Boolean(profile)}, agent_scopes=${profiles.map((item) => `${item.name}:${item.knowledge_scope}:${item.enabled}`).join(";")}).`);
}

await login("manager");
const managedGroups = (await request("GET", "/api/departments/managed", undefined, 200)).payload.groups;
const operationsGroup = managedGroups.find((group) => group.name === "QA Operations");
if (!operationsGroup) throw new Error("Manager's synthetic Operations department was missing.");
const initialMembers = (await request("GET", `/api/departments/${operationsGroup.id}/members`, undefined, 200)).payload.members;
if (!initialMembers.some((member) => member.user_id === employee.id)) {
  await request("POST", `/api/departments/${operationsGroup.id}/members`, { email: employee.email }, 204);
}

await login("employee");
const chat = await request("POST", "/api/rag/chat", {
  question: "What is the Finance batch F-42 reconciliation target?",
  document_ids: [finance.id],
}, 200);
assertRefusal(chat, "Chat");
if (chat.payload.retrieved_chunks !== 0 || chat.payload.citations?.length !== 0 ||
    !chat.payload.insufficient_evidence || chat.payload.provider !== null || chat.payload.strategy !== "none") {
  throw new Error("Chat did not fail closed before the answer provider for an unreadable selected document.");
}
console.log("ACL_CHAT_PRIVATE_SELECTION_NO_PROVIDER=PASS");

const agent = await request("POST", `/api/agents/${profile.id}/runs`, {
  capability: "knowledge_search",
  task: "What is the Finance batch F-42 reconciliation target?",
  document_ids: [finance.id],
});
if (agent.status === 200) {
  assertRefusal(agent, "Agent");
  if (agent.payload.result.retrieved_chunks !== 0 || agent.payload.result.citations?.length !== 0 ||
      !agent.payload.result.insufficient_evidence || agent.payload.result.provider !== null) {
    throw new Error("Agent did not enforce the Finance document ACL before answer generation.");
  }
  console.log("ACL_AGENT_PRIVATE_SELECTION_NO_PROVIDER=PASS");
} else if ([403, 404].includes(agent.status)) {
  // On a repeat run, the Employee may already have been removed from the scoped department.
  console.log(`ACL_AGENT_PROFILE_REVOKED_MEMBERSHIP=PASS status=${agent.status}`);
} else {
  throw new Error(`Agent private-document request returned unexpected HTTP ${agent.status}.`);
}

const research = await request("POST", "/api/rag/research", {
  question: "What is the Finance batch F-42 reconciliation target?",
  document_ids: [finance.id],
}, 200);
assertRefusal(research, "Research");
if (!research.payload.findings?.length || research.payload.findings.some((finding) =>
  finding.retrieved_chunks !== 0 || finding.citations?.length !== 0 || !finding.insufficient_evidence)) {
  throw new Error("Internal research returned evidence from a Finance document outside the Employee ACL.");
}
console.log(`ACL_RESEARCH_PRIVATE_SELECTION=PASS findings=${research.payload.findings.length}`);

const training = await request("POST", "/api/rag/training", {
  topic: "Finance batch F-42 reconciliation",
  document_ids: [finance.id],
}, 200);
assertRefusal(training, "Training");
if (!training.payload.modules?.length || training.payload.modules.some((module) =>
  module.retrieved_chunks !== 0 || module.citations?.length !== 0 || !module.insufficient_evidence) ||
  training.payload.quiz?.length !== 0) {
  throw new Error("Employee training returned evidence or quiz content from an unreadable Finance document.");
}
console.log(`ACL_TRAINING_PRIVATE_SELECTION=PASS modules=${training.payload.modules.length}`);

await login("manager");
const members = (await request("GET", `/api/departments/${operationsGroup.id}/members`, undefined, 200)).payload.members;
if (members.some((member) => member.user_id === employee.id)) {
  await request("DELETE", `/api/departments/${operationsGroup.id}/members/${employee.id}`, undefined, 204);
}
console.log("ACL_DEPARTMENT_MEMBERSHIP_REVOCATION=PASS");

await login("employee");
const detail = await request("GET", `/api/rag/documents/${operations.id}`, undefined);
const preview = await request("GET", `/api/rag/documents/${operations.id}/pages/1/image`, undefined);
if (detail.status !== 404 || preview.status !== 404) {
  throw new Error(`Department revocation left source access open (document=${detail.status}, preview=${preview.status}).`);
}
for (const [label, route, body] of [
  ["chat", "/api/rag/chat", { question: "Summarize qa-operations.pdf.", document_ids: [operations.id] }],
  ["research", "/api/rag/research", { question: "Summarize qa-operations.pdf.", document_ids: [operations.id] }],
  ["training", "/api/rag/training", { topic: "Operations recovery procedure", document_ids: [operations.id] }],
]) {
  const result = await request("POST", route, body, 200);
  assertRefusal(result, `Revoked ${label}`);
  const value = label === "chat" ? result.payload : label === "research" ? result.payload.findings : result.payload.modules;
  const rows = Array.isArray(value) ? value : [value];
  if (rows.some((row) => row.retrieved_chunks !== 0 || row.citations?.length !== 0 || !row.insufficient_evidence)) {
    const summary = rows.map((row) => ({
      retrieved_chunks: row.retrieved_chunks,
      citations: row.citations?.map((citation) => ({ document_id: citation.document_id, filename: citation.filename })) ?? null,
      insufficient_evidence: row.insufficient_evidence,
      provider: row.provider ?? null,
      strategy: row.strategy ?? null,
      general_knowledge: row.general_knowledge ?? null,
      restricted_value_present: JSON.stringify(row).includes("83,715") || JSON.stringify(row).includes("FINANCE_ONLY_CANDIDATE_83715"),
    }));
    throw new Error(`Department revocation did not return an insufficient-evidence shape for ${label}: ${JSON.stringify(summary)}.`);
  }
}
console.log("ACL_REVOKED_MEMBERSHIP_CHAT_RESEARCH_TRAINING=PASS");

const revokedAgent = await request("POST", `/api/agents/${profile.id}/runs`, {
  capability: "knowledge_search",
  task: "Summarize qa-operations.pdf.",
  document_ids: [operations.id],
});
if (![403, 404].includes(revokedAgent.status)) {
  throw new Error(`Department revocation left agent access open (HTTP ${revokedAgent.status}).`);
}
console.log(`ACL_REVOKED_MEMBERSHIP_AGENT=PASS status=${revokedAgent.status}`);

await login("manager");
await request("POST", `/api/departments/${operationsGroup.id}/members`, { email: employee.email }, 204);
console.log("SYNTHETIC_EMPLOYEE_MEMBERSHIP_RESTORED=PASS");
