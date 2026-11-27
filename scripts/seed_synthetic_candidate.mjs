#!/usr/bin/env node
/** Seed one disposable local QA installation with synthetic role fixtures and ACL documents.
 * Secrets are read only from an ACL-protected JSON file and are never written to stdout.
 */
import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { setTimeout as delay } from "node:timers/promises";

const base = new URL(process.env.INFRASPINE_CANDIDATE_URL ?? "http://127.0.0.1:18082");
const fixturePath = process.env.INFRASPINE_QA_USERS_FILE;
const envPath = process.env.INFRASPINE_CANDIDATE_ENV_FILE;
const composePath = process.env.INFRASPINE_CANDIDATE_COMPOSE_FILE;
const project = process.env.INFRASPINE_CANDIDATE_PROJECT;
if (!fixturePath || !envPath || !composePath || !project) {
  throw new Error("Candidate URL, protected account fixture, Compose env/file and project are required.");
}
const accounts = JSON.parse(readFileSync(fixturePath, "utf8").replace(/^\uFEFF/, ""));
for (const role of ["admin", "manager", "employee"]) {
  if (typeof accounts[role]?.email !== "string" || typeof accounts[role]?.password !== "string") {
    throw new Error("Protected synthetic account fixture is incomplete.");
  }
}

const docker = process.env.INFRASPINE_DOCKER_PATH ?? "docker.exe";
const bootstrap = spawnSync(docker, [
  "--context", "desktop-linux", "compose", "--env-file", envPath, "-f", composePath, "-p", project,
  "exec", "-T", "api", "python", "-m", "app.cli", "create-user", "--email", accounts.admin.email,
  "--role", "admin", "--password-stdin",
], { input: `${accounts.admin.password}\n`, encoding: "utf8", windowsHide: true });
if (bootstrap.status !== 0) throw new Error("Synthetic administrator bootstrap failed.");

let cookie = "";
async function request(method, route, body, file) {
  const headers = { "X-Requested-With": "XMLHttpRequest" };
  if (cookie) headers.Cookie = cookie;
  let requestBody;
  if (file) {
    requestBody = new FormData();
    requestBody.append("file", new Blob([file.bytes], { type: "application/pdf" }), file.name);
    requestBody.set("visibility", "restricted");
    requestBody.set("department_id", file.departmentId);
  } else if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    requestBody = JSON.stringify(body);
  }
  const response = await fetch(new URL(route, base), { method, headers, body: requestBody });
  let payload = null;
  try { payload = await response.json(); } catch {}
  return { status: response.status, payload, response };
}

async function expect(method, route, status, body, file) {
  const result = await request(method, route, body, file);
  if (result.status !== status) throw new Error(`${method} ${route} returned HTTP ${result.status}; expected ${status}.`);
  return result.payload;
}

const loginResponse = await fetch(new URL("/api/auth/login", base), {
  method: "POST",
  headers: { "Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest" },
  body: JSON.stringify({ email: accounts.admin.email, password: accounts.admin.password }),
});
if (!loginResponse.ok) throw new Error(`Synthetic administrator login returned HTTP ${loginResponse.status}.`);
const setCookies = loginResponse.headers.getSetCookie?.() ?? [loginResponse.headers.get("set-cookie") ?? ""];
cookie = setCookies.map((line) => line.split(";", 1)[0]).filter(Boolean).join("; ");
if (!cookie) throw new Error("Synthetic administrator session cookie was not set.");

const financeGroup = await expect("POST", "/api/admin/access/groups", 201, {
  name: "QA Finance", kind: "department",
});
const operationsGroup = await expect("POST", "/api/admin/access/groups", 201, {
  name: "QA Operations", kind: "department",
});

async function invite(role) {
  const account = accounts[role];
  const invitation = await expect("POST", "/api/admin/users/invitations", 201, {
    email: account.email, role, department_ids: [operationsGroup.id],
  });
  if (typeof invitation.token !== "string" || invitation.token.length < 30) {
    throw new Error(`Synthetic ${role} invitation did not return a usable single-use token.`);
  }
  const accepted = await expect("POST", "/api/onboarding/accept", 200, {
    token: invitation.token, password: account.password,
  });
  if (accepted.role !== role) throw new Error(`Synthetic ${role} onboarding assigned the wrong role.`);
}
await invite("manager");
await invite("employee");

function pdf(text) {
  const escaped = text.replaceAll("\\", "\\\\").replaceAll("(", "\\(").replaceAll(")", "\\)");
  const stream = `BT /F1 12 Tf 72 720 Td (${escaped}) Tj ET`;
  const objects = [
    "<< /Type /Catalog /Pages 2 0 R >>",
    "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
    "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
    "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    `<< /Length ${Buffer.byteLength(stream, "ascii")} >>\nstream\n${stream}\nendstream`,
  ];
  let result = "%PDF-1.4\n";
  const offsets = [0];
  for (let i = 0; i < objects.length; i++) {
    offsets.push(Buffer.byteLength(result, "ascii"));
    result += `${i + 1} 0 obj\n${objects[i]}\nendobj\n`;
  }
  const xref = Buffer.byteLength(result, "ascii");
  result += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n`;
  for (const offset of offsets.slice(1)) result += `${String(offset).padStart(10, "0")} 00000 n \n`;
  result += `trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`;
  return Buffer.from(result, "ascii");
}

async function upload(name, content, departmentId) {
  const doc = await expect("POST", "/api/rag/documents", 202, undefined, {
    name, bytes: pdf(content), departmentId,
  });
  const until = Date.now() + 150_000;
  let state;
  while (Date.now() < until) {
    state = await expect("GET", `/api/rag/documents/${doc.id}`, 200);
    if (state.status === "ready" && state.searchable && state.chunk_count > 0) return state;
    if (state.status === "failed") throw new Error(`Synthetic PDF processing failed (${state.error_code ?? "no code"}).`);
    await delay(1_000);
  }
  throw new Error(`Synthetic PDF did not become searchable (${state?.status ?? "unknown"}).`);
}

const finance = await upload(
  "qa-finance.pdf",
  "Synthetic Finance batch F-42 reconciliation target is 83,715 units. Marker FINANCE_ONLY_CANDIDATE_83715.",
  financeGroup.id,
);
const operations = await upload(
  "qa-operations.pdf",
  "Synthetic Operations recovery procedure: verify the isolated snapshot, restore files, then validate citations.",
  operationsGroup.id,
);

const adminUsers = await expect("GET", "/api/admin/users", 200);
for (const role of ["admin", "manager", "employee"]) {
  const user = adminUsers.users.find((candidate) => candidate.email === accounts[role].email && candidate.role === role);
  if (!user) throw new Error(`Synthetic ${role} account is not present in the user directory.`);
}

const sql = "CREATE ROLE qa_owner LOGIN; GRANT SELECT ON automation_runs, automation_approvals, automation_tasks, audit_logs, service_requests, service_request_events TO qa_owner;";
const grant = spawnSync(docker, ["--context", "desktop-linux", "exec", `${project}-postgres-1`, "psql", "-U", "acl_candidate", "-d", "infraspine_acl_candidate", "-v", "ON_ERROR_STOP=1", "-c", sql], { encoding: "utf8", windowsHide: true });
if (grant.status !== 0) throw new Error("Read-only browser acceptance SQL role setup failed.");

console.log(`SYNTHETIC_CANDIDATE_SEED=PASS users=3 departments=2 finance=${finance.id} finance_chunks=${finance.chunk_count} operations=${operations.id} operations_chunks=${operations.chunk_count}`);
