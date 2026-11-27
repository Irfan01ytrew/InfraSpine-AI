#!/usr/bin/env node
/** Add one manager-scoped manual task workflow to an isolated candidate fixture. */
import { readFileSync } from "node:fs";

const base = new URL(process.env.INFRASPINE_CANDIDATE_URL ?? "http://127.0.0.1:18082");
const fixturePath = process.env.INFRASPINE_QA_USERS_FILE;
if (!fixturePath) throw new Error("Set INFRASPINE_QA_USERS_FILE to the protected synthetic fixture.");
const accounts = JSON.parse(readFileSync(fixturePath, "utf8").replace(/^\uFEFF/, ""));
let cookie = "";

async function login(account) {
  const response = await fetch(new URL("/api/auth/login", base), {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest" },
    body: JSON.stringify({ email: account.email, password: account.password }),
  });
  if (!response.ok) throw new Error(`Synthetic ${account.email.split("@")[0]} login returned HTTP ${response.status}.`);
  const values = response.headers.getSetCookie?.() ?? [response.headers.get("set-cookie") ?? ""];
  cookie = values.map((value) => value.split(";", 1)[0]).filter(Boolean).join("; ");
  if (!cookie) throw new Error("Synthetic account session cookie was not set.");
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
  if (response.status !== expected) throw new Error(`${method} ${route} returned HTTP ${response.status}; expected ${expected}.`);
  return payload;
}

await login(accounts.admin);
const users = (await request("GET", "/api/admin/users", undefined, 200)).users;
const employee = users.find((user) => user.email === accounts.employee.email && user.role === "employee");
if (!employee) throw new Error("Synthetic QA employee account was not found.");

await login(accounts.manager);
const departments = (await request("GET", "/api/departments/managed", undefined, 200)).groups;
const operations = departments.find((group) => group.name === "QA Operations" && group.kind === "department");
if (!operations) throw new Error("Manager has no managed synthetic Operations department.");
const workflow = await request("POST", "/api/automation/workflows", {
  name: "QA Operations recovery task",
  department_group_id: operations.id,
  trigger_type: "manual",
  interval_minutes: null,
  assignee_user_id: employee.id,
  task_title: "Verify isolated recovery evidence",
  task_body: "Confirm the synthetic source citation and preview in this candidate.",
  due_days: 3,
}, 201);
if (!workflow.enabled || !workflow.can_manage || workflow.department_group_id !== operations.id) {
  throw new Error("Candidate workflow was not saved with the manager's department scope.");
}
console.log(`CANDIDATE_WORKFLOW_SEED=PASS id=${workflow.id} department=${operations.id}`);
