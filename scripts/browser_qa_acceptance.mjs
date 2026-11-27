#!/usr/bin/env node
/**
 * Authenticated browser acceptance for a local synthetic QA installation.
 * Credentials are read from an ACL-protected file and are never written to stdout.
 */
import { spawn, spawnSync } from "node:child_process";
import { createPrivateKey, randomBytes, sign as signSignature } from "node:crypto";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

const base = new URL(process.argv[2] ?? "http://localhost:18081/");
const credentialPath = process.env.INFRASPINE_QA_USERS_FILE;
const dockerPath = process.env.INFRASPINE_DOCKER_PATH ?? "docker.exe";
const databaseContainer = process.env.INFRASPINE_QA_DB_CONTAINER ?? "infraspine-qa-29a24ed3-postgres-1";
const databaseName = process.env.INFRASPINE_QA_DB_NAME ?? "qa_acceptance_20261023_r1";
const databaseUser = process.env.INFRASPINE_QA_DB_USER ?? "qa_owner";
const mediaFixturePath = process.env.INFRASPINE_QA_MEDIA_FILE;
const videoFixturePath = process.env.INFRASPINE_QA_VIDEO_FILE;
let adaptiveTrainingCourseId = process.env.INFRASPINE_QA_TRAINING_COURSE_ID;
const roles = ["admin", "manager", "employee"];
let browserActionContext = "browser interaction";

if (!["http:", "https:"].includes(base.protocol) || !["localhost", "127.0.0.1", "::1"].includes(base.hostname)) {
  throw new Error("Browser acceptance is restricted to a loopback QA URL.");
}
if (!credentialPath) throw new Error("Set INFRASPINE_QA_USERS_FILE to the protected synthetic account fixture.");
const credentials = JSON.parse(readFileSync(credentialPath, "utf8").replace(/^\uFEFF/, ""));
for (const role of roles) {
  if (typeof credentials?.[role]?.email !== "string" || typeof credentials?.[role]?.password !== "string") {
    throw new Error("The protected fixture does not contain all three synthetic role accounts.");
  }
}

function findBrowser() {
  const configured = process.env.INFRASPINE_BROWSER_PATH;
  const candidates = configured ? [configured] : process.platform === "win32"
    ? [
        "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
        "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
        path.join(process.env.LOCALAPPDATA ?? "", "Google\\Chrome\\Application\\chrome.exe"),
        path.join(process.env.LOCALAPPDATA ?? "", "Microsoft\\Edge\\Application\\msedge.exe"),
      ]
    : ["/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome", "/usr/bin/microsoft-edge"];
  const browser = candidates.find((candidate) => candidate && existsSync(candidate));
  if (!browser) throw new Error("Chrome, Chromium or Edge was not found.");
  return browser;
}

function connectDebugger(endpoint) {
  const socket = new WebSocket(endpoint);
  const pending = new Map();
  let sequence = 0;
  const exceptions = [];
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.method === "Runtime.exceptionThrown") exceptions.push("page exception");
    if (message.id && pending.has(message.id)) {
      const entry = pending.get(message.id);
      clearTimeout(entry.timer);
      pending.delete(message.id);
      if (message.error) entry.reject(new Error("Browser command failed: " + String(message.error.message ?? "protocol error").slice(0, 240)));
      else entry.resolve(message.result);
    }
  });
  const opened = new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, { once: true });
    socket.addEventListener("error", () => reject(new Error("Browser debugging connection failed.")), { once: true });
  });
  return {
    socket,
    exceptions,
    async ready() { await opened; },
    command(method, params = {}, timeoutMs = 15_000) {
      const id = ++sequence;
      return new Promise((resolve, reject) => {
        const timer = setTimeout(() => {
          pending.delete(id);
          reject(new Error(`Browser command timed out: ${method}`));
        }, timeoutMs);
        pending.set(id, { resolve, reject, timer });
        socket.send(JSON.stringify({ id, method, params }));
      });
    },
  };
}

function databaseWorkflowCounts() {
  const sql = "SELECT (SELECT count(*) FROM automation_runs)||','||(SELECT count(*) FROM automation_approvals)||','||(SELECT count(*) FROM automation_tasks)||','||(SELECT count(*) FROM audit_logs);";
  const result = spawnSync(dockerPath, [
    "--context", "desktop-linux", "exec", databaseContainer, "psql", "-U", databaseUser, "-d", databaseName, "-Atc", sql,
  ], { encoding: "utf8", windowsHide: true });
  if (result.status !== 0 || !/^\d+,\d+,\d+,\d+\s*$/.test(result.stdout)) {
    throw new Error("Could not read PostgreSQL workflow side-effect counts.");
  }
  return result.stdout.trim();
}

function databaseWorkflowAuditCounts(runId, taskId) {
  const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  if (!uuid.test(runId) || !uuid.test(taskId)) {
    throw new Error("Workflow audit fixture identifiers must be UUIDs.");
  }
  const sql = "SELECT " +
    "count(*) FILTER (WHERE action='automation.run_requested' AND target='" + runId + "')||','||" +
    "count(*) FILTER (WHERE action='automation.run_approved' AND target='" + runId + "')||','||" +
    "count(*) FILTER (WHERE action='automation.task_created' AND target='" + runId + "')||','||" +
    "count(*) FILTER (WHERE action='automation.task_completed' AND target='" + taskId + "') FROM audit_logs;";
  const result = spawnSync(dockerPath, [
    "--context", "desktop-linux", "exec", databaseContainer, "psql", "-U", databaseUser, "-d", databaseName, "-Atc", sql,
  ], { encoding: "utf8", windowsHide: true });
  if (result.status !== 0 || !/^\d+,\d+,\d+,\d+\s*$/.test(result.stdout)) {
    throw new Error("Could not read the synthetic workflow audit lifecycle.");
  }
  return result.stdout.trim();
}

function databaseRequestCounts() {
  const sql = "SELECT (SELECT count(*) FROM service_requests)||','||(SELECT count(*) FROM service_request_events);";
  const result = spawnSync(dockerPath, [
    "--context", "desktop-linux", "exec", databaseContainer, "psql", "-U", databaseUser, "-d", databaseName, "-Atc", sql,
  ], { encoding: "utf8", windowsHide: true });
  if (result.status !== 0 || !/^\d+,\d+\s*$/.test(result.stdout)) {
    throw new Error("Could not read PostgreSQL service-request event counts.");
  }
  return result.stdout.trim();
}

function databaseFirstChunk(documentId) {
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(documentId)) {
    throw new Error("The conflict source document identifier is invalid.");
  }
  const result = spawnSync(dockerPath, [
    "--context", "desktop-linux", "exec", databaseContainer, "psql", "-U", databaseUser,
    "-d", databaseName, "-Atc", `SELECT content FROM document_chunks WHERE document_id='${documentId}' ORDER BY chunk_index LIMIT 1;`,
  ], { encoding: "utf8", windowsHide: true });
  const content = result.stdout.trim();
  if (result.status !== 0 || content.length < 40 || content.length > 200_000) {
    throw new Error("The synthetic conflict source did not produce a bounded indexed chunk.");
  }
  return content;
}

function databaseVideoLocator(documentId) {
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(documentId)) {
    throw new Error("The synthetic video document identifier is invalid.");
  }
  const sql = "SELECT coalesce(locator->>'kind','')||'|'||coalesce(locator->>'timestamp_seconds','')||'|'||" +
    "coalesce(locator->>'label','') FROM document_chunks WHERE document_id='" + documentId + "' ORDER BY chunk_index LIMIT 1;";
  const result = spawnSync(dockerPath, [
    "--context", "desktop-linux", "exec", databaseContainer, "psql", "-U", databaseUser, "-d", databaseName, "-Atc", sql,
  ], { encoding: "utf8", windowsHide: true });
  const fields = result.stdout.trim().split("|");
  const timestamp = Number(fields[1]);
  if (result.status !== 0 || fields.length !== 3 || fields[0] !== "video_frame" ||
      !Number.isFinite(timestamp) || timestamp < 0 || !/^Frame at \d{2}:\d{2}:\d{2}\.\d{3}$/.test(fields[2])) {
    throw new Error("The synthetic video worker did not persist a timestamped frame locator.");
  }
  return { kind: fields[0], timestamp_seconds: timestamp, label: fields[2] };
}

function databaseOpenConflictForRevision(revisionId) {
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(revisionId)) {
    throw new Error("The submitted conflict revision identifier is invalid.");
  }
  const result = spawnSync(dockerPath, [
    "--context", "desktop-linux", "exec", databaseContainer, "psql", "-U", databaseUser,
    "-d", databaseName, "-Atc", `SELECT id FROM knowledge_conflicts WHERE revision_id='${revisionId}' AND status='open';`,
  ], { encoding: "utf8", windowsHide: true });
  const conflictId = result.stdout.trim();
  if (result.status !== 0 || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(conflictId)) {
    throw new Error("The submitted synthetic revision did not produce one open conflict candidate.");
  }
  return conflictId;
}

function databaseConflictSummary(conflictId) {
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(conflictId)) {
    throw new Error("The synthetic conflict identifier is invalid.");
  }
  const sql = `SELECT c.status||','||
    (SELECT count(*) FROM knowledge_conflict_reviews r WHERE r.conflict_id=c.id)||','||
    (SELECT count(*) FROM audit_logs a WHERE a.action='knowledge.conflict_reviewed' AND a.target=c.id::text)
    FROM knowledge_conflicts c WHERE c.id='${conflictId}';`;
  const result = spawnSync(dockerPath, [
    "--context", "desktop-linux", "exec", databaseContainer, "psql", "-U", databaseUser,
    "-d", databaseName, "-Atc", sql,
  ], { encoding: "utf8", windowsHide: true });
  const summary = result.stdout.trim();
  if (result.status !== 0 || !/^(?:open|confirmed|dismissed),\d+,\d+$/.test(summary)) {
    throw new Error("Could not verify persisted synthetic knowledge-conflict review state.");
  }
  return summary;
}

function databaseBackdateKnowledgeRevision(revisionId) {
  const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  if (!uuid.test(revisionId)) throw new Error("Freshness-review fixture identifier must be a UUID.");
  const sql = "BEGIN; ALTER TABLE knowledge_revisions DISABLE TRIGGER knowledge_revision_freshness_fields_guard; " +
    "UPDATE knowledge_revisions SET review_due_at=now()-interval '1 day' WHERE id='" + revisionId + "'; " +
    "ALTER TABLE knowledge_revisions ENABLE TRIGGER knowledge_revision_freshness_fields_guard; COMMIT;";
  const result = spawnSync(dockerPath, [
    "--context", "desktop-linux", "exec", databaseContainer, "psql", "-v", "ON_ERROR_STOP=1", "-U", databaseUser,
    "-d", databaseName, "-Atc", sql,
  ], { encoding: "utf8", windowsHide: true });
  if (result.status !== 0) throw new Error("Could not age the synthetic knowledge revision in its disposable clone.");
}

function databaseFreshnessReviewState(revisionId) {
  const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  if (!uuid.test(revisionId)) throw new Error("Freshness-review fixture identifier must be a UUID.");
  const sql = "SELECT (review_due_at>now())::text||','||(SELECT count(*) FROM knowledge_freshness_reviews WHERE revision_id='" +
    revisionId + "') FROM knowledge_revisions WHERE id='" + revisionId + "';";
  const result = spawnSync(dockerPath, [
    "--context", "desktop-linux", "exec", databaseContainer, "psql", "-U", databaseUser, "-d", databaseName, "-Atc", sql,
  ], { encoding: "utf8", windowsHide: true });
  if (result.status !== 0 || !/^true,1\s*$/.test(result.stdout)) {
    throw new Error("PostgreSQL did not persist the authorized freshness review and future deadline.");
  }
}

function databaseRoleTemplateSummary(prefix) {
  if (!/^QA Browser Role Template \d+$/.test(prefix)) {
    throw new Error("Role-template browser fixture prefix is not in its reserved synthetic namespace.");
  }
  const sql = `WITH templates AS (SELECT id FROM role_templates WHERE name LIKE '${prefix} %'),
  assignments AS (SELECT id FROM role_template_assignments WHERE role_template_id IN (SELECT id FROM templates)),
  permission_rows AS (SELECT id FROM permission_grants WHERE role_template_assignment_id IN (SELECT id FROM assignments))
  SELECT (SELECT count(*) FROM templates)||','||
    (SELECT count(*) FROM assignments)||','||
    (SELECT count(*) FROM permission_rows)||','||
    (SELECT count(*) FROM governance_approvals WHERE permission_grant_id IN (SELECT id FROM permission_rows))||','||
    (SELECT count(*) FROM audit_logs WHERE action='access.role_template_created' AND target IN (SELECT id::text FROM templates))||','||
    (SELECT count(*) FROM audit_logs WHERE action='access.role_template_assigned' AND target IN (SELECT id::text FROM assignments))||','||
    (SELECT count(*) FROM audit_logs WHERE action='access.role_template_revoked' AND target IN (SELECT id::text FROM assignments));`;
  const result = spawnSync(dockerPath, [
    "--context", "desktop-linux", "exec", databaseContainer, "psql", "-U", databaseUser, "-d", databaseName, "-Atc", sql,
  ], { encoding: "utf8", windowsHide: true });
  if (result.status !== 0 || !/^\d+,\d+,\d+,\d+,\d+,\d+,\d+\s*$/.test(result.stdout)) {
    throw new Error("Could not read synthetic custom-role migration and audit evidence.");
  }
  return result.stdout.trim();
}

function databasePolicyExceptionSummary(exceptionId) {
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(exceptionId)) {
    throw new Error("The synthetic policy exception identifier is invalid.");
  }
  const sql = "SELECT x.status||','||(SELECT count(*) FROM knowledge_policy_exception_events e WHERE e.exception_id=x.id)||','||" +
    "(SELECT count(*) FROM audit_logs a WHERE a.target=x.id::text AND a.action='knowledge.policy_exception_assigned')||','||" +
    "(SELECT count(*) FROM audit_logs a WHERE a.target=x.id::text AND a.action='knowledge.policy_exception_acknowledged')||','||" +
    "(SELECT count(*) FROM audit_logs a WHERE a.target=x.id::text AND a.action='knowledge.policy_exception_resolved') " +
    `FROM knowledge_policy_exceptions x WHERE x.id='${exceptionId}';`;
  const result = spawnSync(dockerPath, [
    "--context", "desktop-linux", "exec", databaseContainer, "psql", "-U", databaseUser,
    "-d", databaseName, "-Atc", sql,
  ], { encoding: "utf8", windowsHide: true });
  const summary = result.stdout.trim();
  if (result.status !== 0 || !/^(open|acknowledged|resolved),\d+,\d+,\d+,\d+$/.test(summary)) {
    throw new Error("Could not verify the synthetic PostgreSQL policy-exception history.");
  }
  return summary;
}

function databaseRoleTemplateVersionSummary(templateName, requestId) {
  if (!/^QA Browser Role Version \d+ Review$/.test(templateName) ||
      !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(requestId)) {
    throw new Error("Role-template version acceptance must use its reserved synthetic name.");
  }
  const sql = `WITH template AS (SELECT id,version,is_active FROM role_templates WHERE name='${templateName}'),
    old_assignment AS (SELECT a.id,a.revoked_at FROM role_template_assignments a JOIN template t ON t.id=a.role_template_id WHERE t.version=1),
    old_grant AS (SELECT g.revoked_at FROM permission_grants g JOIN old_assignment a ON a.id=g.role_template_assignment_id),
    counts AS (SELECT (SELECT count(*) FROM template)||','||
      (SELECT count(*) FROM template WHERE version=1)||','||
      (SELECT count(*) FROM template WHERE version=2 AND is_active)||','||
      (SELECT count(*) FROM old_assignment)||','||
      (SELECT count(*) FROM old_assignment WHERE revoked_at IS NOT NULL)||','||
      (SELECT count(*) FROM old_grant)||','||
      (SELECT count(*) FROM old_grant WHERE revoked_at IS NOT NULL)||','||
      (SELECT count(*) FROM role_template_change_requests WHERE id='${requestId}' AND status='approved')||','||
      (SELECT count(*) FROM audit_logs WHERE action='access.role_template_change_approved' AND target='${requestId}'))
    SELECT * FROM counts;`;
  const result = spawnSync(dockerPath, [
    "--context", "desktop-linux", "exec", databaseContainer, "psql", "-U", databaseUser, "-d", databaseName, "-Atc", sql,
  ], { encoding: "utf8", windowsHide: true });
  if (result.status !== 0 || !/^\d+(?:,\d+){8}\s*$/.test(result.stdout)) {
    throw new Error("Could not read PostgreSQL role-template version acceptance counts.");
  }
  return result.stdout.trim();
}

function seedMeetingRecording(groupId, employeeEmail, adminEmail, prefix) {
  const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  if (!uuid.test(groupId) || !/^QA Meeting Browser \d+$/.test(prefix) ||
      !/^[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}$/i.test(employeeEmail) ||
      !/^[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}$/i.test(adminEmail)) {
    throw new Error("Meeting browser fixtures are outside the reserved synthetic scope.");
  }
  const sqlLiteral = (value) => `'${String(value).replaceAll("'", "''")}'`;
  const employeeSql = sqlLiteral(employeeEmail);
  const adminSql = sqlLiteral(adminEmail);
  const groupSql = sqlLiteral(groupId);
  const filenameSql = sqlLiteral(`${prefix}.mp3`);
  const sql = `WITH identities AS (
    SELECT employee.id AS employee_id, admin.id AS admin_id
    FROM users employee CROSS JOIN users admin
    WHERE employee.email=${employeeSql} AND admin.email=${adminSql}
      AND employee.is_active AND admin.is_active
  ), inserted_document AS (
    INSERT INTO documents (id,owner_id,visibility,source_system,filename,file_format,storage_key,sha256,size_bytes,
      version,status,chunk_count,indexed_version,indexed_filename,indexed_at,acl_status)
    SELECT gen_random_uuid(),employee_id,'restricted','upload',${filenameSql},'audio',
      md5(random()::text||clock_timestamp()::text),repeat('a',64),64,1,'ready',1,1,${filenameSql},now(),'active'
    FROM identities RETURNING id,owner_id
  ), group_acl AS (
    INSERT INTO resource_acl (resource_type,document_id,principal_group_id,origin,created_by)
    SELECT 'document',id,${groupSql}::uuid,'manual',owner_id FROM inserted_document
  ), admin_acl AS (
    INSERT INTO resource_acl (resource_type,document_id,principal_user_id,origin,created_by)
    SELECT 'document',inserted_document.id,identities.admin_id,'manual',inserted_document.owner_id
    FROM inserted_document CROSS JOIN identities
  ), inserted_chunk AS (
    INSERT INTO document_chunks (document_id,chunk_index,page_number,locator,content,embedding)
    SELECT id,0,1,'{"kind":"audio","label":"00:00:15","attrs":{"start_seconds":15.0,"end_seconds":22.0}}'::jsonb,
      'Synthetic browser recording: The team agreed to review access every quarter and record the review owner.',
      decode(repeat('00',1536),'hex')
    FROM inserted_document RETURNING id,document_id
  )
  SELECT document_id FROM inserted_chunk;`;
  const result = spawnSync(dockerPath, [
    "--context", "desktop-linux", "exec", databaseContainer, "psql", "-v", "ON_ERROR_STOP=1",
    "-U", databaseUser, "-d", databaseName, "-Atc", sql,
  ], { encoding: "utf8", windowsHide: true });
  const documentIds = String(result.stdout ?? "").split(/\r?\n/).map((line) => line.trim()).filter((line) => uuid.test(line));
  if (result.status !== 0 || documentIds.length !== 1) {
    const diagnostic = String(result.stderr ?? "").split(/\r?\n/).filter(Boolean).slice(-2).join(" ").replace(/\s+/g, " ").slice(0, 280);
    const resultShape = String(result.stdout ?? "").trim().replace(/[0-9a-f]{8}-[0-9a-f-]{27}/gi, "[ID]").slice(0, 120);
    throw new Error(`Could not seed the source-restricted synthetic meeting recording (exit=${result.status ?? "null"}, output=${resultShape || "empty"}${diagnostic ? `, ${diagnostic}` : ""}).`);
  }
  return documentIds[0];
}

function databaseMeetingEvidence(title) {
  if (!/^QA Meeting Browser \d+$/.test(title)) throw new Error("Meeting database check is outside the synthetic namespace.");
  const meetingTitle = `'${title.replaceAll("'", "''")}'`;
  const sql = `SELECT
    (SELECT count(*) FROM meeting_artifacts WHERE title=${meetingTitle})||','||
    (SELECT count(*) FROM meeting_revisions r JOIN meeting_artifacts m ON m.id=r.meeting_id WHERE m.title=${meetingTitle})||','||
    (SELECT count(*) FROM meeting_reviews r JOIN meeting_artifacts m ON m.id=r.meeting_id WHERE m.title=${meetingTitle})||','||
    (SELECT count(*) FROM meeting_action_tasks t JOIN meeting_artifacts m ON m.id=t.meeting_id WHERE m.title=${meetingTitle} AND t.status='completed')||','||
    (SELECT count(*) FROM audit_logs WHERE action='meeting.created' AND target IN (SELECT id::text FROM meeting_artifacts WHERE title=${meetingTitle}))||','||
    (SELECT count(*) FROM audit_logs WHERE action='meeting.reviewed' AND target IN (SELECT id::text FROM meeting_artifacts WHERE title=${meetingTitle}));`;
  const result = spawnSync(dockerPath, [
    "--context", "desktop-linux", "exec", databaseContainer, "psql", "-v", "ON_ERROR_STOP=1",
    "-U", databaseUser, "-d", databaseName, "-Atc", sql,
  ], { encoding: "utf8", windowsHide: true });
  if (result.status !== 0 || !/^1,3,2,1,1,2\s*$/.test(result.stdout)) {
    throw new Error("Meeting browser flow did not persist the expected revisions, reviews, completed task and audit trail.");
  }
  return result.stdout.trim();
}

function seedAdaptiveTrainingCourse(ownerId, documentId) {
  const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  if (!uuid.test(ownerId) || !uuid.test(documentId)) {
    throw new Error("Adaptive-training fixture identifiers must be UUIDs.");
  }
  const sql = `
WITH source_document AS (
  SELECT id, filename, version, sha256, indexed_version
  FROM documents WHERE id='${documentId}' AND visibility='shared' AND status='ready'
), citation AS (
  SELECT jsonb_build_object(
    'source_id','S1','document_id',id::text,'filename',filename,'page_number',1,
    'locator',NULL,'location','Page 1','chunk_index',0,'score',0.95,
    'snippet','Synthetic QA fixture: verify the owner before issuing the recovery token.','available',true
  ) AS item FROM source_document
), inserted AS (
  INSERT INTO training_courses (id,owner_user_id,topic,plan,source_snapshots,quiz_answer_key,status)
  SELECT gen_random_uuid(),'${ownerId}','Synthetic adaptive practice acceptance',
    jsonb_build_object(
      'topic','Synthetic adaptive practice acceptance','modules','[]'::jsonb,
      'quiz',jsonb_build_array(
        jsonb_build_object('question','Which synthetic recovery step requires confirmation?','choices',jsonb_build_array('Verify the owner','Skip verification','Delete all records','Ignore the request'),'citations',jsonb_build_array(citation.item)),
        jsonb_build_object('question','How should a recovery token be handled?','choices',jsonb_build_array('Keep it restricted','Publish it','Reuse without review','Discard its audit trail'),'citations',jsonb_build_array(citation.item)),
        jsonb_build_object('question','What action follows the synthetic policy?','choices',jsonb_build_array('Confirm the owner before issue','Share it publicly','Bypass approval','Disable logging'),'citations',jsonb_build_array(citation.item))
      ),
      'quiz_required',true,'model',NULL,'provider','synthetic-fixture','grounded_only',true,
      'status','in_progress','created_at',NULL,'completed_at',NULL
    ),
    jsonb_build_array(jsonb_build_array(jsonb_build_object(
      'document_id',source_document.id::text,'version',source_document.version,
      'sha256',source_document.sha256,'indexed_version',source_document.indexed_version
    ))),
    jsonb_build_array(
      jsonb_build_object('correct_index',0,'explanation','Verify the owner before issuing the recovery token [S1].'),
      jsonb_build_object('correct_index',0,'explanation','Keep the recovery token restricted under the source policy [S1].'),
      jsonb_build_object('correct_index',0,'explanation','The source requires owner confirmation before token issue [S1].')
    ),'in_progress'
  FROM source_document,citation RETURNING id
)
SELECT id FROM inserted;`;
  const result = spawnSync(dockerPath, [
    "--context", "desktop-linux", "exec", databaseContainer, "psql", "-v", "ON_ERROR_STOP=1",
    "-U", databaseUser, "-d", databaseName, "-Atc", sql,
  ], { encoding: "utf8", windowsHide: true });
  const courseId = result.stdout.trim();
  if (result.status !== 0 || !uuid.test(courseId)) {
    throw new Error("Could not seed the source-bound synthetic adaptive-training fixture.");
  }
  return courseId;
}

function markServiceRequestsOverdue(requestIds) {
  if (!requestIds.length || requestIds.some((id) => !/^[0-9a-f-]{36}$/i.test(id))) {
    throw new Error("The synthetic SLA acceptance supplied an invalid request identifier.");
  }
  const ids = requestIds.map((id) => `'${id}'`).join(",");
  const sql = `UPDATE service_requests SET response_due_at=now()-interval '1 second' WHERE id IN (${ids}) AND status='submitted' AND response_sla_met_at IS NULL RETURNING id;`;
  const result = spawnSync(dockerPath, [
    "--context", "desktop-linux", "exec", databaseContainer, "psql", "-U", databaseUser, "-d", databaseName, "-Atc", sql,
  ], { encoding: "utf8", windowsHide: true });
  const updated = result.stdout.trim().split(/\r?\n/).filter((line) => /^[0-9a-f-]{36}$/i.test(line)).length;
  if (result.status !== 0 || updated !== requestIds.length) {
    throw new Error("Could not prepare only the synthetic browser requests for SLA acceptance.");
  }
  return requestIds.length;
}

const browserPath = findBrowser();
const profile = mkdtempSync(path.join(tmpdir(), "infraspine-qa-browser-"));
const browser = spawn(browserPath, [
  "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
  "--disable-extensions", "--disable-background-networking", "--disable-sync", "--remote-debugging-port=0",
  `--user-data-dir=${profile}`, base.href,
], { stdio: "ignore", windowsHide: true });
let cdp;
const evidence = [];
let documentIds = {};
let identity = {};
let workflowTaskTitleForInbox = null;
let temporaryAssignee = null;
let temporaryAssigneeId = null;
let temporaryAssigneeDeactivated = false;

async function evaluate(expression, timeoutMs = 15_000) {
  const result = await cdp.command("Runtime.evaluate", {
    expression,
    awaitPromise: true,
    returnByValue: true,
  }, timeoutMs);
  if (result.exceptionDetails) {
    const detail = result.exceptionDetails.exception?.description ?? result.exceptionDetails.text ?? "page exception";
    throw new Error(`Browser-side acceptance action failed during ${browserActionContext}: ` + String(detail).slice(0, 240));
  }
  return result.result?.value;
}

async function request(method, route, body, timeoutMs = 15_000) {
  const encodedBody = body === undefined ? "undefined" : JSON.stringify(body);
  const expression = `(async()=>{const init={method:${JSON.stringify(method)},credentials:"same-origin",headers:{"X-Requested-With":"XMLHttpRequest"}};const data=${encodedBody};if(data!==undefined){init.headers["Content-Type"]="application/json";init.body=JSON.stringify(data)}const response=await fetch(${JSON.stringify(route)},init);let json=null;try{json=await response.json()}catch{}return {status:response.status,json}})()`;
  const result = await evaluate(expression, timeoutMs);
  if (!result || typeof result.status !== "number") throw new Error("Browser API request did not return a response.");
  return result;
}

async function expect(method, route, expected, body, timeoutMs = 15_000) {
  const result = await request(method, route, body, timeoutMs);
  if (result.status !== expected) throw new Error(`${method} ${route} returned HTTP ${result.status}; expected ${expected}.`);
  return result.json;
}

async function waitFor(predicate, timeoutMs = 20_000) {
  const until = Date.now() + timeoutMs;
  while (Date.now() < until) {
    const found = await evaluate(predicate);
    if (found) return found;
    await delay(250);
  }
  return false;
}

async function signIn(role) {
  return signInAccount(credentials[role], role);
}

async function signInAccount(credential, role) {
  const expression = `(()=>{const email=document.querySelector('input[name="email"]');const password=document.querySelector('input[name="password"]');const form=document.querySelector('form.login-form');if(!email||!password||!form)return false;const set=(input,value)=>{Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,value);input.dispatchEvent(new Event('input',{bubbles:true}))};set(email,${JSON.stringify(credential.email)});set(password,${JSON.stringify(credential.password)});form.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));return true})()`;
  const submitted = await evaluate(expression);
  if (!submitted) throw new Error(`Login form unavailable for ${role}.`);
  const session = await waitFor(`(async()=>{const r=await fetch('/api/auth/me',{credentials:'same-origin'});if(!r.ok)return false;const u=await r.json();return u.role===${JSON.stringify(role)}})()`);
  if (!session) throw new Error(`Authenticated ${role} session was not established.`);
  const ui = await evaluate(`(()=>({signedIn:!document.querySelector('input[name="email"]'),heading:document.querySelector('main h1, h1')?.textContent?.trim()??'',error:Boolean(document.querySelector('.banner-error,.inline-alert-error')),role:document.querySelector('.user-chip-role')?.textContent?.trim()??'',hasKnowledge:[...document.querySelectorAll('button.nav-item')].some(x=>x.textContent.trim()==='Knowledge'),hasRequests:[...document.querySelectorAll('button.nav-item')].some(x=>x.textContent.trim()==='Help & requests'),hasWorkflows:[...document.querySelectorAll('button.nav-item')].some(x=>x.textContent.trim()==='Workflows & tasks'),hasPeople:[...document.querySelectorAll('button.nav-item')].some(x=>x.textContent.trim()==='People & access')}))()`);
  const expectedLabel = { admin: "Administrator", manager: "Department manager", employee: "Employee" }[role];
  if (process.env.INFRASPINE_QA_LOGIN_ONLY === "1") {
    if (!ui?.signedIn || !ui.heading || ui.error) {
      throw new Error(`Authenticated ${role} account did not render a clean workspace (signed_in=${Boolean(ui?.signedIn)}, heading=${Boolean(ui?.heading)}, error=${Boolean(ui?.error)}).`);
    }
    return ui;
  }
  if (!ui?.signedIn || ui.role !== expectedLabel || !ui.hasKnowledge || !ui.hasRequests || !ui.hasWorkflows) {
    throw new Error(`The ${role} browser shell did not render the expected navigation.`);
  }
  if (ui.hasPeople !== (role === "admin")) throw new Error(`The ${role} admin navigation boundary was incorrect.`);
  evidence.push(`ROLE_UI_${role.toUpperCase()}=PASS`);
  return ui;
}

async function signOut() {
  const clicked = await evaluate(`(()=>{const button=document.querySelector('button[aria-label="Sign out"]');if(!button)return false;button.click();return true})()`);
  if (!clicked || !await waitFor(`Boolean(document.querySelector('form.login-form'))`)) {
    throw new Error("Browser sign-out did not return to the login form.");
  }
}

async function navigate(label, heading, selector) {
  const clicked = await evaluate(`(()=>{const b=[...document.querySelectorAll('button.nav-item')].find(x=>x.textContent.trim()===${JSON.stringify(label)});if(!b)return false;b.click();return true})()`);
  const ready = selector
    ? `Boolean(document.querySelector(${JSON.stringify(selector)}))`
    : `Boolean([...document.querySelectorAll('h1')].some(h=>h.textContent.trim()===${JSON.stringify(heading)}))`;
  if (!clicked || !await waitFor(ready)) {
    throw new Error(`The authenticated UI could not open ${label}.`);
  }
}

async function auditMeetingMinutesAcceptance() {
  const title = `QA Meeting Browser ${Date.now()}`;
  const employee = credentials.employee.email;
  const admin = credentials.admin.email;

  browserActionContext = "Manager meeting department setup";
  await signIn("manager");
  const managed = (await expect("GET", "/api/departments/managed", 200)).groups;
  const department = managed.find((item) => item.kind === "department");
  if (!department) throw new Error("The synthetic Manager has no assigned department for meeting acceptance.");
  await expect("POST", `/api/departments/${department.id}/members`, 204, { email: employee });
  const employeeMember = (await expect("GET", `/api/departments/${department.id}/members`, 200)).members
    .find((item) => item.email === employee);
  if (!employeeMember) throw new Error("Synthetic Employee membership was not available for the meeting acceptance flow.");
  await signOut();

  browserActionContext = "synthetic meeting transcript seed";
  const sourceId = seedMeetingRecording(department.id, admin, admin, title);
  browserActionContext = "Admin meeting draft UI";
  await signIn("admin");
  await navigate("Meeting minutes", "Meeting minutes");
  if (!await waitFor(`(()=>{const form=[...document.querySelectorAll("form")].find(item=>item.querySelector('input[placeholder="Quarterly operations review"]'));return Boolean([...((form?.querySelector("select")?.options)??[])].some(option=>option.value===${JSON.stringify(sourceId)}))})()`)) {
    throw new Error("The source selector did not finish loading the synthetic meeting recording.");
  }
  const create = await evaluate(`(()=>{
    const form=[...document.querySelectorAll("form")].find(item=>item.querySelector('input[placeholder="Quarterly operations review"]'));
    if(!form)return false;
    const source=form.querySelector("select");const name=form.querySelector('input[placeholder="Quarterly operations review"]');
    if(!source||!name||![...source.options].some(option=>option.value===${JSON.stringify(sourceId)}))return false;
    Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,"value").set.call(source,${JSON.stringify(sourceId)});source.dispatchEvent(new Event("change",{bubbles:true}));
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,"value").set.call(name,${JSON.stringify(title)});name.dispatchEvent(new Event("input",{bubbles:true}));
    const button=[...form.querySelectorAll("button")].find(item=>item.textContent.trim()==="Create minutes draft");
    if(!button||button.disabled)return false;button.click();return true;
  })()`);
  if (!create || !await waitFor(`Boolean([...document.querySelectorAll("h2")].some(item=>item.textContent.trim()===${JSON.stringify(title)}))`)) {
    throw new Error("Admin could not create and open meeting minutes from the visible UI.");
  }
  const transcriptShown = await waitFor(`document.body.innerText.includes("Synthetic browser recording: The team agreed to review access every quarter")`);
  if (!transcriptShown) throw new Error("The source-bound transcript evidence did not render in the meeting editor.");
  const meeting = (await expect("GET", "/api/meetings", 200)).meetings.find((item) => item.title === title);
  if (!meeting || meeting.owner_email !== admin || !meeting.source_is_current || !meeting.can_edit) {
    throw new Error("The meeting draft did not persist with the source owner and current transcript version.");
  }
  const clickCardButton = async (heading, label) => evaluate(`(()=>{
    const card=[...document.querySelectorAll(".card")].find(item=>item.querySelector("h3")?.textContent.trim()===${JSON.stringify(heading)});
    const button=[...(card?.querySelectorAll("button")??[])].find(item=>item.textContent.trim()===${JSON.stringify(label)});
    if(!button||button.disabled)return false;button.click();return true;
  })()`);
  const setCardField = async (heading, selector, index, value) => evaluate(`(()=>{
    const card=[...document.querySelectorAll(".card")].find(item=>item.querySelector("h3")?.textContent.trim()===${JSON.stringify(heading)});
    const field=card?.querySelectorAll(${JSON.stringify(selector)})[${index}];if(!field)return false;
    const proto=field instanceof HTMLSelectElement?HTMLSelectElement.prototype:field instanceof HTMLTextAreaElement?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto,"value").set.call(field,${JSON.stringify(value)});
    field.dispatchEvent(new Event(field instanceof HTMLSelectElement?"change":"input",{bubbles:true}));return true;
  })()`);
  const setMinutes = async (text) => evaluate(`(()=>{
    const field=document.querySelector("section.card textarea[rows='10']");if(!field)return false;
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,"value").set.call(field,${JSON.stringify(text)});
    field.dispatchEvent(new Event("input",{bubbles:true}));return true;
  })()`);
  const draft = "The team reviewed the synthetic access process, agreed on quarterly review, and named an owner for the next review date.";
  if (!await setMinutes(draft) || !await clickCardButton("Decisions", "Add decision")) {
    throw new Error("Admin could not edit minutes or add a cited decision in the UI.");
  }
  if (!await setCardField("Decisions", "input", 0, "Repeat access review every quarter.") ||
      !await setCardField("Decisions", "select", 0, String((await expect("GET", `/api/meetings/${meeting.id}/transcript`, 200)).chunks[0].id)) ||
      !await clickCardButton("Proposed tasks", "Add task")) {
    throw new Error("Admin could not link a decision to the source transcript and add a proposed task.");
  }
  const transcriptChunkId = (await expect("GET", `/api/meetings/${meeting.id}/transcript`, 200)).chunks[0].id;
  const employeeOption = await evaluate(`(()=>[...document.querySelectorAll(".card")].find(item=>item.querySelector("h3")?.textContent.trim()==="Proposed tasks")?.querySelector("select")?.value??"")()`);
  if (!employeeOption) throw new Error("The UI did not provide an initial eligible task assignee.");
  if (!await setCardField("Proposed tasks", "input", 0, "Record the next quarterly review date.") ||
      !await setCardField("Proposed tasks", "select", 0, employeeMember.user_id) ||
      !await setCardField("Proposed tasks", "textarea", 0, "Add the next synthetic review date to the team calendar.") ||
      !await setCardField("Proposed tasks", "select", 1, String(transcriptChunkId))) {
    throw new Error("Admin could not prepare an employee-assigned task with a transcript citation.");
  }
  const submitClicked = await evaluate(`(()=>{const button=[...document.querySelectorAll("button")].find(item=>item.textContent.trim()==="Save and submit for review");if(!button||button.disabled)return false;button.click();return true})()`);
  if (!submitClicked || !await waitFor(`(async()=>{const r=await fetch(${JSON.stringify(`/api/meetings/${meeting.id}`)});if(!r.ok)return false;return (await r.json()).status==="submitted"})()`)) {
    throw new Error("Admin could not save the editable revision and submit it for review.");
  }
  const submitted = await expect("GET", `/api/meetings/${meeting.id}`, 200);
  if (submitted.current_revision !== 2 || submitted.action_tasks.length !== 0) {
    throw new Error("Submission created a task before review or failed to retain revision two.");
  }
  evidence.push("F02_ADMIN_EDIT_CITE_AND_SUBMIT_UI=PASS");
  await signOut();

  browserActionContext = "Manager meeting review UI";
  await signIn("manager");
  await navigate("Meeting minutes", "Meeting minutes");
  const openMeeting = async () => evaluate(`(()=>{
    const row=[...document.querySelectorAll(".people-row")].find(item=>item.textContent.includes(${JSON.stringify(title)}));
    const button=[...(row?.querySelectorAll("button")??[])].find(item=>item.textContent.trim()==="Open");
    if(!button)return false;button.click();return true;
  })()`);
  if (!await openMeeting() || !await waitFor(`Boolean([...document.querySelectorAll("h2")].some(item=>item.textContent.trim()===${JSON.stringify(title)}))`)) {
    throw new Error("Manager could not open the scoped submitted meeting in the UI.");
  }
  const managerReview = await expect("GET", `/api/meetings/${meeting.id}`, 200);
  if (!managerReview.can_review) throw new Error("Assigned Manager did not receive the meeting review action.");
  const setReviewRationale = async (text) => evaluate(`(()=>{
    const card=[...document.querySelectorAll(".card")].find(item=>item.querySelector("h3")?.textContent.trim()==="Review this revision");
    const field=card?.querySelector("textarea");if(!field)return false;
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,"value").set.call(field,${JSON.stringify(text)});
    field.dispatchEvent(new Event("input",{bubbles:true}));return true;
  })()`);
  if (!await setReviewRationale("Add the review owner and next review date to the meeting record.") ||
      !await clickCardButton("Review this revision", "Request changes")) {
    throw new Error("Manager could not request a reasoned revision through the visible review form.");
  }
  if (!await waitFor(`(async()=>{const r=await fetch(${JSON.stringify(`/api/meetings/${meeting.id}`)});if(!r.ok)return false;return (await r.json()).status==="changes_requested"})()`)) {
    throw new Error("Manager's change request was not persisted.");
  }
  const changeRequest = await expect("GET", `/api/meetings/${meeting.id}`, 200);
  if (changeRequest.action_tasks.length !== 0 || changeRequest.reviews.length !== 1) {
    throw new Error("A change request created a task or failed to retain its review history.");
  }
  evidence.push("F02_MANAGER_REVIEW_REQUEST_CHANGES_UI=PASS");
  await signOut();

  browserActionContext = "Admin revised minutes UI";
  await signIn("admin");
  await navigate("Meeting minutes", "Meeting minutes");
  if (!await openMeeting()) throw new Error("Employee could not reopen a meeting returned for changes.");
  await waitFor(`Boolean(document.querySelector("section.card textarea[rows='10']"))`);
  if (!await setMinutes(`${draft} The revised record identifies the review owner.`)) throw new Error("Admin revision editor was unavailable.");
  const resubmitClicked = await evaluate(`(()=>{const button=[...document.querySelectorAll("button")].find(item=>item.textContent.trim()==="Save and submit for review");if(!button||button.disabled)return false;button.click();return true})()`);
  if (!resubmitClicked || !await waitFor(`(async()=>{const r=await fetch(${JSON.stringify(`/api/meetings/${meeting.id}`)});if(!r.ok)return false;const m=await r.json();return m.status==="submitted"&&m.current_revision===3})()`)) {
    throw new Error("Admin could not save and resubmit a new meeting revision.");
  }
  const resubmitted = await expect("GET", `/api/meetings/${meeting.id}`, 200);
  if (resubmitted.current_revision !== 3 || resubmitted.action_tasks.length !== 0) {
    throw new Error("Resubmission did not keep action items pending approval.");
  }
  evidence.push("F02_ADMIN_RESUBMIT_REVISION_UI=PASS");
  await signOut();

  browserActionContext = "Manager approval UI";
  await signIn("manager");
  await navigate("Meeting minutes", "Meeting minutes");
  if (!await openMeeting()) throw new Error("Scoped Manager could not open the submitted meeting for review.");
  await waitFor(`Boolean(document.querySelector("section.card textarea[rows='10']"))`);
  const finalManagerReview = await expect("GET", `/api/meetings/${meeting.id}`, 200);
  if (!finalManagerReview.can_review || finalManagerReview.owner_email === credentials.manager.email) {
    throw new Error("Scoped Manager did not receive review access separate from the meeting author.");
  }
  if (!await setReviewRationale("The transcript supports these minutes and the assigned follow-up.") ||
      !await clickCardButton("Review this revision", "Approve minutes and tasks")) {
    throw new Error("Manager could not approve the current meeting revision through the visible review form.");
  }
  if (!await waitFor(`(async()=>{const r=await fetch(${JSON.stringify(`/api/meetings/${meeting.id}`)});if(!r.ok)return false;const m=await r.json();return m.status==="approved"&&m.action_tasks.length===1})()`)) {
    throw new Error("Manager approval did not create exactly one reviewed action task.");
  }
  const approved = await expect("GET", `/api/meetings/${meeting.id}`, 200);
  const task = approved.action_tasks[0];
  if (task.status !== "open" || task.assignee_email !== employee || task.assignee_user_id !== employeeMember.user_id || task.evidence_chunk_ids[0] !== transcriptChunkId) {
    throw new Error("The approved task lost its assignee or source citation.");
  }
  evidence.push("F02_MANAGER_APPROVAL_CREATES_CITED_TASK_UI=PASS");
  await signOut();

  browserActionContext = "Employee action completion and ACL revocation";
  await signIn("employee");
  await navigate("Meeting minutes", "Meeting minutes");
  if (!await openMeeting()) throw new Error("Assigned Employee could not open the approved meeting action.");
  await waitFor(`Boolean([...document.querySelectorAll("button")].some(item=>item.textContent.trim()==="Mark complete"))`);
  const complete = await evaluate(`(()=>{const button=[...document.querySelectorAll("button")].find(item=>item.textContent.trim()==="Mark complete");if(!button)return false;button.click();return true})()`);
  if (!complete || !await waitFor(`(async()=>{const r=await fetch(${JSON.stringify(`/api/meetings/${meeting.id}`)});if(!r.ok)return false;return (await r.json()).action_tasks[0]?.status==="completed"})()`)) {
    throw new Error("The assigned Employee could not complete the approved task through the visible UI.");
  }
  await signOut();
  await signIn("manager");
  const departmentMembers = (await expect("GET", `/api/departments/${department.id}/members`, 200)).members;
  const currentEmployee = departmentMembers.find((item) => item.email === employee);
  if (!currentEmployee || currentEmployee.user_id !== employeeMember.user_id) throw new Error("Synthetic Employee membership disappeared before revocation acceptance.");
  await expect("DELETE", `/api/departments/${department.id}/members/${employeeMember.user_id}`, 204);
  evidence.push("F02_MANAGER_REVOKE_SOURCE_ACCESS=PASS");
  await signOut();
  await signIn("employee");
  const deniedMeeting = await request("GET", `/api/meetings/${meeting.id}`);
  const deniedTranscript = await request("GET", `/api/meetings/${meeting.id}/transcript`);
  const deniedTask = await request("POST", `/api/meetings/tasks/${task.id}/complete`);
  if (deniedMeeting.status !== 404 || deniedTranscript.status !== 404 || deniedTask.status !== 404) {
    throw new Error("Meeting records, transcript or task remained accessible after department membership revocation.");
  }
  evidence.push("F02_EMPLOYEE_REVOKED_SOURCE_REFUSAL=PASS");
  await signOut();

  const counts = databaseMeetingEvidence(title);
  if (counts !== "1,3,2,1,1,2") throw new Error("PostgreSQL did not retain the expected F02 revision/review/action/audit history.");
  evidence.push(`F02_POSTGRESQL_REVISIONS_REVIEWS_COMPLETION_AUDIT=PASS counts=${counts}`);
}

async function auditPeopleInvitationControls() {
  const adminInvitationsPath = "/api/admin/users/invitations";
  const csvEmail = `qa-csv-invite-${Date.now()}@example.test`;
  const duplicateEmail = credentials.admin.email;
  await signIn("admin");
  await navigate("People & access", "People & access");

  // Revoke only active invitations from this harness's reserved synthetic namespace.
  // Revocation preserves the invitation/audit history; it does not delete any account or row.
  const priorHarnessInvitations = (await expect("GET", adminInvitationsPath, 200)).invitations
    .filter((item) => /^qa-csv-invite-\d+@example\.test$/.test(item.email));
  for (const item of priorHarnessInvitations) {
    await expect("POST", `${adminInvitationsPath}/${item.id}/revoke`, 200);
  }

  const beforePreview = (await expect("GET", adminInvitationsPath, 200)).invitations.length;
  const csvText = `email,role,departments\n${csvEmail},employee,\n${duplicateEmail},employee,`;
  const attached = await evaluate(`(()=>{const input=document.querySelector("section[aria-labelledby='csv-invite-heading'] input[type='file']");if(!input)return false;const transfer=new DataTransfer();transfer.items.add(new File([${JSON.stringify(csvText)}],"qa-invitations.csv",{type:"text/csv"}));input.files=transfer.files;input.dispatchEvent(new Event("change",{bubbles:true}));return true})()`);
  if (!attached) throw new Error("Admin CSV invitation file control was unavailable.");
  const previewClicked = await evaluate(`(()=>{const section=document.querySelector("section[aria-labelledby='csv-invite-heading']");const button=[...(section?.querySelectorAll("button")??[])].find(item=>item.textContent.trim()==="Preview rows");if(!button||button.disabled)return false;button.click();return true})()`);
  if (!previewClicked || !await waitFor(`document.querySelector("section[aria-labelledby='csv-invite-heading'] [role='status']")?.textContent.includes("1 valid, 1 need attention")`)) {
    throw new Error("Admin CSV preview did not distinguish a valid row from a duplicate existing account.");
  }
  const afterPreview = (await expect("GET", adminInvitationsPath, 200)).invitations.length;
  if (beforePreview !== afterPreview) throw new Error("CSV preview created an invitation before Admin selected a row.");
  const validCreated = await evaluate(`(()=>{const section=document.querySelector("section[aria-labelledby='csv-invite-heading']");const row=[...(section?.querySelectorAll("tbody tr")??[])].find(item=>item.textContent.includes(${JSON.stringify(csvEmail)}));const button=[...(row?.querySelectorAll("button")??[])].find(item=>item.textContent.trim()==="Create invitation");if(!button||button.disabled)return false;button.click();return true})()`);
  if (!validCreated || !await waitFor(`Boolean([...document.querySelectorAll("section[aria-labelledby='csv-invite-heading'] tbody tr")].find(row=>row.textContent.includes(${JSON.stringify(csvEmail)}))?.querySelector("a[href*='onboarding?token=']"))`)) {
    throw new Error("Admin could not create a one-time invitation from a valid preview row.");
  }
  const originalLink = await evaluate(`(()=>[...document.querySelectorAll("section[aria-labelledby='csv-invite-heading'] tbody tr")].find(row=>row.textContent.includes(${JSON.stringify(csvEmail)}))?.querySelector("a[href*='onboarding?token=']")?.href??null)()`);
  const tokenFromLink = (href) => {
    const hashQuery = String(href ?? "").split("#", 2)[1]?.split("?", 2)[1] ?? "";
    return new URLSearchParams(hashQuery).get("token") ?? "";
  };
  const originalToken = tokenFromLink(originalLink);
  if (originalToken.length < 30) throw new Error("Admin invitation link was not rendered as a single-use onboarding URL.");
  const createdInvitation = (await expect("GET", adminInvitationsPath, 200)).invitations.find((item) => item.email === csvEmail);
  if (!createdInvitation || createdInvitation.role !== "employee") throw new Error("Admin invitation did not persist with the requested employee role.");

  const adminResendReady = await waitFor(`(()=>{const section=[...document.querySelectorAll(".people-section")].find(item=>item.querySelector("h2")?.textContent.trim()==="Invite an employee");const row=[...(section?.querySelectorAll("tbody tr")??[])].find(item=>item.textContent.includes(${JSON.stringify(csvEmail)}));const button=[...(row?.querySelectorAll("button")??[])].find(item=>item.textContent.trim()==="Resend link");return Boolean(button&&!button.disabled)})()`);
  if (!adminResendReady) throw new Error("Admin invitation row did not finish loading before the resend check.");
  const adminRowResend = await evaluate(`(()=>{const section=[...document.querySelectorAll(".people-section")].find(item=>item.querySelector("h2")?.textContent.trim()==="Invite an employee");const row=[...(section?.querySelectorAll("tbody tr")??[])].find(item=>item.textContent.includes(${JSON.stringify(csvEmail)}));const button=[...(row?.querySelectorAll("button")??[])].find(item=>item.textContent.trim()==="Resend link");if(!button)return false;button.click();return true})()`);
  if (!adminRowResend || !await waitFor(`(()=>{const link=document.querySelector(".invite-link a[href*='onboarding?token=']");return Boolean(link&&link.href!==${JSON.stringify(originalLink)})})()`)) throw new Error("Admin invitation resend control did not return a replacement link.");
  const adminReplacementLink = await evaluate(`document.querySelector(".invite-link a[href*='onboarding?token=']")?.href??null`);
  const adminReplacementToken = tokenFromLink(adminReplacementLink);
  if (adminReplacementToken.length < 30 || adminReplacementToken === originalToken) throw new Error("Admin resend did not rotate the invitation token.");
  const passwordProbe = randomBytes(24).toString("base64url");
  const oldAdminToken = await request("POST", "/api/onboarding/accept", { token: originalToken, password: passwordProbe });
  if (![400, 403, 404, 410, 422].includes(oldAdminToken.status)) throw new Error(`The prior Admin invitation token remained usable after resend (HTTP ${oldAdminToken.status}).`);
  const adminRowRevoke = await evaluate(`(()=>{const section=[...document.querySelectorAll(".people-section")].find(item=>item.querySelector("h2")?.textContent.trim()==="Invite an employee");const row=[...(section?.querySelectorAll("tbody tr")??[])].find(item=>item.textContent.includes(${JSON.stringify(csvEmail)}));const button=[...(row?.querySelectorAll("button")??[])].find(item=>item.textContent.trim()==="Revoke");if(!button)return false;button.click();return true})()`);
  if (!adminRowRevoke) throw new Error("Admin invitation revoke row control was not found.");
  const revokeConfirm = await waitFor(`Boolean(document.querySelector("[role='dialog'] button.btn-danger"))`);
  if (!revokeConfirm) throw new Error("Admin invitation revoke confirmation dialog did not open.");
  const confirmAdminRevoke = revokeConfirm && await evaluate(`(()=>{const dialog=document.querySelector("[role='dialog']");const button=[...(dialog?.querySelectorAll("button")??[])].find(item=>item.textContent.trim()==="Revoke");if(!button)return false;button.click();return true})()`);
  if (!confirmAdminRevoke) throw new Error("Admin invitation revoke confirmation button was not clickable.");
  const adminInvitationRemoved = await waitFor(`(()=>{const section=[...document.querySelectorAll(".people-section")].find(item=>item.querySelector("h2")?.textContent.trim()==="Invite an employee");return ![...(section?.querySelectorAll("tbody tr")??[])].some(row=>row.textContent.includes(${JSON.stringify(csvEmail)}))})()`, 15_000);
  if (!adminInvitationRemoved) throw new Error("Admin revoke completed but the pending invitation remained in the active list.");
  const revokedAdminToken = await request("POST", "/api/onboarding/accept", { token: adminReplacementToken, password: passwordProbe });
  if (![400, 403, 404, 410, 422].includes(revokedAdminToken.status)) throw new Error(`A revoked Admin invitation token remained usable (HTTP ${revokedAdminToken.status}).`);
  evidence.push("C04_ADMIN_CSV_PREVIEW_NO_WRITE_CREATE_RESEND_ROTATION_AND_REVOKE=PASS");

  await signOut();
  await signIn("manager");
  const managed = (await expect("GET", "/api/departments/managed", 200)).groups;
  const department = managed.find((item) => item.kind === "department");
  if (!department) throw new Error("The synthetic Manager has no managed department for invite acceptance.");
  await navigate("My departments", "My departments");
  const managerInvitationPath = `/api/departments/${department.id}/invitations`;
  const scopedRowsBefore = (await expect("GET", managerInvitationPath, 200)).invitations;
  for (const item of scopedRowsBefore.filter((row) => /^qa-manager-invite-\d+@example\.test$/.test(row.email))) {
    await expect("POST", `${managerInvitationPath}/${item.id}/revoke`, 200);
  }
  if (scopedRowsBefore.some((item) => item.email === csvEmail)) throw new Error("Manager could view an Admin-created invitation outside their own invitation scope.");
  const managerEmail = `qa-manager-invite-${Date.now()}@example.test`;
  if (!await waitFor(`Boolean([...document.querySelectorAll("form")].find(item=>item.querySelector("button")?.textContent.includes("Create invitation link")))`, 15_000)) {
    const state = await evaluate(`(()=>({emptyState:document.body.innerText.includes("No department assignments"),error:Boolean(document.querySelector(".banner-error")),selects:document.querySelectorAll("select").length}))()`);
    throw new Error(`Manager department invitation form did not finish loading (empty=${state.emptyState}, error=${state.error}, department_selector_count=${state.selects}).`);
  }
  await evaluate(`(()=>{const original=window.fetch.bind(window);window.__qaInvitationPostStatuses=[];window.fetch=(input,init={})=>{const url=typeof input==="string"?input:input.url;const route=new URL(url,location.href).pathname;const method=String(init.method??"GET").toUpperCase();const result=original(input,init);if(method==="POST"&&route.includes("/api/departments/")&&route.endsWith("/invitations")){result.then(response=>window.__qaInvitationPostStatuses.push(response.status)).catch(()=>window.__qaInvitationPostStatuses.push(0))}return result};return true})()`);
  const managerInputSet = await evaluate(`(()=>{const form=[...document.querySelectorAll("form")].find(item=>item.querySelector("button")?.textContent.includes("Create invitation link"));const input=form?.querySelector("input[type='email']");if(!input)return false;const setter=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,"value").set;setter.call(input,${JSON.stringify(managerEmail)});input.dispatchEvent(new Event("input",{bubbles:true}));return true})()`);
  const managerInputState = managerInputSet && await evaluate(`(()=>{const form=[...document.querySelectorAll("form")].find(item=>item.querySelector("button")?.textContent.includes("Create invitation link"));const input=form?.querySelector("input[type='email']");const button=form?.querySelector("button");return {present:Boolean(input),valid:Boolean(input?.checkValidity()),disabled:Boolean(button?.disabled)}})()`);
  if (!managerInputSet || !managerInputState?.present) {
    const managerPageState = await evaluate(`(()=>({heading:[...document.querySelectorAll("h1")].map(item=>item.textContent.trim()),forms:[...document.querySelectorAll("form")].map(form=>({labels:[...form.querySelectorAll("label")].map(label=>label.textContent.trim()),inputTypes:[...form.querySelectorAll("input")].map(input=>input.type),buttons:[...form.querySelectorAll("button")].map(button=>button.textContent.trim())})),departmentLabels:[...document.querySelectorAll("select option")].map(option=>option.textContent.trim()),pageError:Boolean(document.querySelector(".banner-error"))}))()`);
    throw new Error(`Manager invitation email field was not available in My departments (ui=${JSON.stringify(managerPageState)}).`);
  }
  if (!managerInputState.valid || managerInputState.disabled) throw new Error(`Manager invitation form did not accept the synthetic address (valid=${managerInputState.valid}, submit_disabled=${managerInputState.disabled}).`);
  const managerSubmit = await evaluate(`(()=>{const form=[...document.querySelectorAll("form")].find(item=>item.querySelector("button")?.textContent.includes("Create invitation link"));const button=form?.querySelector("button");if(!button||button.disabled)return false;button.click();return true})()`);
  const managerInvitationReady = managerSubmit && await waitFor(`Boolean(document.querySelector(".invite-link a[href*='onboarding?token=']"))`);
  if (!managerInvitationReady) {
    const diagnostic = await evaluate(`(()=>({postStatuses:window.__qaInvitationPostStatuses??[],errorBanner:Boolean(document.querySelector(".banner-error")),successLink:Boolean(document.querySelector(".invite-link a[href*='onboarding?token=']"))}))()`);
    throw new Error(`Manager invitation UI did not produce a one-time link (submit=${managerSubmit}, response=${diagnostic.postStatuses.join(",")||"no POST"}, error_banner=${diagnostic.errorBanner}, link=${diagnostic.successLink}).`);
  }
  const managerOriginalLink = await evaluate(`document.querySelector(".invite-link a[href*='onboarding?token=']")?.href??null`);
  const managerOriginalToken = tokenFromLink(managerOriginalLink);
  const managerRow = (await expect("GET", `/api/departments/${department.id}/invitations`, 200)).invitations.find((item) => item.email === managerEmail);
  if (!managerRow || managerRow.role !== "employee" || JSON.stringify(managerRow.department_ids) !== JSON.stringify([department.id])) {
    throw new Error("Manager invitation did not stay limited to Employee role and the selected department.");
  }
  const managerTableResend = await evaluate(`(()=>{const row=[...document.querySelectorAll("tbody tr")].find(item=>item.textContent.includes(${JSON.stringify(managerEmail)}));const button=[...(row?.querySelectorAll("button")??[])].find(item=>item.textContent.trim()==="Resend link");if(!button)return false;button.click();return true})()`);
  if (!managerTableResend || !await waitFor(`(()=>{const anchor=document.querySelector(".invite-link a[href*='onboarding?token=']");return Boolean(anchor&&anchor.href!==${JSON.stringify(managerOriginalLink)})})()`)) throw new Error("Manager resend control did not return a replacement link.");
  const managerReplacementLink = await evaluate(`document.querySelector(".invite-link a[href*='onboarding?token=']")?.href??null`);
  const managerReplacementToken = tokenFromLink(managerReplacementLink);
  if (managerReplacementToken.length < 30 || managerReplacementToken === managerOriginalToken) throw new Error("Manager resend did not rotate the invitation token.");
  const oldManagerToken = await request("POST", "/api/onboarding/accept", { token: managerOriginalToken, password: passwordProbe });
  if (![400, 403, 404, 410, 422].includes(oldManagerToken.status)) throw new Error(`The prior Manager invitation token remained usable after resend (HTTP ${oldManagerToken.status}).`);
  const managerRowRevoke = await evaluate(`(()=>{const row=[...document.querySelectorAll("tbody tr")].find(item=>item.textContent.includes(${JSON.stringify(managerEmail)}));const button=[...(row?.querySelectorAll("button")??[])].find(item=>item.textContent.trim()==="Revoke");if(!button)return false;button.click();return true})()`);
  const managerConfirm = managerRowRevoke && await waitFor(`Boolean(document.querySelector("[role='dialog'] button.btn-danger"))`);
  const managerConfirmRevoke = managerConfirm && await evaluate(`(()=>{const dialog=document.querySelector("[role='dialog']");const button=[...(dialog?.querySelectorAll("button")??[])].find(item=>item.textContent.trim()==="Revoke");if(!button)return false;button.click();return true})()`);
  if (!managerConfirmRevoke || !await waitFor(`![...document.querySelectorAll("tbody tr")].some(row=>row.textContent.includes(${JSON.stringify(managerEmail)}))`, 8_000)) throw new Error("Manager invitation revoke confirmation did not remove the invitation from the active list.");
  const revokedManagerToken = await request("POST", "/api/onboarding/accept", { token: managerReplacementToken, password: passwordProbe });
  if (![400, 403, 404, 410, 422].includes(revokedManagerToken.status)) throw new Error(`A revoked Manager invitation token remained usable (HTTP ${revokedManagerToken.status}).`);
  evidence.push("C04_MANAGER_INVITE_UI_DEPARTMENT_SCOPE_RESEND_AND_REVOKE=PASS");

  await signOut();
  await signIn("employee");
  const deniedUsers = await request("GET", "/api/admin/users");
  const deniedInvites = await request("POST", `/api/departments/${department.id}/invitations`, { email: `qa-forbidden-${Date.now()}@example.test` });
  if (![401, 403].includes(deniedUsers.status) || ![401, 403].includes(deniedInvites.status)) {
    throw new Error("Employee was able to access administrative user or department invitation endpoints.");
  }
  if (await evaluate(`Boolean([...document.querySelectorAll("button.nav-item")].some(item=>item.textContent.trim()==="People & access"||item.textContent.trim()==="My departments"))`)) {
    throw new Error("Employee navigation exposed a people-management page.");
  }
  evidence.push("C04_EMPLOYEE_UI_AND_API_ADMIN_DENIAL=PASS");
}

async function auditCustomRoleTemplateControls() {
  browserActionContext = "Admin login and People navigation";
  await signIn("admin");
  await navigate("People & access", "People & access");
  const sectionSelector = "section[aria-labelledby='role-template-heading']";
  const prefix = `QA Browser Role Template ${Date.now()}`;
  const departmentName = `${prefix} Department`;
  browserActionContext = "synthetic department setup";
  const group = await expect("POST", "/api/admin/access/groups", 201, { name: departmentName, kind: "department" });
  for (const role of ["manager", "employee"]) {
    await expect("POST", `/api/admin/access/groups/${group.id}/members`, 204, { email: credentials[role].email });
  }
  browserActionContext = "People membership refresh";
  const refreshed = await evaluate(`(()=>{const button=document.querySelector('header.page-header button.btn');if(!button)return false;button.click();return true})()`);
  if (!refreshed || !await waitFor(`Boolean([...document.querySelectorAll('.people-chips .chip')].some(item=>item.textContent.trim()===${JSON.stringify(departmentName)}))`)) {
    throw new Error("The People UI did not refresh the synthetic department membership before scoped role assignment.");
  }

  const createTemplateInUi = async (name, baseRole, permission) => {
    browserActionContext = `create ${baseRole} ${permission} template through UI`;
    const clicked = await evaluate(`(()=>{
      const form=document.querySelector(${JSON.stringify(sectionSelector)})?.querySelectorAll('form')[0];
      const inputs=[...(form?.querySelectorAll('input')??[])];
      if(!form||inputs.length<2)return false;
      const setValue=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;
      setValue.call(inputs[0],${JSON.stringify(name)});inputs[0].dispatchEvent(new Event('input',{bubbles:true}));
      setValue.call(inputs[1],'Synthetic browser acceptance template.');inputs[1].dispatchEvent(new Event('input',{bubbles:true}));
      const roleSelect=form.querySelector('select');if(!roleSelect)return false;
      Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set.call(roleSelect,${JSON.stringify(baseRole)});
      roleSelect.dispatchEvent(new Event('change',{bubbles:true}));
      const actions=form.querySelectorAll('fieldset')[0];
      for(const label of actions?.querySelectorAll('label')??[]){const input=label.querySelector('input[type=checkbox]');const wanted=label.textContent.trim()===${JSON.stringify(permission)};if(input&&input.checked!==wanted)input.click()}
      const categories=form.querySelectorAll('fieldset')[1];
      for(const label of categories?.querySelectorAll('label')??[]){const input=label.querySelector('input[type=checkbox]');const wanted=label.textContent.trim()==='general';if(input&&input.checked!==wanted)input.click()}
      const button=form.querySelector('button.btn-primary');if(!button||button.disabled)return false;button.click();return true;
    })()`);
    if (!clicked || !await waitFor(`Boolean([...document.querySelectorAll(${JSON.stringify(sectionSelector)}+' table tbody tr')].find(row=>row.textContent.includes(${JSON.stringify(name)})))`)) {
      throw new Error(`Admin could not create the ${baseRole} ${permission} template through the People UI.`);
    }
    const row = (await expect("GET", "/api/admin/role-templates", 200)).templates.find((item) => item.name === name);
    if (!row || row.permissions.length !== 1 || row.permissions[0].permission !== permission ||
        JSON.stringify(row.permissions[0].categories) !== JSON.stringify(["general"])) {
      throw new Error("The role-template editor did not persist the selected action and category.");
    }
    return row;
  };

  const reviewer = await createTemplateInUi(`${prefix} Employee Review`, "employee", "knowledge.review");
  const publisher = await createTemplateInUi(`${prefix} Manager Publish`, "manager", "knowledge.publish");

  const assignInUi = async (template, role, expiresInMinutes = null) => {
    browserActionContext = `assign ${role} template through UI`;
    browserActionContext = `assign ${role} template: choose template`;
    const chooseTemplate = await evaluate(`(()=>{
      const form=document.querySelector(${JSON.stringify(sectionSelector)})?.querySelectorAll('form')[1];
      const select=form?.querySelector('select');if(!select)return false;
      Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set.call(select,${JSON.stringify(template.id)});
      select.dispatchEvent(new Event('change',{bubbles:true}));return true;
    })()`);
    browserActionContext = `assign ${role} template: wait for eligible-user options`;
    const optionReady = chooseTemplate && await waitFor(`Boolean([...document.querySelectorAll(${JSON.stringify(sectionSelector)}+' form')[1]?.querySelectorAll('select option')??[]].some(option=>option.value===${JSON.stringify(credentials[role].email)}))`);
    if (!optionReady) throw new Error(`The assignment UI did not load the ${role} template's matching user list.`);
    browserActionContext = `assign ${role} template: select user`;
    const chooseUser = await evaluate(`(()=>{
      const form=document.querySelector(${JSON.stringify(sectionSelector)})?.querySelectorAll('form')[1];
      const select=form?.querySelectorAll('select')[1];if(!select)return false;
      Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set.call(select,${JSON.stringify(credentials[role].email)});
      select.dispatchEvent(new Event('change',{bubbles:true}));return true;
    })()`);
    browserActionContext = `assign ${role} template: wait for department choices`;
    if (!chooseUser || !await waitFor(`Boolean([...document.querySelectorAll(${JSON.stringify(sectionSelector)}+' form')[1]?.querySelectorAll('fieldset label')??[]].find(label=>label.textContent.trim()===${JSON.stringify(departmentName)}))`)) {
      throw new Error(`The People UI did not expose ${role}'s current synthetic department membership.`);
    }
    const expiryValue = expiresInMinutes === null ? null : (() => {
      const date = new Date(Date.now() + expiresInMinutes * 60_000);
      date.setSeconds(0, 0);
      return new Date(date.getTime() - date.getTimezoneOffset() * 60_000).toISOString().slice(0, 16);
    })();
    browserActionContext = `assign ${role} template: submit scoped assignment`;
    browserActionContext = `assign ${role} template: click submit`;
    const submitted = await evaluate(`(()=>{
      const form=document.querySelector(${JSON.stringify(sectionSelector)})?.querySelectorAll('form')[1];
      const expiry=form?.querySelector('input[type="datetime-local"]');
      if(${JSON.stringify(expiryValue)}!==null){if(!expiry)return false;Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(expiry,${JSON.stringify(expiryValue)});expiry.dispatchEvent(new Event('input',{bubbles:true}));expiry.dispatchEvent(new Event('change',{bubbles:true}))}
      const label=[...(form?.querySelectorAll('fieldset label')??[])].find(item=>item.textContent.trim()===${JSON.stringify(departmentName)});
      const checkbox=label?.querySelector('input[type=checkbox]');if(!checkbox)return false;if(!checkbox.checked)checkbox.click();
      const button=[...(form?.querySelectorAll('button')??[])].find(item=>item.textContent.trim()==='Assign template');
      if(!button||button.disabled)return false;button.click();return true;
    })()`);
    browserActionContext = `assign ${role} template: wait for saved assignment`;
    if (!submitted || !await waitFor(`Boolean([...document.querySelectorAll(${JSON.stringify(sectionSelector)}+' table tbody tr')].find(row=>row.textContent.includes(${JSON.stringify(credentials[role].email)})&&row.textContent.includes(${JSON.stringify(template.name)})))`)) {
      throw new Error(`Admin could not assign the ${role} template through the scoped People UI.`);
    }
    const assignments = (await expect("GET", "/api/admin/role-template-assignments", 200)).assignments;
    const assignment = assignments.find((item) => item.email === credentials[role].email && item.role_template_id === template.id);
    if (!assignment || JSON.stringify(assignment.department_ids) !== JSON.stringify([group.id]) || assignment.grant_count !== 1 ||
        !assignment.expires_at || Date.parse(assignment.expires_at) <= Date.now() ||
        Date.parse(assignment.expires_at) > Date.now() + 366 * 24 * 60 * 60 * 1000) {
      throw new Error("The browser role assignment did not persist one department-scoped grant with a bounded future expiry.");
    }
    if (expiresInMinutes !== null && Date.parse(assignment.expires_at) - Date.now() > (expiresInMinutes + 1) * 60_000) {
      throw new Error("The role expiry input did not save the requested short synthetic access period.");
    }
    return assignment;
  };

  const employeeAssignment = await assignInUi(reviewer, "employee");
  const managerAssignment = await assignInUi(publisher, "manager");
  evidence.push("C04_CUSTOM_ROLE_ADMIN_CREATE_ASSIGN_UI=PASS");

  await signOut();
  await signIn("manager");
  const managerDenied = await request("GET", "/api/admin/role-templates");
  if (![401, 403].includes(managerDenied.status) || await evaluate(`Boolean([...document.querySelectorAll('button.nav-item')].some(item=>item.textContent.trim()==='People & access'))`)) {
    throw new Error("The Manager could access the custom-role administration surface.");
  }
  await signOut();
  await signIn("employee");
  const employeeDenied = await request("GET", "/api/admin/role-templates");
  if (![401, 403].includes(employeeDenied.status)) throw new Error("The Employee could access the custom-role administration API.");
  await signOut();

  await signIn("admin");
  const claim = await expect("POST", "/api/claims", 201, {
    kind: "proposal", statement: `${prefix} approval lifecycle evidence`, category: "general", scope_group_id: group.id,
    sources: [{ kind: "note", note: "Synthetic browser role-template acceptance evidence." }],
  });
  await signOut();
  await signIn("employee");
  const review = await expect("POST", `/api/governance/claims/${claim.id}/decisions`, 200, {
    stage: "review", rationale: "The assigned employee reviewer checked this synthetic claim.",
  });
  if (review.stage !== "review") throw new Error("The scoped Employee review did not complete.");
  await signOut();
  await signIn("manager");
  const publish = await expect("POST", `/api/governance/claims/${claim.id}/decisions`, 200, {
    stage: "publish", rationale: "The assigned department Manager approved the reviewed synthetic claim.",
  });
  if (publish.stage !== "publish") throw new Error("The scoped Manager publish approval did not complete.");
  evidence.push("C04_MANAGER_EMPLOYEE_CUSTOM_ROLE_APPROVAL_SCOPE=PASS");

  await signOut();
  await signIn("admin");
  await navigate("People & access", "People & access");
  const revokeInUi = async (assignment, email, templateName) => {
    browserActionContext = `revoke ${templateName} from ${email} through UI`;
    const rowReady = await waitFor(`Boolean([...document.querySelectorAll(${JSON.stringify(sectionSelector)}+' table tbody tr')].some(row=>row.textContent.includes(${JSON.stringify(email)})&&row.textContent.includes(${JSON.stringify(templateName)})))`);
    const clicked = rowReady && await evaluate(`(()=>{
      const section=document.querySelector(${JSON.stringify(sectionSelector)});
      const row=[...(section?.querySelectorAll('table tbody tr')??[])].find(item=>item.textContent.includes(${JSON.stringify(email)})&&item.textContent.includes(${JSON.stringify(templateName)}));
      const button=[...(row?.querySelectorAll('button')??[])].find(item=>item.textContent.trim()==='Revoke');
      if(!button)return false;button.click();return true;
    })()`);
    const dialog = clicked && await waitFor(`Boolean(document.querySelector('[role=dialog] button.btn-danger'))`);
    const confirmed = dialog && await evaluate(`(()=>{const d=document.querySelector('[role=dialog]');const b=[...(d?.querySelectorAll('button')??[])].find(item=>item.textContent.trim()==='Revoke assignment');if(!b)return false;b.click();return true})()`);
    const removedFromUi = confirmed && await waitFor(`![...document.querySelectorAll(${JSON.stringify(sectionSelector)}+' table tbody tr')].some(row=>row.textContent.includes(${JSON.stringify(email)})&&row.textContent.includes(${JSON.stringify(templateName)}))`);
    if (!removedFromUi) {
      const visibleAssignment = await evaluate(`([...document.querySelectorAll(${JSON.stringify(sectionSelector)}+' table tbody tr')].map(row=>row.textContent.trim()).filter(text=>text.includes(${JSON.stringify(email)})))`);
      throw new Error(`Admin could not revoke ${email}'s custom role from the People UI (row=${rowReady}, button=${clicked}, dialog=${dialog}, confirm=${confirmed}, removed=${removedFromUi}, rows=${JSON.stringify(visibleAssignment)}).`);
    }
    const active = (await expect("GET", "/api/admin/role-template-assignments", 200)).assignments;
    if (active.some((item) => item.id === assignment.id)) throw new Error("A revoked custom-role assignment remained active in PostgreSQL.");
  };
  await revokeInUi(employeeAssignment, credentials.employee.email, reviewer.name);
  const refusalClaim = await expect("POST", "/api/claims", 201, {
    kind: "proposal", statement: `${prefix} post-revocation refusal evidence`, category: "general", scope_group_id: group.id,
    sources: [{ kind: "note", note: "Synthetic post-revocation authorization check." }],
  });
  await signOut();
  await signIn("employee");
  const refusedDecision = await request("POST", `/api/governance/claims/${refusalClaim.id}/decisions`, {
    stage: "review", rationale: "The old role must no longer authorize this decision.",
  });
  if (refusedDecision.status !== 403) throw new Error(`A revoked employee role still authorized review (HTTP ${refusedDecision.status}).`);
  await signOut();
  await signIn("admin");
  await navigate("People & access", "People & access");
  await revokeInUi(managerAssignment, credentials.manager.email, publisher.name);

  const expiringReviewer = await createTemplateInUi(`${prefix} Expiring Employee Review`, "employee", "knowledge.review");
  const expiringAssignment = await assignInUi(expiringReviewer, "employee", 3);
  const activeWindowClaim = await expect("POST", "/api/claims", 201, {
    kind: "proposal", statement: `${prefix} temporary grant active evidence`, category: "general", scope_group_id: group.id,
    sources: [{ kind: "note", note: "Synthetic evidence for active temporary role access." }],
  });
  const expiredWindowClaim = await expect("POST", "/api/claims", 201, {
    kind: "proposal", statement: `${prefix} expired role refusal evidence`, category: "general", scope_group_id: group.id,
    sources: [{ kind: "note", note: "Synthetic evidence for expired role access." }],
  });
  await signOut();
  await signIn("employee");
  const activeTemporaryDecision = await expect("POST", `/api/governance/claims/${activeWindowClaim.id}/decisions`, 200, {
    stage: "review", rationale: "Temporary access is valid before its recorded expiry.",
  });
  if (activeTemporaryDecision.stage !== "review") throw new Error("A valid temporary role assignment could not review within its time window.");
  const remainingMs = Date.parse(expiringAssignment.expires_at) - Date.now();
  if (remainingMs > 0) await delay(remainingMs + 250);
  const expiredDecision = await request("POST", `/api/governance/claims/${expiredWindowClaim.id}/decisions`, {
    stage: "review", rationale: "Expired temporary access must no longer authorize review.",
  });
  if (expiredDecision.status !== 403) throw new Error(`An expired role template still authorized review (HTTP ${expiredDecision.status}).`);
  evidence.push("C04_CUSTOM_ROLE_EXPIRED_GRANT_API_DENIAL=PASS");
  await signOut();
  await signIn("admin");
  await navigate("People & access", "People & access");
  const expiredRowVisible = await waitFor(`Boolean([...document.querySelectorAll(${JSON.stringify(sectionSelector)}+' table tbody tr')].find(row=>row.textContent.includes(${JSON.stringify(credentials.employee.email)})&&row.textContent.includes(${JSON.stringify(expiringReviewer.name)})&&row.textContent.includes('Expired')))`);
  if (!expiredRowVisible) throw new Error("The Admin role-template table did not show the assignment as expired after its deadline.");
  await revokeInUi(expiringAssignment, credentials.employee.email, expiringReviewer.name);

  const summary = databaseRoleTemplateSummary(prefix);
  if (summary !== "3,3,3,3,3,3,3") throw new Error(`Custom-role database and audit evidence was incomplete: ${summary}.`);
  evidence.push("C04_CUSTOM_ROLE_REVOKE_UI_AND_PERMISSION_REFRESH=PASS");
  evidence.push("C04_CUSTOM_ROLE_EXPIRED_STATUS_AND_REVOCATION_UI=PASS");
  evidence.push(`C04_CUSTOM_ROLE_POSTGRES_COUNTS_TEMPLATES_ASSIGNMENTS_GRANTS_APPROVALS_CREATED_ASSIGNED_REVOKED=${summary}`);
}

async function auditRoleTemplateVersionApproval() {
  if (typeof credentials?.reviewer?.email !== "string" || typeof credentials?.reviewer?.password !== "string") {
    throw new Error("The protected fixture does not contain the independent synthetic Admin reviewer.");
  }
  const prefix = `QA Browser Role Version ${Date.now()}`;
  const templateName = `${prefix} Review`;
  await signIn("admin");
  const users = (await expect("GET", "/api/admin/users", 200)).users;
  const employee = users.find((row) => row.email.toLowerCase() === credentials.employee.email.toLowerCase());
  if (!employee) throw new Error("The protected synthetic Employee is absent from the candidate database.");
  const department = await expect("POST", "/api/admin/access/groups", 201, { name: `${prefix} Department`, kind: "department" });
  const membership = await request("PATCH", `/api/admin/users/${employee.id}`, { role: "employee", department_ids: [department.id] });
  if (membership.status !== 200) throw new Error("Could not establish the synthetic Employee department for approval acceptance.");

  await navigate("People & access", "People & access");
  const section = "section[aria-labelledby='role-template-heading']";
  browserActionContext = "create role-template version test fixture in Admin UI";
  const created = await evaluate(`(()=>{const root=document.querySelector(${JSON.stringify(section)});const form=root?.querySelectorAll('form')[0];const inputs=[...(form?.querySelectorAll('input')??[])];if(!form||inputs.length<2)return false;const set=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;set.call(inputs[0],${JSON.stringify(templateName)});inputs[0].dispatchEvent(new Event('input',{bubbles:true}));set.call(inputs[1],'Synthetic approval test.');inputs[1].dispatchEvent(new Event('input',{bubbles:true}));const category=form.querySelectorAll('fieldset')[1];for(const label of category?.querySelectorAll('label')??[]){const box=label.querySelector('input');const wanted=label.textContent.trim()==='financial';if(box&&box.checked!==wanted)box.click()}const button=form.querySelector('button.btn-primary');if(!button||button.disabled)return false;button.click();return true})()`);
  if (!created || !await waitFor(`Boolean([...document.querySelectorAll(${JSON.stringify(section)}+' table tbody tr')].some(row=>row.textContent.includes(${JSON.stringify(templateName)})))`)) {
    throw new Error("Admin UI failed to create the version-approval test template.");
  }
  const template = (await expect("GET", "/api/admin/role-templates", 200)).templates.find((row) => row.name === templateName);
  if (!template || template.version !== 1 || !template.is_active) throw new Error("The initial immutable version was not active.");
  const assignment = await expect("POST", `/api/admin/role-templates/${template.id}/assignments`, 201, {
    email: credentials.employee.email,
    department_ids: [department.id],
    expires_at: new Date(Date.now() + 30 * 60_000).toISOString(),
  });
  if (assignment.grant_count !== 1) throw new Error("The initial synthetic Employee grant did not apply.");
  const beforeClaim = await expect("POST", "/api/claims", 201, {
    kind: "proposal", statement: `${prefix} review available before version approval`, category: "financial", scope_group_id: department.id,
    sources: [{ kind: "note", note: "Synthetic two-admin role version acceptance evidence." }],
  });
  await signOut();
  await signIn("employee");
  const beforeReview = await expect("POST", `/api/governance/claims/${beforeClaim.id}/decisions`, 200, {
    stage: "review", rationale: "Initial Finance reviewer grant is active before approved version change.",
  });
  if (beforeReview.stage !== "review") throw new Error("The initial version's scoped Employee review failed.");
  await signOut();

  await signIn("admin");
  await navigate("People & access", "People & access");
  const proposedFromUi = await evaluate(`(()=>{const row=[...document.querySelectorAll(${JSON.stringify(section)}+' table tbody tr')].find(item=>item.textContent.includes(${JSON.stringify(templateName)}));const button=[...(row?.querySelectorAll('button')??[])].find(item=>item.textContent.trim()==='Propose next version');if(!button)return false;button.click();return true})()`);
  if (!proposedFromUi || !await waitFor(`Boolean([...document.querySelectorAll(${JSON.stringify(section)}+' h3')].some(item=>item.textContent.includes(${JSON.stringify(templateName)})))`)) {
    throw new Error("Admin UI did not open the next-version proposal editor.");
  }
  const submitted = await evaluate(`(()=>{const root=document.querySelector(${JSON.stringify(section)});const form=root?.querySelectorAll('form')[1];if(!form)return false;const input=form.querySelector('input');if(!input)return false;const set=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;set.call(input,'Reviewed Finance publishing permissions.');input.dispatchEvent(new Event('input',{bubbles:true}));for(const label of form.querySelectorAll('fieldset')[0]?.querySelectorAll('label')??[]){const box=label.querySelector('input');const wanted=['knowledge.review','knowledge.publish'].includes(label.textContent.trim());if(box&&box.checked!==wanted)box.click()}for(const label of form.querySelectorAll('fieldset')[1]?.querySelectorAll('label')??[]){const box=label.querySelector('input');const wanted=label.textContent.trim()==='financial';if(box&&box.checked!==wanted)box.click()}const button=[...(form.querySelectorAll('button')??[])].find(item=>item.textContent.includes('Submit for separate Admin approval'));if(!button||button.disabled)return false;button.click();return true})()`);
  if (!submitted || !await waitFor(`Boolean([...document.querySelectorAll(${JSON.stringify(section)}+' table tbody tr')].some(row=>row.textContent.includes(${JSON.stringify(templateName)})&&row.textContent.includes('pending')))`)) {
    throw new Error("Admin UI failed to submit the changed template as a pending proposal.");
  }
  const proposal = (await expect("GET", "/api/admin/role-template-change-requests", 200)).requests.find((row) => row.template_name === templateName && row.status === "pending");
  if (!proposal || proposal.proposed_permissions.length !== 2) throw new Error("The proposed version did not persist the changed action scope.");
  const selfApproval = await request("POST", `/api/admin/role-template-change-requests/${proposal.id}/decision`, {
    decision: "approve", rationale: "Self approval must remain forbidden.",
  });
  if (selfApproval.status !== 403) throw new Error(`The requester could self-approve a role change (HTTP ${selfApproval.status}).`);
  evidence.push("C04_ROLE_VERSION_SELF_APPROVAL_DENIED=PASS");
  await signOut();

  await signInAccount(credentials.reviewer, "admin");
  await navigate("People & access", "People & access");
  const pendingRowReady = await waitFor(`Boolean([...document.querySelectorAll(${JSON.stringify(section)}+' table tbody tr')].some(row=>row.textContent.includes(${JSON.stringify(templateName)})&&row.textContent.includes('pending')))`);
  const approvedFromUi = pendingRowReady && await evaluate(`(()=>{const row=[...document.querySelectorAll(${JSON.stringify(section)}+' table tbody tr')].find(item=>item.textContent.includes(${JSON.stringify(templateName)})&&item.textContent.includes('pending'));const input=row?.querySelector('input[maxlength="2000"]');if(!input)return false;Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,'Independent Admin review approved the Finance permission change.');input.dispatchEvent(new Event('input',{bubbles:true}));const button=[...(row?.querySelectorAll('button')??[])].find(item=>item.textContent.trim()==='Approve');if(!button||button.disabled)return false;button.click();return true})()`);
  const approvalVisible = approvedFromUi && await waitFor(`Boolean([...document.querySelectorAll(${JSON.stringify(section)}+' table tbody tr')].some(row=>row.textContent.includes(${JSON.stringify(templateName)})&&row.textContent.includes('approved')))`);
  if (!approvalVisible) throw new Error("A distinct Admin could not approve the pending template version through the UI.");
  const approved = (await expect("GET", "/api/admin/role-template-change-requests", 200)).requests.find((row) => row.id === proposal.id);
  const versions = (await expect("GET", "/api/admin/role-templates", 200)).templates.filter((row) => row.name === templateName);
  const v1 = versions.find((row) => row.version === 1);
  const v2 = versions.find((row) => row.version === 2);
  if (approved?.status !== "approved" || approved.decided_by_email !== credentials.reviewer.email || !v1 || v1.is_active || !v2 || !v2.is_active || v2.supersedes_id !== v1.id) {
    throw new Error("Admin approval did not activate the immutable next version with correct approver history.");
  }
  const dbSummary = databaseRoleTemplateVersionSummary(templateName, proposal.id);
  if (dbSummary !== "2,1,1,1,1,1,1,1,1") throw new Error(`Approved role-version database checks failed: ${dbSummary}.`);
  evidence.push("C04_ROLE_VERSION_SEPARATE_ADMIN_APPROVAL_UI=PASS");
  evidence.push(`C04_ROLE_VERSION_POSTGRES_HISTORY_AND_OLD_GRANT_REVOCATION=${dbSummary}`);

  const afterClaim = await expect("POST", "/api/claims", 201, {
    kind: "proposal", statement: `${prefix} old grant denied after version approval`, category: "financial", scope_group_id: department.id,
    sources: [{ kind: "note", note: "Synthetic old-grant revocation check." }],
  });
  await signOut();
  await signIn("employee");
  const denied = await request("POST", `/api/governance/claims/${afterClaim.id}/decisions`, {
    stage: "review", rationale: "Superseded template grants must no longer authorize review.",
  });
  if (denied.status !== 403) throw new Error(`Superseded version remained authorized after approval (HTTP ${denied.status}).`);
  evidence.push("C04_ROLE_VERSION_OLD_PERMISSION_REVOKED=PASS");
}

async function auditOfflineLicenseAdminUI() {
  const keyPath = process.env.INFRASPINE_QA_LICENSE_SIGNING_KEY_FILE;
  if (!keyPath) throw new Error("Set the protected synthetic license signing-key file for C03 browser acceptance.");
  const keyPair = JSON.parse(readFileSync(keyPath, "utf8").replace(/^\uFEFF/, ""));
  if (typeof keyPair?.privateKey !== "string" || !keyPair.privateKey.includes("BEGIN PRIVATE KEY")) {
    throw new Error("The protected C03 fixture does not contain a valid synthetic private signer.");
  }
  const privateKey = createPrivateKey(keyPair.privateKey);
  let deploymentId = "";
  const canonicalLicense = (licenseId, seatLimit, features) => {
    const issuedAt = new Date(Math.floor(Date.now() / 1000) * 1000).toISOString().replace(/\.000Z$/, "Z");
    const expiresAt = new Date(Date.parse(issuedAt) + 365 * 24 * 60 * 60 * 1000).toISOString().replace(/\.000Z$/, "Z");
    const payload = {
      product: "infraspine-ai",
      license_id: licenseId,
      customer: "Synthetic License Acceptance",
      deployment_id: deploymentId,
      issued_at: issuedAt,
      expires_at: expiresAt,
      seat_limit: seatLimit,
      features,
    };
    const canonical = JSON.stringify(Object.fromEntries(Object.entries(payload).sort(([left], [right]) => left.localeCompare(right))));
    const signature = signSignature(null, Buffer.from(canonical, "utf8"), privateKey).toString("base64");
    return { payload, signature };
  };

  await signIn("admin");
  const initial = await expect("GET", "/api/admin/deployment", 200);
  if (!initial.license?.enforcement_enabled || initial.license.status !== "missing") {
    throw new Error("The isolated candidate did not start with license enforcement enabled and no installed license.");
  }
  deploymentId = initial.license.deployment_id;
  await navigate("Company setup", "Company setup");
  const findLicenseSection = `([...document.querySelectorAll('section.card')].find(section=>section.querySelector('h2')?.textContent.trim()==='License'))`;
  const fillAndSubmit = async (envelope) => {
    const filled = await evaluate(`(()=>{const section=${findLicenseSection};const textarea=section?.querySelector('textarea');if(!textarea)return false;Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(textarea,${JSON.stringify(JSON.stringify(envelope))});textarea.dispatchEvent(new Event('input',{bubbles:true}));return true})()`);
    const enabled = filled && await waitFor(`(()=>{const section=${findLicenseSection};const button=section?.querySelector('button.btn-primary');return Boolean(button&&!button.disabled)})()`);
    const submitted = enabled && await evaluate(`(()=>{const section=${findLicenseSection};const button=section?.querySelector('button.btn-primary');if(!button||button.disabled)return false;button.click();return true})()`);
    return Boolean(submitted);
  };
  const statusFor = async () => expect("GET", "/api/admin/deployment", 200);

  browserActionContext = "install first synthetic offline license through Company setup UI";
  const firstLicense = canonicalLicense(`QA-UI-${Date.now()}-1`, 10, ["rag", "reports"]);
  if (!await fillAndSubmit(firstLicense)) throw new Error("The Admin UI could not submit the first synthetic signed license.");
  const firstVisible = await waitFor(`Boolean([...document.querySelectorAll('.deployment-license-details dd')].some(item=>item.textContent.trim()===${JSON.stringify(firstLicense.payload.license_id)}))`);
  const firstStatus = await statusFor();
  if (!firstVisible || firstStatus.license.status !== "valid" || firstStatus.license.license_id !== firstLicense.payload.license_id ||
      firstStatus.license.seat_limit !== 10 || JSON.stringify(firstStatus.license.features) !== JSON.stringify(["rag", "reports"])) {
    throw new Error("The first signed offline license did not install with the expected entitlements.");
  }
  evidence.push("C03_ADMIN_UI_SIGNED_LICENSE_INSTALL_AND_ENTITLEMENTS=PASS");

  browserActionContext = "reject tampered synthetic license without replacing the valid license";
  const tampered = structuredClone(firstLicense);
  tampered.payload.seat_limit = 99;
  if (!await fillAndSubmit(tampered)) throw new Error("The Admin UI could not submit a tampered license test envelope.");
  const tamperError = await waitFor(`Boolean(document.querySelector('.banner-error')?.textContent.toLowerCase().includes('signature'))`);
  const afterTamper = await statusFor();
  if (!tamperError || afterTamper.license.status !== "valid" || afterTamper.license.license_id !== firstLicense.payload.license_id) {
    throw new Error("Tampering was not refused while the prior valid license remained installed.");
  }
  evidence.push("C03_INVALID_SIGNATURE_REFUSED_WITHOUT_OVERWRITE=PASS");

  browserActionContext = "renew the synthetic offline license in Company setup UI";
  const renewedLicense = canonicalLicense(`QA-UI-${Date.now()}-2`, 20, ["rag", "reports", "automation"]);
  if (!await fillAndSubmit(renewedLicense)) throw new Error("The Admin UI could not submit the synthetic renewed license.");
  const renewedVisible = await waitFor(`Boolean([...document.querySelectorAll('.deployment-license-details dd')].some(item=>item.textContent.trim()===${JSON.stringify(renewedLicense.payload.license_id)}))`);
  const renewedStatus = await statusFor();
  if (!renewedVisible || renewedStatus.license.status !== "valid" || renewedStatus.license.license_id !== renewedLicense.payload.license_id ||
      renewedStatus.license.seat_limit !== 20 || JSON.stringify(renewedStatus.license.features) !== JSON.stringify(["rag", "reports", "automation"])) {
    throw new Error("The renewed signed license did not replace the previous entitlement envelope.");
  }
  evidence.push("C03_ADMIN_UI_SIGNED_LICENSE_RENEWAL=PASS");
  await signOut();
}

async function auditRoleSurfaces(role) {
  const labels = await evaluate(`([...document.querySelectorAll('button.nav-item')].map(button=>button.textContent.trim()).filter(Boolean))`);
  if (!Array.isArray(labels) || labels.length === 0) throw new Error(`${role} has no reachable navigation pages.`);
  const viewports = [
    { name: "desktop", width: 1440, height: 900, mobile: false },
    // Use a narrow CSS viewport without mobile scaling. This keeps layout media queries in CSS pixels
    // and avoids Chromium's emulated visual viewport reporting a 1560px layout width on high-DPI hosts.
    { name: "mobile", width: 390, height: 844, mobile: false },
  ];
  const observations = [];
  for (const viewport of viewports) {
    await cdp.command("Emulation.setDeviceMetricsOverride", {
      width: viewport.width, height: viewport.height, deviceScaleFactor: 1, mobile: viewport.mobile,
    });
    for (const label of labels) {
      const clicked = await evaluate(`(()=>{const b=[...document.querySelectorAll('button.nav-item')].find(x=>x.textContent.trim()===${JSON.stringify(label)});if(!b)return false;b.click();return true})()`);
      const ready = `(()=>{const active=document.querySelector('button.nav-item-active')?.textContent?.trim();return active===${JSON.stringify(label)}&&Boolean(document.querySelector('main.main'))})()`;
      if (!clicked || !await waitFor(ready, 8_000)) throw new Error(`${role} could not open ${label} at ${viewport.name} viewport.`);
      await delay(350);
      await waitFor(`!document.querySelector('main.main .loading')`, 5_000);
      const state = await evaluate(`(()=>{const main=document.querySelector('main.main');const title=main?.querySelector('h1')?.textContent?.trim()??'';const width=window.innerWidth;const bodyWidth=document.body.scrollWidth;const selectors=['html','body','.shell','.sidebar','.nav','main.main','.people-section','.table-wrap'];const layout=selectors.map(sel=>{const el=document.querySelector(sel);if(!el)return {sel,missing:true};const r=el.getBoundingClientRect(),s=getComputedStyle(el);return {sel,left:Math.round(r.left),right:Math.round(r.right),scrollWidth:el.scrollWidth,clientWidth:el.clientWidth,overflowX:s.overflowX}});const offenders=[...document.querySelectorAll('body *')].filter(el=>!el.closest('.nav')&&!el.closest('.table-wrap')).map(el=>{const r=el.getBoundingClientRect();const chain=[];let p=el;while(p&&chain.length<7){const q=p.getBoundingClientRect(),s=getComputedStyle(p);chain.push({tag:p.tagName.toLowerCase(),className:typeof p.className==='string'?p.className:'',left:Math.round(q.left),right:Math.round(q.right),overflowX:s.overflowX,scrollWidth:p.scrollWidth,clientWidth:p.clientWidth});p=p.parentElement}return {tag:el.tagName.toLowerCase(),className:typeof el.className==='string'?el.className:'',left:Math.round(r.left),right:Math.round(r.right),width:Math.round(r.width),scrollWidth:el.scrollWidth,chain}}).filter(x=>x.right>width+2).sort((a,b)=>b.right-a.right).slice(0,3);return {title,buttons:main?.querySelectorAll('button').length??0,forms:main?.querySelectorAll('form').length??0,loading:Boolean(main?.querySelector('.loading')),error:Boolean(main?.querySelector('.banner-error')),viewportWidth:width,bodyWidth,documentWidth:document.documentElement.scrollWidth,visualWidth:Math.round(window.visualViewport?.width??0),mobileMedia:matchMedia('(max-width: 960px)').matches,overflow:Math.max(0,bodyWidth-width),layout,offenders,main:Boolean(main)}})()`);
      if (!state?.main) throw new Error(`${role} page shell disappeared on ${label}.`);
      observations.push({ role, viewport: viewport.name, label, ...state });
    }
  }
  await cdp.command("Emulation.clearDeviceMetricsOverride");
  const overflows = observations.filter((item) => item.overflow > 1);
  const errors = observations.filter((item) => item.error);
  const loading = observations.filter((item) => item.loading);
  const missingTitles = observations.filter((item) => !item.title);
  for (const item of overflows) evidence.push(`PAGE_SURFACE_DEFECT role=${item.role} viewport=${item.viewport} page=${item.label} horizontal_overflow_px=${item.overflow} viewport_width=${item.viewportWidth} document_width=${item.documentWidth} layout=${JSON.stringify(item.layout)}`);
  for (const item of errors) evidence.push(`PAGE_SURFACE_DEFECT role=${item.role} viewport=${item.viewport} page=${item.label} error_banner=true`);
  for (const item of loading) evidence.push(`PAGE_SURFACE_PENDING role=${item.role} viewport=${item.viewport} page=${item.label} spinner_after_wait=true`);
  for (const item of missingTitles) evidence.push(`PAGE_SURFACE_DEFECT role=${item.role} viewport=${item.viewport} page=${item.label} missing_h1=true`);
  evidence.push(`PAGE_SURFACE_AUDIT role=${role} pages=${labels.length} desktop=${observations.filter((item)=>item.viewport==='desktop').length} mobile=${observations.filter((item)=>item.viewport==='mobile').length} overflow=${overflows.length} error_banners=${errors.length} loading=${loading.length} missing_h1=${missingTitles.length}`);
  for (const item of observations) evidence.push(`PAGE_SURFACE role=${role} viewport=${item.viewport} page=${item.label} title=${item.title || "(no heading)"} buttons=${item.buttons} forms=${item.forms}`);
  if (overflows.length || errors.length || loading.length || missingTitles.length) {
    throw new Error(`${role} has ${overflows.length} viewport overflow(s), ${errors.length} page error(s), ${loading.length} stuck page(s), and ${missingTitles.length} page(s) without an H1.`);
  }
}

function makeSyntheticPdf(contents) {
  const objects = [
    "<< /Type /Catalog /Pages 2 0 R >>",
    "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
    "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
    "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    `<< /Length ${Buffer.byteLength(contents, "ascii")} >>\nstream\n${contents}\nendstream`,
  ];
  let pdf = "%PDF-1.4\n";
  const offsets = [0];
  for (let i = 0; i < objects.length; i++) {
    offsets.push(Buffer.byteLength(pdf, "ascii"));
    pdf += `${i + 1} 0 obj\n${objects[i]}\nendobj\n`;
  }
  const xref = Buffer.byteLength(pdf, "ascii");
  pdf += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n`;
  for (const offset of offsets.slice(1)) pdf += `${String(offset).padStart(10, "0")} 00000 n \n`;
  pdf += `trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`;
  return Buffer.from(pdf, "ascii");
}

async function uploadSyntheticPdf() {
  const contents = "BT /F1 12 Tf 72 720 Td (Synthetic QA recovery token ZEPHYR 42. This report explains the safe restore procedure.) Tj ET";
  const base64 = makeSyntheticPdf(contents).toString("base64");
  const result = await evaluate(`(async()=>{const binary=atob(${JSON.stringify(base64)});const bytes=Uint8Array.from(binary,c=>c.charCodeAt(0));const form=new FormData();form.append('file',new Blob([bytes],{type:'application/pdf'}),'qa-browser-recovery.pdf');form.append('visibility','shared');const response=await fetch('/api/rag/documents',{method:'POST',credentials:'same-origin',headers:{'X-Requested-With':'XMLHttpRequest'},body:form});let json=null;try{json=await response.json()}catch{}return {status:response.status,id:json?.id}})()`);
  if (result?.status !== 202 || typeof result.id !== "string") throw new Error(`Synthetic PDF upload returned HTTP ${result?.status}.`);
  documentIds.upload = result.id;
  const encoded = JSON.stringify(result.id);
  let state;
  const until = Date.now() + 90_000;
  while (Date.now() < until) {
    state = await expect("GET", `/api/rag/documents/${result.id}`, 200);
    if (state.status === "ready" && state.searchable && state.chunk_count > 0) break;
    if (state.status === "failed") throw new Error(`Synthetic PDF worker failed with code ${state.error_code ?? "unknown"}.`);
    await delay(1_000);
  }
  if (state?.status !== "ready" || !state.searchable || state.chunk_count < 1) {
    throw new Error("Synthetic PDF did not reach searchable ready state within 90 seconds.");
  }
  evidence.push(`UPLOAD_WORKER_READY=PASS pages=${state.page_count} chunks=${state.chunk_count}`);
  return result.id;
}

async function auditKnowledgeConflictAcceptance() {
  browserActionContext = "Admin synthetic conflict source setup";
  await signIn("admin");
  const managerEmail = credentials.manager.email;
  const employeeEmail = credentials.employee.email;
  const currentGrants = (await expect("GET", "/api/admin/access/grants", 200)).grants;
  const findGrant = (email, permission) => currentGrants.find((grant) =>
    grant.email.toLowerCase() === email.toLowerCase() && grant.permission === permission &&
    grant.category === "policy" && grant.scope_group_id === null && !grant.revoked_at);
  let reviewGrant = findGrant(managerEmail, "knowledge.review");
  if (!reviewGrant) {
    reviewGrant = await expect("POST", "/api/admin/access/grants", 201, {
      email: managerEmail, permission: "knowledge.review", category: "policy",
    });
  }
  if (!findGrant(employeeEmail, "knowledge.publish")) {
    await expect("POST", "/api/admin/access/grants", 201, {
      email: employeeEmail, permission: "knowledge.publish", category: "policy",
    });
  }

  const sourceText = "Synthetic QA conflict evidence: billing policy refund timelines require comparison against the cited source.";
  const pdf = makeSyntheticPdf(`BT /F1 12 Tf 72 720 Td (${sourceText}) Tj ET`).toString("base64");
  const uploaded = await evaluate(`(async()=>{const binary=atob(${JSON.stringify(pdf)});const bytes=Uint8Array.from(binary,c=>c.charCodeAt(0));const form=new FormData();form.append('file',new Blob([bytes],{type:'application/pdf'}),'qa-conflict-policy.pdf');form.append('visibility','shared');const response=await fetch('/api/rag/documents',{method:'POST',credentials:'same-origin',headers:{'X-Requested-With':'XMLHttpRequest'},body:form});let json=null;try{json=await response.json()}catch{}return {status:response.status,id:json?.id}})()`);
  if (uploaded?.status !== 202 || typeof uploaded.id !== "string") throw new Error(`Conflict source upload returned HTTP ${uploaded?.status}.`);
  const until = Date.now() + 90_000;
  let source;
  while (Date.now() < until) {
    source = await expect("GET", `/api/rag/documents/${uploaded.id}`, 200);
    if (source.status === "ready" && source.searchable && source.chunk_count > 0) break;
    if (source.status === "failed") throw new Error(`Conflict source worker failed with code ${source.error_code ?? "unknown"}.`);
    await delay(1_000);
  }
  if (source?.status !== "ready" || !source.searchable) throw new Error("Conflict source did not become searchable.");
  const quote = databaseFirstChunk(uploaded.id).slice(0, 80);
  const createStatement = async (statement) => expect("POST", "/api/knowledge", 201, {
    statement, category: "policy", visibility: "shared", evidence: [{ document_id: uploaded.id, quote }],
  });
  const submit = async (entry) => {
    await expect("POST", `/api/knowledge/${entry.id}/submit`, 200);
    return entry.revisions[0].id;
  };

  const existingEntry = await createStatement("Customer refund requests are permitted within 30 days after the invoice date.");
  const existingRevisionId = await submit(existingEntry);
  browserActionContext = "Manager approval of original policy";
  await signOut();
  await signIn("manager");
  await expect("POST", `/api/governance/knowledge/${existingRevisionId}/decisions`, 200, {
    stage: "review", rationale: "Compared the original policy statement with its cited passage.",
  });
  await signOut();
  await signIn("employee");
  await expect("POST", `/api/governance/knowledge/${existingRevisionId}/decisions`, 200, {
    stage: "publish", rationale: "Approved the independently reviewed source-backed policy.",
  });

  browserActionContext = "Admin potential-conflict submission";
  await signOut();
  await signIn("admin");
  const submittedEntry = await createStatement("Customer refund requests are prohibited after 14 days from the invoice date.");
  const submittedRevisionId = await submit(submittedEntry);
  const conflictId = databaseOpenConflictForRevision(submittedRevisionId);

  browserActionContext = "Employee review-scope denial";
  await signOut();
  await signIn("employee");
  const employeeQueue = await expect("GET", "/api/governance/knowledge/conflicts", 200);
  if (employeeQueue.items.length !== 0) throw new Error("A publisher-only Employee saw the review conflict queue.");
  const employeeDecision = await request("POST", `/api/governance/knowledge/conflicts/${conflictId}/decisions`, {
    decision: "dismissed", rationale: "Publisher permission alone does not authorize conflict review.",
  });
  if (employeeDecision.status !== 403) throw new Error(`Publisher-only conflict decision returned HTTP ${employeeDecision.status}, expected 403.`);
  evidence.push("A07_CONFLICT_REVIEW_GRANT_BOUNDARY=PASS");

  browserActionContext = "Manager conflict approval UI";
  await signOut();
  await signIn("manager");
  await navigate("Approvals", "Approvals");
  if (!await waitFor(`(()=>{const section=[...document.querySelectorAll('.approval-section')].find(x=>x.querySelector('h2')?.textContent.trim()==='Potentially conflicting knowledge');const card=section?.querySelector('.approval-card');return Boolean(card&&card.textContent.includes('Customer refund requests are permitted within 30 days')&&card.textContent.includes('Customer refund requests are prohibited after 14 days')&&card.textContent.includes('qa-conflict-policy.pdf'))})()`)) {
    throw new Error("Approvals did not show both in-scope statements and their cited source to the Manager.");
  }
  const reason = "The cited policy passages support different refund deadlines and require correction.";
  const entered = await evaluate(`(()=>{const section=[...document.querySelectorAll('.approval-section')].find(x=>x.querySelector('h2')?.textContent.trim()==='Potentially conflicting knowledge');const card=section?.querySelector('.approval-card');const textarea=card?.querySelector('textarea');if(!textarea)return false;const set=Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set;set.call(textarea,${JSON.stringify(reason)});textarea.dispatchEvent(new Event('input',{bubbles:true}));return true})()`);
  const clicked = entered && await evaluate(`(()=>{const section=[...document.querySelectorAll('.approval-section')].find(x=>x.querySelector('h2')?.textContent.trim()==='Potentially conflicting knowledge');const card=section?.querySelector('.approval-card');const button=[...(card?.querySelectorAll('button')??[])].find(x=>x.textContent.trim()==='Confirm conflict');if(!button||button.disabled)return false;button.click();return true})()`);
  if (!clicked || !await waitFor(`(()=>{const section=[...document.querySelectorAll('.approval-section')].find(x=>x.querySelector('h2')?.textContent.trim()==='Potentially conflicting knowledge');return section?.textContent.includes('Conflict confirmed')&&section?.textContent.includes(${JSON.stringify(managerEmail)})&&section?.textContent.includes(${JSON.stringify(reason)})})()`)) {
    throw new Error("Manager conflict review did not persist the decision and rationale in the UI.");
  }
  const queue = await expect("GET", "/api/governance/knowledge/conflicts", 200);
  const reviewed = queue.items.find((item) => item.id === conflictId);
  if (!reviewed || reviewed.status !== "confirmed" || reviewed.review?.decision !== "confirmed" ||
      reviewed.submitted.revision_id !== submittedRevisionId || reviewed.existing.revision_id !== existingRevisionId) {
    throw new Error("The conflict API did not return the confirmed decision and both revision identities.");
  }
  const summary = databaseConflictSummary(conflictId);
  if (summary !== "confirmed,1,1") throw new Error("PostgreSQL conflict status, append-only review or audit evidence did not match.");
  evidence.push("A07_CONFLICT_SUBMIT_TO_SCOPED_MANAGER_UI_DECISION_AUDIT=PASS");
  evidence.push(`A07_CONFLICT_POSTGRES_SUMMARY=${summary}`);
}

async function auditPolicyProcessImpactAcceptance() {
  const managerEmail = credentials.manager.email;
  const employeeEmail = credentials.employee.email;
  await signIn("admin");
  const grants = (await expect("GET", "/api/admin/access/grants", 200)).grants;
  const hasGrant = (email, permission) => grants.some((grant) =>
    grant.email.toLowerCase() === email.toLowerCase() && grant.permission === permission &&
    grant.category === "policy" && grant.scope_group_id === null && !grant.revoked_at);
  if (!hasGrant(managerEmail, "knowledge.review")) {
    await expect("POST", "/api/admin/access/grants", 201, {
      email: managerEmail, permission: "knowledge.review", category: "policy",
    });
  }
  if (!hasGrant(employeeEmail, "knowledge.publish")) {
    await expect("POST", "/api/admin/access/grants", 201, {
      email: employeeEmail, permission: "knowledge.publish", category: "policy",
    });
  }
  const sourceText = "Synthetic policy source: customer refunds may be requested within thirty days of the invoice date; the operations team must record and review every request.";
  const sourcePdf = makeSyntheticPdf(`BT /F1 12 Tf 72 720 Td (${sourceText}) Tj ET`).toString("base64");
  const upload = await evaluate(`(async()=>{const binary=atob(${JSON.stringify(sourcePdf)});const bytes=Uint8Array.from(binary,c=>c.charCodeAt(0));const form=new FormData();form.append('file',new Blob([bytes],{type:'application/pdf'}),'qa-policy-impact-source.pdf');form.append('visibility','shared');const response=await fetch('/api/rag/documents',{method:'POST',credentials:'same-origin',headers:{'X-Requested-With':'XMLHttpRequest'},body:form});let json=null;try{json=await response.json()}catch{}return {status:response.status,id:json?.id}})()`);
  if (upload?.status !== 202 || typeof upload.id !== "string") throw new Error(`Policy-impact source upload returned HTTP ${upload?.status}.`);
  const deadline = Date.now() + 90_000;
  let source;
  while (Date.now() < deadline) {
    source = await expect("GET", `/api/rag/documents/${upload.id}`, 200);
    if (source.status === "ready" && source.searchable && source.chunk_count > 0) break;
    if (source.status === "failed") throw new Error(`Policy-impact source processing failed (${source.error_code ?? "unknown"}).`);
    await delay(1_000);
  }
  if (source?.status !== "ready" || !source.searchable) throw new Error("Policy-impact source did not become searchable.");
  const quote = databaseFirstChunk(upload.id).slice(0, 100);
  const createdPolicy = await expect("POST", "/api/knowledge", 201, {
    statement: "Refunds are allowed within 30 days of the invoice date.", category: "policy", visibility: "shared",
    evidence: [{ document_id: upload.id, quote }],
  });
  const revisionId = createdPolicy.revisions[0].id;
  await expect("POST", `/api/knowledge/${createdPolicy.id}/submit`, 200);
  await signOut();
  await signIn("manager");
  await expect("POST", `/api/governance/knowledge/${revisionId}/decisions`, 200, {
    stage: "review", rationale: "The source passage supports the submitted refund timing.",
  });
  await signOut();
  await signIn("employee");
  await expect("POST", `/api/governance/knowledge/${revisionId}/decisions`, 200, {
    stage: "publish", rationale: "I approved the independently reviewed synthetic policy.",
  });

  const policyEntryId = createdPolicy.id;
  const processName = `QA Refund Process ${Date.now()}`;
  browserActionContext = "Manager creates process impact map in Approvals UI";
  await signOut();
  await signIn("manager");
  await navigate("Approvals", "Approvals");
  const policyFormReady = await waitFor(`(()=>{const section=[...document.querySelectorAll('.approval-section')].find(x=>x.querySelector('h2')?.textContent.trim()==='Policy impact and exceptions');const form=section?.querySelector('form');return Boolean(form?.querySelector('input')&&form.querySelector('textarea')&&form.querySelector('select option[value]:not([value=""])')&&form.querySelector('button'))})()`, 30_000);
  if (!policyFormReady) {
    const candidateCount = (await expect("GET", "/api/governance/policy-candidates", 200)).items.length;
    throw new Error(`Policy impact form did not finish loading in the Manager UI (readable candidates: ${candidateCount}).`);
  }
  const processCreated = await evaluate(`(()=>{const section=[...document.querySelectorAll('.approval-section')].find(x=>x.querySelector('h2')?.textContent.trim()==='Policy impact and exceptions');const form=section?.querySelector('form');const name=form?.querySelector('input');const description=form?.querySelector('textarea');const select=form?.querySelector('select');if(!form||!name||!description||!select)return false;Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(name,${JSON.stringify(processName)});name.dispatchEvent(new Event('input',{bubbles:true}));Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(description,${JSON.stringify("Refund intake, eligibility review and customer response.")});description.dispatchEvent(new Event('input',{bubbles:true}));Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set.call(select,${JSON.stringify(policyEntryId)});select.dispatchEvent(new Event('change',{bubbles:true}));const button=[...(form.querySelectorAll('button'))].find(x=>x.textContent.trim()==='Create impact map');if(!button||button.disabled)return false;button.click();return true})()`);
  const processVisible = processCreated && await waitFor(`Boolean([...document.querySelectorAll('article.approval-card')].some(card=>card.querySelector('h3')?.textContent.trim()===${JSON.stringify(processName)}&&card.textContent.includes('30 days of the invoice date')&&card.textContent.includes('qa-policy-impact-source.pdf')))`);
  if (!processVisible) throw new Error("Manager could not create and inspect the source-backed process impact map in the UI.");
  const process = (await expect("GET", "/api/governance/processes", 200)).items.find((item) => item.name === processName);
  if (!process || process.policies.length !== 1 || !process.policies[0].can_manage) throw new Error("The scoped process API did not retain the Manager's active policy mapping.");
  const managerCardExpr = `(()=>[...document.querySelectorAll('article.approval-card')].find(card=>card.querySelector('h3')?.textContent.trim()===${JSON.stringify(processName)}))()`;
  const loadedAssignees = await evaluate(`(()=>{const card=${managerCardExpr};const button=[...(card?.querySelectorAll('button')??[])].find(x=>x.textContent.trim()==='Load in-scope assignees');if(!button)return false;button.click();return true})()`);
  if (!loadedAssignees || !await waitFor(`Boolean([...(${managerCardExpr})?.querySelectorAll('select option')??[]].some(option=>option.textContent.includes(${JSON.stringify(employeeEmail)})))`)) {
    throw new Error("Manager could not load active in-scope assignees in the process UI.");
  }
  const employeeId = await evaluate(`(()=>{const card=${managerCardExpr};return [...(card?.querySelectorAll('select option')??[])].find(option=>option.textContent.toLowerCase().includes(${JSON.stringify(employeeEmail.toLowerCase())}))?.value??null})()`);
  if (!employeeId) throw new Error("The protected synthetic Employee fixture does not match an active QA account.");
  const assignmentEntered = await evaluate(`(()=>{const card=${managerCardExpr};const select=card?.querySelector('select');const textArea=card?.querySelector('textarea');if(!select||!textArea)return false;Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set.call(select,${JSON.stringify(employeeId)});select.dispatchEvent(new Event('change',{bubbles:true}));Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(textArea,'Update and check the refund intake procedure against the approved source.');textArea.dispatchEvent(new Event('input',{bubbles:true}));const button=[...(card?.querySelectorAll('button')??[])].find(x=>x.textContent.trim()==='Assign exception');if(!button||button.disabled)return false;button.click();return true})()`);
  if (!assignmentEntered || !await waitFor(`Boolean([...document.querySelectorAll('article.approval-card')].some(card=>card.textContent.includes(${JSON.stringify(processName)})&&card.textContent.includes(${JSON.stringify(`Assigned to ${employeeEmail}`)})&&card.textContent.includes('open')))`)) {
    throw new Error("Manager could not assign a policy implementation exception through the UI.");
  }
  const managerExceptions = await expect("GET", "/api/governance/policy-exceptions", 200);
  const assignedException = managerExceptions.items.find((item) => item.process_id === process.id);
  if (!assignedException || assignedException.status !== "open" || assignedException.assigned_to.user_id !== employeeId) {
    throw new Error(`The Manager exception list did not reflect its new assignment (visible=${managerExceptions.items.length}; target=${process.name}; visible_processes=${managerExceptions.items.map((item) => item.process_name).join(",")}; found=${Boolean(assignedException)}; open=${assignedException?.status === "open"}; assignee_matches=${assignedException?.assigned_to.user_id === employeeId}).`);
  }
  evidence.push("A07_MANAGER_POLICY_TO_PROCESS_IMPACT_AND_EXCEPTION_ASSIGNMENT_UI=PASS");

  browserActionContext = "Employee acknowledges and resolves assigned policy exception in UI";
  await signOut();
  await signIn("employee");
  await navigate("Approvals", "Approvals");
  const exceptionCardExpr = `(()=>[...document.querySelectorAll('article.approval-card')].find(card=>card.textContent.includes(${JSON.stringify(processName)})&&card.textContent.includes(${JSON.stringify(employeeEmail)})) )()`;
  if (!await waitFor(`Boolean(${exceptionCardExpr})`)) throw new Error("The Employee did not see the source-readable assigned policy action.");
  const acked = await evaluate(`(()=>{const card=${exceptionCardExpr};const area=card?.querySelector('textarea');if(!area)return false;Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(area,'I reviewed the intake steps and am updating the procedure.');area.dispatchEvent(new Event('input',{bubbles:true}));const button=[...(card?.querySelectorAll('button')??[])].find(x=>x.textContent.trim()==='Acknowledge');if(!button||button.disabled)return false;button.click();return true})()`);
  if (!acked || !await waitFor(`Boolean(${exceptionCardExpr}?.textContent.includes('acknowledged'))`)) {
    throw new Error("The assigned Employee could not acknowledge the policy action in the UI.");
  }
  const resolved = await evaluate(`(()=>{const card=${exceptionCardExpr};const area=card?.querySelector('textarea');if(!area)return false;Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(area,'The procedure is updated and checked against the approved policy source.');area.dispatchEvent(new Event('input',{bubbles:true}));const button=[...(card?.querySelectorAll('button')??[])].find(x=>x.textContent.trim()==='Resolve action');if(!button||button.disabled)return false;button.click();return true})()`);
  if (!resolved || !await waitFor(`Boolean(${exceptionCardExpr}?.textContent.includes('resolved'))`)) {
    throw new Error("The assigned Employee could not resolve the policy action in the UI.");
  }
  const finalException = (await expect("GET", "/api/governance/policy-exceptions", 200)).items.find((item) => item.id === assignedException.id);
  if (!finalException || finalException.status !== "resolved" || finalException.events.map((event) => event.action).join(",") !== "assigned,acknowledged,resolved") {
    throw new Error("The Employee action history did not show the complete append-only assignment lifecycle.");
  }
  const summary = databasePolicyExceptionSummary(finalException.id);
  if (summary !== "resolved,3,1,1,1") throw new Error("The PostgreSQL exception events or audit rows did not match the browser lifecycle.");
  evidence.push(`A07_POLICY_EXCEPTION_POSTGRES_SUMMARY=${summary}`);
  evidence.push("A07_EMPLOYEE_POLICY_EXCEPTION_ACKNOWLEDGE_RESOLVE_UI=PASS");
}

async function uploadRestrictedPdfFromUi(groupId, filename, pdfText) {
  await navigate("Knowledge", "", ".docs-panel");
  const selectedScope = await evaluate(`(()=>{
    const section=document.querySelector('.docs-panel');
    const sharing=[...(section?.querySelectorAll('label')??[])].find(label=>label.textContent.includes('Sharing'))?.querySelector('select');
    if(!sharing)return false;
    Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set.call(sharing,'restricted');
    sharing.dispatchEvent(new Event('change',{bubbles:true}));return true;
  })()`);
  const scopeReady = selectedScope && await waitFor(`(()=>{const section=document.querySelector('.docs-panel');const label=[...(section?.querySelectorAll('label')??[])].find(item=>item.textContent.includes('Department or workspace'));return Boolean([...((label?.querySelector('select'))?.options??[])].some(option=>option.value===${JSON.stringify(groupId)}))})()`);
  if (!scopeReady) throw new Error("Admin's upload UI did not offer the newly created workspace as an ACL scope.");
  const scopeChosen = await evaluate(`(()=>{const section=document.querySelector('.docs-panel');const label=[...(section?.querySelectorAll('label')??[])].find(item=>item.textContent.includes('Department or workspace'));const select=label?.querySelector('select');if(!select)return false;Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set.call(select,${JSON.stringify(groupId)});select.dispatchEvent(new Event('change',{bubbles:true}));return true})()`);
  if (!scopeChosen) throw new Error("Admin could not select the restricted workspace in the upload UI.");
  const fileAttached = await evaluate(`(()=>{const input=document.querySelector('.docs-panel input[type=file][accept]');if(!input)return false;const transfer=new DataTransfer();transfer.items.add(new File([Uint8Array.from(atob(${JSON.stringify(makeSyntheticPdf(pdfText).toString("base64"))}),c=>c.charCodeAt(0))],${JSON.stringify(filename)},{type:'application/pdf'}));input.files=transfer.files;input.dispatchEvent(new Event('change',{bubbles:true}));return true})()`);
  if (!fileAttached) throw new Error("The restricted document file picker was unavailable in the Admin upload UI.");
  let item;
  const until = Date.now() + 90_000;
  while (Date.now() < until) {
    item = (await expect("GET", "/api/rag/documents", 200)).documents.find((document) => document.filename === filename);
    if (item?.status === "ready" && item.searchable && item.chunk_count > 0) break;
    if (item?.status === "failed") throw new Error(`Workspace-scoped PDF worker failed with code ${item.error_code ?? "unknown"}.`);
    await delay(1_000);
  }
  if (item?.status !== "ready" || !item.searchable || item.chunk_count < 1 || item.visibility !== "restricted") {
    throw new Error("The Admin restricted upload did not become searchable with the expected workspace scope.");
  }
  return item.id;
}

async function uploadSyntheticAudio() {
  if (!mediaFixturePath) return null;
  const audio = readFileSync(mediaFixturePath);
  if (audio.length < 1000 || audio.length > 10 * 1024 * 1024) {
    throw new Error("The configured synthetic audio fixture is empty or exceeds the 10 MiB acceptance bound.");
  }
  const encoded = audio.toString("base64");
  const result = await evaluate(`(async()=>{const binary=atob(${JSON.stringify(encoded)});const bytes=Uint8Array.from(binary,c=>c.charCodeAt(0));const form=new FormData();form.append('file',new Blob([bytes],{type:'audio/wav'}),'qa-known-answer.wav');form.append('visibility','shared');const response=await fetch('/api/rag/documents',{method:'POST',credentials:'same-origin',headers:{'X-Requested-With':'XMLHttpRequest'},body:form});let json=null;try{json=await response.json()}catch{}return {status:response.status,id:json?.id}})()`);
  if (result?.status !== 202 || typeof result.id !== "string") {
    throw new Error(`Synthetic audio upload returned HTTP ${result?.status}.`);
  }
  documentIds.media = result.id;
  let state;
  const until = Date.now() + 180_000;
  while (Date.now() < until) {
    state = await expect("GET", `/api/rag/documents/${result.id}`, 200);
    if (state.status === "ready" && state.searchable && state.chunk_count > 0) break;
    if (state.status === "failed") throw new Error(`Synthetic audio worker failed with code ${state.error_code ?? "unknown"}.`);
    await delay(1_000);
  }
  if (state?.status !== "ready" || !state.searchable || state.chunk_count < 1) {
    throw new Error("Real-model audio upload did not reach searchable ready state within 180 seconds.");
  }
  evidence.push(`REAL_ASR_API_WORKER_READY=PASS pages=${state.page_count} chunks=${state.chunk_count}`);
  return result.id;
}

async function auditVideoBrowserAcceptance() {
  if (!videoFixturePath || !existsSync(videoFixturePath)) {
    throw new Error("Set INFRASPINE_QA_VIDEO_FILE to the bounded synthetic MP4 fixture.");
  }
  const video = readFileSync(videoFixturePath);
  if (video.length < 1000 || video.length > 10 * 1024 * 1024) {
    throw new Error("The synthetic MP4 fixture is empty or exceeds the 10 MiB browser-test bound.");
  }
  const filename = `qa-video-browser-${Date.now()}.mp4`;
  await signIn("admin");
  await navigate("Knowledge", "", ".docs-panel");
  const shared = await evaluate(`(()=>{const section=document.querySelector('.docs-panel');const select=[...(section?.querySelectorAll('label')??[])].find(label=>label.textContent.includes('Sharing'))?.querySelector('select');if(!select)return false;Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set.call(select,'shared');select.dispatchEvent(new Event('change',{bubbles:true}));return true})()`);
  if (!shared) throw new Error("Admin shared-upload control was unavailable for the synthetic video.");
  const encoded = video.toString("base64");
  const fileReady = await evaluate(`(()=>{const input=document.querySelector('.docs-panel input[type=file][accept]');if(!input)return false;const transfer=new DataTransfer();transfer.items.add(new File([Uint8Array.from(atob(${JSON.stringify(encoded)}),c=>c.charCodeAt(0))],${JSON.stringify(filename)},{type:'video/mp4'}));input.files=transfer.files;input.dispatchEvent(new Event('change',{bubbles:true}));return true})()`);
  if (!fileReady) throw new Error("Admin video file picker was unavailable.");
  const until = Date.now() + 180_000;
  let document;
  while (Date.now() < until) {
    document = (await expect("GET", "/api/rag/documents", 200)).documents.find((item) => item.filename === filename);
    if (document?.status === "ready" && document.searchable && document.chunk_count > 0) break;
    if (document?.status === "failed") throw new Error(`Video worker failed with code ${document.error_code ?? "unknown"}.`);
    await delay(1_000);
  }
  if (!document?.id || document.status !== "ready" || !document.searchable || document.chunk_count < 1) {
    throw new Error("Synthetic video did not reach searchable ready state within 180 seconds.");
  }
  if (!databaseFirstChunk(document.id).includes("4821")) throw new Error("Local OCR did not index the synthetic frame value.");
  const locator = databaseVideoLocator(document.id);
  evidence.push(`C06_VIDEO_API_WORKER_OCR=PASS chunks=${document.chunk_count} locator=${locator.label}`);

  browserActionContext = "Admin video citation and original-source UI";
  const documentRowReady = await waitFor(`(()=>{const row=[...document.querySelectorAll('.doc-item')].find(item=>item.querySelector('.doc-title strong')?.textContent.trim()===${JSON.stringify(filename)});return Boolean(row&&row.getClientRects().length&&row.querySelector('.doc-status-ready')&&row.querySelector('button.doc-ask'))})()`, 20_000);
  if (!documentRowReady) {
    const listState = await evaluate(`(()=>({visibleRows:[...document.querySelectorAll('.doc-item .doc-title strong')].map(item=>item.textContent.trim()).slice(0,12),libraryText:document.querySelector('.docs-panel')?.innerText?.slice(0,240)??''}))()`);
    throw new Error(`Indexed video was not visible in the Knowledge library after worker completion (${JSON.stringify(listState)}).`);
  }
  const asked = await evaluate(`(()=>{const row=[...document.querySelectorAll('.doc-item')].find(item=>item.querySelector('.doc-title strong')?.textContent.trim()===${JSON.stringify(filename)});const button=row?.querySelector('button.doc-ask');if(!button)return false;button.click();return true})()`);
  const focused = asked && await waitFor(`document.querySelector('.focus-chip')?.textContent.includes(${JSON.stringify(filename)})`);
  if (!focused) throw new Error("Admin could not focus the indexed video in the Knowledge UI.");
  const question = "What pressure value is shown on the synthetic QA pump frame?";
  const submitted = await evaluate(`(()=>{const input=document.querySelector('.chat-composer textarea[aria-label="Question"]');const send=document.querySelector('.chat-composer button[aria-label="Send"]');if(!input||!send)return false;Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(input,${JSON.stringify(question)});input.dispatchEvent(new Event('input',{bubbles:true}));if(send.disabled)return false;send.click();return true})()`);
  const citeVisible = submitted && await waitFor(`(()=>{const turn=[...document.querySelectorAll('.turn')].find(item=>item.querySelector('.bubble-user')?.textContent===${JSON.stringify(question)});const source=[...(turn?.querySelectorAll('.source-head')??[])].find(item=>item.textContent.includes(${JSON.stringify(filename)}));return Boolean(source&&source.textContent.includes(${JSON.stringify(locator.label)}))})()`, 90_000);
  if (!citeVisible) throw new Error("Admin's Knowledge UI answer did not render the video frame timestamp citation.");
  const expanded = await evaluate(`(()=>{const turn=[...document.querySelectorAll('.turn')].find(item=>item.querySelector('.bubble-user')?.textContent===${JSON.stringify(question)});const source=[...(turn?.querySelectorAll('.source-head')??[])].find(item=>item.textContent.includes(${JSON.stringify(filename)}));if(!source)return false;source.click();return true})()`);
  const previewLink = expanded && await waitFor(`(()=>{const turn=[...document.querySelectorAll('.turn')].find(item=>item.querySelector('.bubble-user')?.textContent===${JSON.stringify(question)});const source=turn?.querySelector('.source-open .source-page-link');return Boolean(source?.textContent.includes('Open original media at ${locator.label}'))})()`);
  if (!previewLink) throw new Error("The video citation did not expose its timestamped original-media preview link.");
  const uiPreview = await evaluate(`(async()=>{const turn=[...document.querySelectorAll('.turn')].find(item=>item.querySelector('.bubble-user')?.textContent===${JSON.stringify(question)});const link=turn?.querySelector('.source-open .source-page-link');if(!link)return null;const response=await fetch(link.href,{credentials:'same-origin'});const body=await response.arrayBuffer();return {status:response.status,type:response.headers.get('content-type')??'',bytes:body.byteLength}})()`);
  if (uiPreview?.status !== 200 || uiPreview.type !== "video/mp4" || uiPreview.bytes !== video.length) {
    throw new Error("Admin's cited original-video preview did not return the uploaded MP4.");
  }
  evidence.push(`C06_VIDEO_CITATION_ORIGINAL_PREVIEW_UI=PASS bytes=${uiPreview.bytes}`);

  await signOut();
  await signIn("employee");
  await navigate("Knowledge", "", ".chat-panel");
  const answer = await expect("POST", "/api/rag/chat", 200, {
    question, document_ids: [document.id],
  });
  const citation = answer.citations?.find((item) => item.document_id === document.id);
  if (!citation || citation.locator?.kind !== "video_frame" ||
      citation.locator.timestamp_seconds !== locator.timestamp_seconds || !answer.retrieved_chunks) {
    throw new Error("Employee did not receive the timestamped video citation from authorized retrieval.");
  }
  const employeePreview = await evaluate(`(async()=>{const r=await fetch(${JSON.stringify(`/api/rag/documents/${document.id}/media`)},{credentials:'same-origin'});const b=await r.arrayBuffer();return {status:r.status,type:r.headers.get('content-type')??'',bytes:b.byteLength}})()`);
  if (employeePreview?.status !== 200 || employeePreview.type !== "video/mp4" || employeePreview.bytes !== video.length) {
    throw new Error("Authorized Employee video preview did not stream the original source.");
  }
  evidence.push(`C06_VIDEO_EMPLOYEE_CITATION_PREVIEW=PASS locator=${citation.location}`);

  await signOut();
  await signIn("admin");
  await expect("PUT", `/api/rag/documents/${document.id}/access`, 200, {
    visibility: "private", user_emails: [], group_ids: [],
  });
  await signOut();
  await signIn("employee");
  const revokedPreview = await request("GET", `/api/rag/documents/${document.id}/media`);
  const revokedAnswer = await expect("POST", "/api/rag/chat", 200, { question, document_ids: [document.id] });
  if (revokedPreview.status !== 404 || revokedAnswer.retrieved_chunks !== 0 ||
      revokedAnswer.citations?.length !== 0 || revokedAnswer.insufficient_evidence !== true ||
      (revokedAnswer.answer ?? "").includes("4821")) {
    throw new Error("Video source ACL revocation did not remove preview and retrieval access immediately.");
  }
  evidence.push("C06_VIDEO_ACL_REVOCATION_PREVIEW_AND_RETRIEVAL=PASS");
}

async function auditCompanySetup() {
  const profile = await expect("GET", "/api/admin/deployment", 200);
  const expected = {
    company_name: profile.company_name,
    industry: profile.industry,
    brand_name: profile.brand_name,
    // HTML color inputs canonicalize hex to lowercase when the form is submitted.
    primary_color: profile.primary_color.toLowerCase(),
  };
  if (Object.values(expected).some((value) => typeof value !== "string" || !value)) {
    throw new Error("Synthetic company setup profile is incomplete.");
  }
  await navigate("Company setup", "Company setup");
  const initialized = await waitFor(`(()=>{const card=[...document.querySelectorAll('.deployment-card')].find(x=>x.querySelector('h2')?.textContent.trim()==='Company profile');const form=card?.querySelector('form.deployment-form');const inputs=[...(form?.querySelectorAll('input')??[])];return inputs.length===4&&inputs.slice(0,3).every(x=>x.value.trim().length>=2)&&inputs[3].type==='color'})()`);
  if (!initialized) throw new Error("Company setup profile form is incomplete.");

  const values = JSON.stringify([expected.company_name, expected.industry, expected.brand_name, expected.primary_color]);
  await evaluate(`(()=>{const form=[...document.querySelectorAll('.deployment-card')].find(x=>x.querySelector('h2')?.textContent.trim()==='Company profile')?.querySelector('form.deployment-form');window.__qaSetupSubmitEvents=0;window.__qaSetupRequests=[];form?.addEventListener('submit',()=>window.__qaSetupSubmitEvents++,true);const original=window.fetch.bind(window);window.fetch=async(input,init)=>{const url=typeof input==='string'?input:input.url;const response=await original(input,init);if(String(url).includes('/api/admin/deployment'))window.__qaSetupRequests.push({url:new URL(url,location.href).pathname,status:response.status});return response}})()`);
  const saveClicked = await evaluate(`(()=>{const card=[...document.querySelectorAll('.deployment-card')].find(x=>x.querySelector('h2')?.textContent.trim()==='Company profile');const form=card?.querySelector('form.deployment-form');const inputs=[...(form?.querySelectorAll('input')??[])];const values=${values};if(inputs.length!==4)return false;inputs.forEach((input,index)=>{const setter=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;setter.call(input,values[index]);input.dispatchEvent(new Event('input',{bubbles:true}));input.dispatchEvent(new Event('change',{bubbles:true}))});const button=form.querySelector('button[type="submit"],button:not([type])');if(!button||button.disabled)return false;form.requestSubmit(button);return true})()`);
  if (!saveClicked) throw new Error("Company profile save control was unavailable or disabled.");
  if (!await waitFor(`document.querySelector('.banner-info')?.textContent?.includes('Company profile saved on this deployment.')`)) {
    const state = await evaluate(`(()=>{const form=[...document.querySelectorAll('.deployment-card')].find(x=>x.querySelector('h2')?.textContent.trim()==='Company profile')?.querySelector('form.deployment-form');const inputs=[...(form?.querySelectorAll('input')??[])];return {info:document.querySelector('.banner-info')?.textContent?.trim()?.slice(0,160)??'',error:document.querySelector('.banner-error')?.textContent?.trim()?.slice(0,160)??'',formValid:form?.checkValidity()??false,submitEvents:window.__qaSetupSubmitEvents??0,requests:window.__qaSetupRequests??[],inputs:inputs.map(x=>({type:x.type,required:x.required,minLength:x.minLength,valueLength:x.value.length,valid:x.validity.valid,valueMissing:x.validity.valueMissing,tooShort:x.validity.tooShort})),formButtonDisabled:Boolean(form?.querySelector('button')?.disabled)}})()`);
    throw new Error(`Company setup UI did not report that the profile was saved (${JSON.stringify(state)}).`);
  }
  const persisted = await expect("GET", "/api/admin/deployment", 200);
  if (persisted.company_name !== expected.company_name || persisted.industry !== expected.industry ||
      persisted.brand_name !== expected.brand_name || persisted.primary_color !== expected.primary_color ||
      persisted.setup_complete !== true) {
    throw new Error("Company setup UI did not persist the synthetic profile.");
  }
  evidence.push("COMPANY_SETUP_UI_SAVE_ROUNDTRIP=PASS");

  const catalog = await expect("GET", "/api/admin/deployment/templates", 200);
  const manufacturing = catalog.find((template) => template.template_id === "manufacturing");
  if (!manufacturing || manufacturing.version !== "1.0.0" || manufacturing.default_departments.length < 1) {
    throw new Error("The setup API did not return the versioned manufacturing starter template.");
  }
  const templateSelected = await evaluate(`(()=>{const label=[...document.querySelectorAll('.deployment-form label')].find(item=>item.textContent.includes('Starter template'));const select=label?.querySelector('select');if(!select)return false;Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set.call(select,'manufacturing');select.dispatchEvent(new Event('input',{bubbles:true}));select.dispatchEvent(new Event('change',{bubbles:true}));return true})()`);
  const templateProfileSaved = templateSelected && await evaluate(`(()=>{const form=[...document.querySelectorAll('.deployment-card')].find(item=>item.querySelector('h2')?.textContent.trim()==='Company profile')?.querySelector('form');const button=[...(form?.querySelectorAll('button')??[])].find(item=>item.textContent.trim()==='Save company profile');if(!button||button.disabled)return false;button.click();return true})()`);
  if (!templateProfileSaved || !await waitFor(`document.querySelector('.banner-info')?.textContent?.includes('Company profile saved on this deployment.')`)) {
    throw new Error("The setup wizard could not save the selected industry template version.");
  }
  const templateProfile = await expect("GET", "/api/admin/deployment", 200);
  if (templateProfile.template_id !== manufacturing.template_id || templateProfile.template_version !== manufacturing.version ||
      templateProfile.industry !== manufacturing.industry) {
    throw new Error("The selected industry template identity/version did not persist with the company profile.");
  }
  const applyTemplate = await evaluate(`(()=>{const preview=document.querySelector('.deployment-template-preview');const button=[...(preview?.querySelectorAll('button')??[])].find(item=>item.textContent.trim()==='Create missing department groups');if(!button||button.disabled)return false;button.click();return true})()`);
  if (!applyTemplate || !await waitFor(`document.querySelector('.banner-info')?.textContent?.includes('Template 1.0.0 applied: created')`)) {
    throw new Error("The Admin could not apply the template from the setup UI.");
  }
  const templateGroups = (await expect("GET", "/api/admin/access/groups", 200)).groups;
  const groupedByName = new Map(templateGroups.map((group) => [group.name, group]));
  if (manufacturing.default_departments.some((name) => !groupedByName.has(name) || groupedByName.get(name).member_count !== 0)) {
    throw new Error("Industry template application did not create only empty department groups.");
  }
  const reapply = await evaluate(`(()=>{const button=[...(document.querySelector('.deployment-template-preview')?.querySelectorAll('button')??[])].find(item=>item.textContent.trim()==='Create missing department groups');if(!button||button.disabled)return false;button.click();return true})()`);
  if (!reapply || !await waitFor(`document.querySelector('.banner-info')?.textContent?.includes('created 0 empty department groups')`)) {
    throw new Error("Applying the same department template twice was not idempotent.");
  }
  evidence.push("A09_INDUSTRY_TEMPLATE_SETUP_APPLY_IDEMPOTENT_AND_NO_MEMBERS=PASS");

  const invalidJsonShown = await evaluate(`(()=>{const card=[...document.querySelectorAll('.deployment-card')].find(x=>x.querySelector('h2')?.textContent.trim()==='License');const form=card?.querySelector('form.deployment-form');const input=form?.querySelector('textarea');if(!input)return false;const setter=Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set;setter.call(input,'{');input.dispatchEvent(new Event('input',{bubbles:true}));const button=form.querySelector('button[type="submit"],button:not([type])');if(!button||button.disabled)return false;button.click();return true})()`);
  if (!invalidJsonShown || !await waitFor(`document.querySelector('.banner-error')?.textContent?.includes('valid JSON')`)) {
    throw new Error("Company setup did not show a safe validation error for malformed license JSON.");
  }
  evidence.push("LICENSE_FORM_INVALID_JSON_VALIDATION=PASS");
}

async function auditAdaptiveTraining() {
  if (!adaptiveTrainingCourseId) return;
  const initialHistory = (await expect("GET", `/api/rag/training/${adaptiveTrainingCourseId}/adaptive-practice`, 200)).scenarios;
  const initialQuizAttempts = (await expect("GET", `/api/rag/training/${adaptiveTrainingCourseId}/quiz-attempts`, 200)).attempts;
  await navigate("Employee training", "Employee training");
  const historyRendered = await waitFor(`Boolean([...document.querySelectorAll('.training-history-row')].find(item=>item.querySelector('strong')?.textContent.trim()==='Synthetic adaptive practice acceptance'))`);
  if (!historyRendered) throw new Error("The Employee training page did not render the saved synthetic lesson.");
  const opened = await evaluate(`(()=>{const row=[...document.querySelectorAll('.training-history-row')].find(item=>item.querySelector('strong')?.textContent.trim()==='Synthetic adaptive practice acceptance');const button=[...(row?.querySelectorAll('button')??[])].find(item=>item.textContent.trim()==='Continue');if(!button)return false;button.click();return true})()`);
  if (!opened) throw new Error("The Employee could not open the saved synthetic lesson from training history.");
  if (!await waitFor(`Boolean(document.querySelector('[aria-labelledby="adaptive-practice-title"] button'))`)) {
    throw new Error("The Employee opened the lesson, but its adaptive practice panel did not render.");
  }
  const started = await evaluate(`(()=>{const section=document.querySelector('[aria-labelledby="adaptive-practice-title"]');const button=[...(section?.querySelectorAll('button')??[])].find(item=>item.textContent.includes('Start short practice'));if(!button||button.disabled)return false;button.click();return true})()`);
  if (!started || !await waitFor(`document.querySelectorAll('[aria-labelledby="adaptive-practice-title"] fieldset').length>0`)) {
    throw new Error("The Employee could not start an adaptive practice scenario.");
  }
  const prepared = await expect("GET", `/api/rag/training/${adaptiveTrainingCourseId}/adaptive-practice`, 200);
  const pending = prepared.scenarios.find((item) => !item.completed_at && item.question_indexes.length < 3);
  const practiceQuestionCount = await evaluate(`document.querySelectorAll('[aria-labelledby="adaptive-practice-title"] fieldset').length`);
  if (!pending || practiceQuestionCount !== pending.question_indexes.length || pending.question_indexes.length !== 2) {
    throw new Error("Adaptive practice did not render the bounded two-question synthetic scenario.");
  }
  const selected = await evaluate(`(()=>{const section=document.querySelector('[aria-labelledby="adaptive-practice-title"]');const fields=[...(section?.querySelectorAll('fieldset')??[])];for(const field of fields){const first=field.querySelector('input[type="radio"]');if(!first)return false;first.click()}return fields.length})()`);
  if (selected !== practiceQuestionCount) throw new Error("The adaptive practice answer controls did not accept selections.");
  const submitted = await evaluate(`(()=>{const section=document.querySelector('[aria-labelledby="adaptive-practice-title"]');const form=section?.querySelector('form');if(!form)return false;form.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));return true})()`);
  if (!submitted || !await waitFor(`document.querySelector('[aria-labelledby="adaptive-practice-title"] .training-quiz-result')?.textContent.includes('This result is not a certification attempt.')`)) {
    throw new Error("Adaptive practice submission did not show its formative result.");
  }
  const saved = await expect("GET", `/api/rag/training/${adaptiveTrainingCourseId}/adaptive-practice`, 200);
  const completed = saved.scenarios.find((item) => item.scenario_id === pending.scenario_id);
  const finalQuizAttempts = (await expect("GET", `/api/rag/training/${adaptiveTrainingCourseId}/quiz-attempts`, 200)).attempts;
  if (!completed?.completed_at || completed.score === null || completed.max_score !== 2 ||
      finalQuizAttempts.length !== initialQuizAttempts.length || saved.scenarios.length <= initialHistory.length) {
    throw new Error("Adaptive practice history was not saved independently from certification attempts.");
  }
  await cdp.command("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: false });
  await delay(150);
  const mobileLayout = await evaluate(`(()=>({bodyWidth:document.body.scrollWidth,viewportWidth:window.innerWidth,practiceWidth:document.querySelector('[aria-labelledby="adaptive-practice-title"]')?.scrollWidth??0,practiceClientWidth:document.querySelector('[aria-labelledby="adaptive-practice-title"]')?.clientWidth??0}))()`);
  await cdp.command("Emulation.clearDeviceMetricsOverride");
  if (!mobileLayout || mobileLayout.bodyWidth > mobileLayout.viewportWidth + 1 ||
      mobileLayout.practiceWidth > mobileLayout.practiceClientWidth + 1) {
    throw new Error("The adaptive practice result overflows the mobile layout.");
  }
  evidence.push("F13_ADAPTIVE_UI_SCENARIO_SUBMIT_HISTORY_NO_CERTIFICATION=PASS questions=2");
  evidence.push("F13_ADAPTIVE_UI_MOBILE_LAYOUT=PASS width=390");
}

async function auditWorkflowLifecycle(departmentId) {
  let stage = "manager workflow form";
  try {
  const workflowName = "Synthetic browser lifecycle " + Date.now();
  const taskTitle = "Synthetic approved task " + Date.now();
  workflowTaskTitleForInbox = taskTitle;
  const taskBody = "Verify the isolated backup manifest and record the check in QA.";
  const managerFormReady = await waitFor("(()=>{const f=document.querySelector('form.automation-form');if(!f)return false;const selects=[...f.querySelectorAll('select')];return Boolean(selects.length===3&&selects[0].querySelector('option[value=\""+departmentId+"\"]')&&selects[2].querySelector('option[value=\""+identity.employee+"\"]'))})()");
  if (!managerFormReady) throw new Error("Manager workflow form did not load its managed department and active employee.");
  stage = "manager workflow creation";
  const values = JSON.stringify({
    "Workflow name": workflowName,
    Trigger: "manual",
    "Task title": taskTitle,
    "Task instructions": taskBody,
    "Due in days (optional)": "7",
  });
  for (const [label, value] of Object.entries(JSON.parse(values))) {
    const set = await evaluate("(()=>{const form=document.querySelector('form.automation-form');const label=[...(form?.querySelectorAll('label.field')??[])].find(item=>item.querySelector('.field-label')?.textContent?.trim()===" + JSON.stringify(label) + ");const control=label?.querySelector('input,select,textarea');if(!control)return false;const proto=control instanceof HTMLSelectElement?HTMLSelectElement.prototype:control instanceof HTMLTextAreaElement?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;Object.getOwnPropertyDescriptor(proto,'value').set.call(control," + JSON.stringify(value) + ");control.dispatchEvent(new Event('input',{bubbles:true}));control.dispatchEvent(new Event('change',{bubbles:true}));return true})()");
    if (!set) throw new Error("Manager workflow field could not be set: " + label + ".");
  }
  const departmentSet = await evaluate("(()=>{const form=document.querySelector('form.automation-form');const label=[...(form?.querySelectorAll('label.field')??[])].find(item=>item.querySelector('.field-label')?.textContent?.trim()==='Department');const control=label?.querySelector('select');if(!control)return false;Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set.call(control," + JSON.stringify(departmentId) + ");control.dispatchEvent(new Event('input',{bubbles:true}));control.dispatchEvent(new Event('change',{bubbles:true}));return true})()");
  if (!departmentSet || !await waitFor("(()=>{const f=document.querySelector('form.automation-form');const select=[...(f?.querySelectorAll('select')??[])][2];return Boolean(select?.querySelector('option[value=\""+identity.employee+"\"]'))})()")) {
    throw new Error("Manager workflow form did not retain the chosen department or reload its active assignees.");
  }
  const assigneeSet = await evaluate("(()=>{const form=document.querySelector('form.automation-form');const label=[...(form?.querySelectorAll('label.field')??[])].find(item=>item.querySelector('.field-label')?.textContent?.trim()==='Task assignee');const control=label?.querySelector('select');if(!control)return false;Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set.call(control," + JSON.stringify(identity.employee) + ");control.dispatchEvent(new Event('input',{bubbles:true}));control.dispatchEvent(new Event('change',{bubbles:true}));return true})()");
  if (!assigneeSet) throw new Error("Manager could not select the active employee as the workflow assignee.");
  const submitted = await evaluate("(()=>{const form=document.querySelector('form.automation-form');const button=[...(form?.querySelectorAll('button')??[])].find(item=>item.textContent.trim()==='Create workflow');const result={found:Boolean(button),disabled:Boolean(button?.disabled),valid:Boolean(form?.checkValidity()),clicked:false};if(!button||button.disabled||!result.valid)return result;button.click();result.clicked=true;return result})()");
  const createdNotice = submitted?.clicked && await waitFor("document.querySelector('.banner-success')?.textContent?.includes('Workflow created.')");
  if (!createdNotice) {
    const state = await evaluate("(()=>({error:document.querySelector('.banner-error')?.textContent?.trim()??'',success:document.querySelector('.banner-success')?.textContent?.trim()??'',fields:[...(document.querySelector('form.automation-form')?.querySelectorAll('label.field')??[])].map(label=>({name:label.querySelector('.field-label')?.textContent?.trim()??'',value:label.querySelector('input,select,textarea')?.value??'',valid:label.querySelector('input,select,textarea')?.checkValidity?.()??true})),workflowButtonDisabled:[...document.querySelectorAll('form.automation-form button')].find(item=>item.textContent.trim()==='Create workflow')?.disabled??null}))()");
    throw new Error("Manager could not create the synthetic workflow through the visual editor (" + JSON.stringify({ submitted, state }) + ").");
  }
  stage = "manager workflow persistence";
  const workflow = (await expect("GET", "/api/automation/workflows", 200)).workflows
    .find((item) => item.name === workflowName && item.enabled);
  if (!workflow || workflow.department_group_id !== departmentId || workflow.assignee_user_id !== identity.employee ||
      workflow.trigger_type !== "manual" || !workflow.can_manage) {
    throw new Error("The created workflow did not persist its department, assignee, trigger and manager scope.");
  }

  stage = "employee run request";
  await signOut();
  await signIn("employee");
  await navigate("Workflows & tasks", "Workflows & tasks");
  const requestClicked = await evaluate("(()=>{const card=[...document.querySelectorAll('.automation-item')].find(item=>item.querySelector('h3')?.textContent.trim()===" + JSON.stringify(workflowName) + ");const button=[...(card?.querySelectorAll('button')??[])].find(item=>item.textContent.trim()==='Request run');if(!button||button.disabled)return false;button.click();return true})()");
  if (!requestClicked || !await waitFor("document.querySelector('.banner-success')?.textContent?.includes('Run requested and queued for approval.')")) {
    throw new Error("Employee could not request a workflow run through the UI.");
  }
  const run = (await expect("GET", "/api/automation/runs", 200)).runs
    .find((item) => item.workflow_id === workflow.id && item.status === "pending_approval");
  if (!run || run.requested_by_user_id !== identity.employee || !run.can_cancel || run.can_approve) {
    throw new Error("Employee workflow run did not enter a separate pending-approval state.");
  }
  const selfApproval = await request("POST", "/api/automation/runs/" + run.id + "/decision", {
    approved: true, rationale: "Synthetic self-approval must be rejected.",
  });
  if (selfApproval.status !== 403) throw new Error("The employee requester could approve their own workflow run.");

  stage = "manager approval";
  await signOut();
  await signIn("manager");
  await navigate("Workflows & tasks", "Workflows & tasks");
  const pendingCardReady = await waitFor("(()=>[...document.querySelectorAll('.automation-item')].some(item=>item.querySelector('h3')?.textContent.trim()===" + JSON.stringify(workflowName) + "&&item.querySelector('.automation-item-heading .pill')?.textContent.trim()==='pending approval'&&item.querySelector('textarea')))()");
  if (!pendingCardReady) throw new Error("Manager did not receive the pending workflow approval in the UI.");
  const reviewerNotice = (await expect("GET", "/api/automation/notifications", 200)).items
    .find((item) => item.run_id === run.id && item.notification_type === "approval_required");
  if (!reviewerNotice || reviewerNotice.read_at) throw new Error("The authorized Manager did not receive an unread approval notification.");
  const reviewerNoticeRendered = await waitFor("[...document.querySelectorAll('.workflow-notifications li')].some(row=>row.textContent.includes('Workflow approval requested'))");
  if (!reviewerNoticeRendered) throw new Error("The workflow approval notification did not appear in the Manager inbox.");
  const reviewerNoticeRead = await evaluate("(()=>{const row=[...document.querySelectorAll('.workflow-notifications li')].find(item=>item.querySelector('strong')?.textContent.trim()==='Workflow approval requested');const button=[...(row?.querySelectorAll('button')??[])].find(item=>item.textContent.trim()==='Mark read');if(!button||button.disabled)return false;button.click();return true})()");
  if (!reviewerNoticeRead || !await waitFor(`(async()=>{const r=await fetch('/api/automation/notifications',{credentials:'same-origin'});if(!r.ok)return false;const j=await r.json();return Boolean(j.items.find(item=>item.id===${JSON.stringify(reviewerNotice.id)}&&item.read_at))})()`)) {
    throw new Error("The Manager could not persist the approval-notice read receipt.");
  }
  evidence.push("F14_MANAGER_APPROVAL_NOTIFICATION_READ_RECEIPT=PASS");
  const approvalClicked = await evaluate("(()=>{const card=[...document.querySelectorAll('.automation-item')].find(item=>item.querySelector('h3')?.textContent.trim()===" + JSON.stringify(workflowName) + "&&item.querySelector('.automation-item-heading .pill')?.textContent.trim()==='pending approval');const textarea=card?.querySelector('textarea');if(!textarea)return false;Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(textarea,'Approved for the synthetic backup verification task.');textarea.dispatchEvent(new Event('input',{bubbles:true}));const button=[...(card?.querySelectorAll('button')??[])].find(item=>item.textContent.trim()==='Approve');if(!button||button.disabled)return false;button.click();return true})()");
  if (!approvalClicked || !await waitFor("document.querySelector('.banner-success')?.textContent?.includes('Run approved and queued for the worker.')")) {
    throw new Error("Manager could not approve the requested workflow through the UI.");
  }
  let completedRun = null;
  const runDeadline = Date.now() + 40_000;
  while (Date.now() < runDeadline) {
    completedRun = (await expect("GET", "/api/automation/runs", 200)).runs.find((item) => item.id === run.id);
    if (completedRun?.status === "completed") break;
    await delay(500);
  }
  if (completedRun?.status !== "completed" || completedRun.approval_decision !== "approved" || completedRun.attempts < 1) {
    throw new Error("The worker did not create the approved task and complete the workflow run.");
  }
  const tasks = (await expect("GET", "/api/automation/tasks", 200)).tasks.filter((item) => item.run_id === run.id);
  if (tasks.length !== 1 || tasks[0].assignee_user_id !== identity.employee || tasks[0].status !== "open") {
    throw new Error("The worker did not create exactly one open task for the active assigned employee.");
  }

  stage = "employee task completion";
  await signOut();
  await signIn("employee");
  await navigate("Workflows & tasks", "Workflows & tasks");
  const employeeNotifications = (await expect("GET", "/api/automation/notifications", 200)).items;
  const lifecycleNotifications = employeeNotifications.filter((item) => item.run_id === run.id);
  const notificationByType = new Map(lifecycleNotifications.map((item) => [item.notification_type, item]));
  if (lifecycleNotifications.length !== 2 || !notificationByType.has("run_approved") || !notificationByType.has("task_assigned") ||
      notificationByType.get("run_approved").run_id !== run.id ||
      notificationByType.get("task_assigned").run_id !== run.id ||
      notificationByType.get("task_assigned").task_id !== tasks[0].id) {
    throw new Error("Employee workflow notifications did not match the approval and assigned task.");
  }
  if (!await waitFor("Boolean(document.querySelector('section[aria-label=\"Workflow notifications\"] .workflow-notifications'))")) {
    throw new Error("The workflow notification inbox did not render in the Employee UI.");
  }
  const readClicked = await evaluate("(()=>{const item=[...document.querySelectorAll('.workflow-notifications li')].find(row=>row.querySelector('strong')?.textContent.trim()==='Workflow request approved');const button=[...(item?.querySelectorAll('button')??[])].find(entry=>entry.textContent.trim()==='Mark read');if(!button||button.disabled)return false;button.click();return true})()");
  if (!readClicked) throw new Error("Employee could not mark the workflow approval notification as read.");
  const approvalNoticeId = JSON.stringify(notificationByType.get("run_approved").id);
  if (!await waitFor("(async()=>{const r=await fetch('/api/automation/notifications',{credentials:'same-origin'});if(!r.ok)return false;const j=await r.json();return Boolean(j.items.find(item=>item.id===" + approvalNoticeId + "&&item.read_at))})()")) {
    throw new Error("The Employee notification read receipt did not persist.");
  }
  evidence.push("F14_EMPLOYEE_APPROVAL_AND_TASK_NOTIFICATIONS_READ_RECEIPT=PASS");
  const taskCardReady = await waitFor("(()=>[...document.querySelectorAll('.automation-item')].some(item=>item.querySelector('h3')?.textContent.trim()===" + JSON.stringify(taskTitle) + "&&item.querySelector('button')?.textContent.trim()==='Complete task'))()");
  if (!taskCardReady) throw new Error("The assigned Employee did not see the open task or its completion control.");
  const completedTask = await evaluate("(()=>{const card=[...document.querySelectorAll('.automation-item')].find(item=>item.querySelector('h3')?.textContent.trim()===" + JSON.stringify(taskTitle) + ");const button=[...(card?.querySelectorAll('button')??[])].find(item=>item.textContent.trim()==='Complete task');if(!button||button.disabled)return false;button.click();return true})()");
  if (!completedTask || !await waitFor("document.querySelector('.banner-success')?.textContent?.includes('Task marked complete.')")) {
    throw new Error("The assigned Employee could not complete the task through the UI.");
  }
  const savedTask = (await expect("GET", "/api/automation/tasks", 200)).tasks.find((item) => item.id === tasks[0].id);
  if (savedTask?.status !== "completed" || !savedTask.completed_at) throw new Error("Task completion was not persisted.");
  const auditCounts = databaseWorkflowAuditCounts(run.id, savedTask.id);
  if (auditCounts !== "1,1,1,1") throw new Error("Workflow approval/task history was incomplete (" + auditCounts + ").");
  evidence.push("F14_WORKFLOW_CREATE_REQUEST_SELF_APPROVAL_DENIED_MANAGER_APPROVAL_WORKER_TASK_EMPLOYEE_COMPLETE=PASS");
  evidence.push("F14_WORKFLOW_POSTGRES_AUDIT=request,approval,task-create,task-complete=" + auditCounts);

  await signOut();
  await signIn("manager");
  } catch (error) {
    throw new Error("Workflow lifecycle failed at " + stage + ": " + (error?.message ?? "unknown failure"));
  }
}

async function searchExpertiseInUi(skill) {
  await evaluate(`(()=>{if(window.__qaExpertiseSearchWrapped)return true;window.__qaExpertiseSearchWrapped=true;window.__qaExpertiseSearchCompleted=0;const original=window.fetch.bind(window);window.fetch=async(input,init)=>{const url=typeof input==='string'?input:input.url;const response=await original(input,init);if(new URL(url,location.href).pathname==='/api/expertise/search')window.__qaExpertiseSearchCompleted++;return response}})()`);
  const before = await evaluate("window.__qaExpertiseSearchCompleted");
  const setSkill = await evaluate(`(()=>{const input=document.querySelector('input[aria-label="Skill tag"]');if(!input)return false;Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,${JSON.stringify(skill)});input.dispatchEvent(new Event('input',{bubbles:true}));return true})()`);
  const submit = setSkill && await evaluate(`(()=>{const form=document.querySelector('form.expertise-search');const button=form?.querySelector('button[type="submit"],button:not([type])');if(!button||button.disabled)return false;button.click();return true})()`);
  if (!submit || !await waitFor(`window.__qaExpertiseSearchCompleted>${JSON.stringify(before)}`) ||
      !await waitFor(`document.querySelector('.expertise-results h3')?.textContent?.includes(${JSON.stringify(skill)})`)) {
    throw new Error("Expertise directory search did not render a result state.");
  }
  return await evaluate(`([...document.querySelectorAll('.expertise-result strong')].map(item=>item.textContent?.trim()??''))`);
}

async function auditMyWorkAndExpertise(sharedDepartment) {
  await cdp.command("Emulation.setDeviceMetricsOverride", {
    width: 390, height: 844, deviceScaleFactor: 1, mobile: false,
  });
  const assertMobileNoOverflow = async (page) => {
    const dimensions = await evaluate("(()=>({viewport:window.innerWidth,body:document.body.scrollWidth,document:document.documentElement.scrollWidth}))()");
    if (!dimensions || dimensions.viewport !== 390 || dimensions.body > 392 || dimensions.document > 392) {
      throw new Error(`${page} has horizontal overflow at the 390px viewport: ${JSON.stringify(dimensions)}.`);
    }
    evidence.push(`${page.toUpperCase().replaceAll(" ", "_")}_MOBILE_390PX=PASS`);
  };

  await signOut();
  await signIn("employee");
  await navigate("My work", "My work", 'section[aria-label="Assigned workflow tasks"]');
  await assertMobileNoOverflow("Employee My work");
  const employeeTasks = (await expect("GET", "/api/automation/tasks", 200)).tasks;
  if (!Array.isArray(employeeTasks) || employeeTasks.some((task) => task.assignee_user_id !== identity.employee)) {
    throw new Error("Employee My work API returned a task assigned to another account.");
  }
  if (workflowTaskTitleForInbox) {
    const visible = await waitFor(`document.querySelector('section[aria-label="Assigned workflow tasks"]')?.textContent?.includes(${JSON.stringify(workflowTaskTitleForInbox)})`);
    if (!visible || !employeeTasks.some((task) => task.title === workflowTaskTitleForInbox && task.assignee_user_id === identity.employee)) {
      throw new Error("The assigned workflow task did not appear in the Employee My work inbox.");
    }
  }
  if (await evaluate(`Boolean(document.querySelector('.banner-error'))`)) throw new Error("Employee My work showed an API error.");
  evidence.push("F10_EMPLOYEE_MY_WORK_ASSIGNEE_FILTER_AND_LOAD=PASS");
  await navigate("Expertise directory", "Expertise directory", ".expertise-card");
  await assertMobileNoOverflow("Employee expertise");
  if (!await waitFor(`(()=>{const area=document.querySelector('textarea[aria-label="Skills you choose to share"]');const button=area?.closest('form')?.querySelector('button[type="submit"],button:not([type])');return Boolean(area&&!area.disabled&&button&&!button.disabled)})()`)) {
    throw new Error("Employee expertise profile did not finish loading before the edit control became available.");
  }
  const currentTags = await expect("GET", "/api/expertise/me", 200);
  const savedSkill = `qa-safety-reviewer-${randomBytes(3).toString("hex")}`;
  const preservedTags = (currentTags.tags ?? []).filter((tag) => !tag.startsWith("qa-safety-reviewer-"));
  const nextTags = [...preservedTags, savedSkill];
  const saved = await evaluate(`(()=>{const area=document.querySelector('textarea[aria-label="Skills you choose to share"]');const form=area?.closest('form');if(!area||!form||area.disabled)return false;Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(area,${JSON.stringify(nextTags.join(", "))});area.dispatchEvent(new Event('input',{bubbles:true}));const button=form.querySelector('button[type="submit"],button:not([type])');if(!button||button.disabled)return false;button.click();return true})()`);
  if (!saved || !await waitFor(`(async()=>{const r=await fetch('/api/expertise/me',{credentials:'same-origin'});if(!r.ok)return false;const b=await r.json();return b.tags.includes(${JSON.stringify(savedSkill)})})()`)) {
    throw new Error("Employee expertise profile did not save through the page.");
  }
  evidence.push("F15_EMPLOYEE_SELF_AUTHORED_PROFILE_UI=PASS");

  await signOut();
  await signIn("manager");
  await navigate("Expertise directory", "Expertise directory", ".expertise-card");
  await assertMobileNoOverflow("Manager expertise");
  let matches = await searchExpertiseInUi(savedSkill);
  if (!matches.includes(credentials.employee.email)) throw new Error("Manager could not find a colleague sharing the managed department.");
  let membershipRevoked = false;
  try {
    await expect("DELETE", `/api/departments/${sharedDepartment.id}/members/${identity.employee}`, 204);
    membershipRevoked = true;
    const afterRevocation = (await expect("GET", `/api/expertise/search?skill=${encodeURIComponent(savedSkill)}`, 200)).matches;
    if (afterRevocation.some((match) => match.user_id === identity.employee)) throw new Error("Expertise API retained a revoked department member.");
    matches = await searchExpertiseInUi(savedSkill);
    if (matches.includes(credentials.employee.email)) throw new Error("Manager browser results retained a revoked colleague after a fresh search.");
  } finally {
    if (membershipRevoked) {
      await expect("POST", `/api/departments/${sharedDepartment.id}/members`, 204, { email: credentials.employee.email });
    }
  }
  evidence.push("F15_MANAGER_SCOPE_AND_IMMEDIATE_MEMBERSHIP_REVOCATION=PASS");

  await signOut();
  await signIn("admin");
  await navigate("Expertise directory", "Expertise directory", ".expertise-card");
  await assertMobileNoOverflow("Admin expertise");
  matches = await searchExpertiseInUi(savedSkill);
  if (!matches.includes(credentials.employee.email)) throw new Error("Organization Admin could not find the shared employee profile.");
  evidence.push("F15_ADMIN_DIRECTORY_SEARCH_UI=PASS");

  await navigate("My work", "My work", 'section[aria-label="Assigned workflow tasks"]');
  await assertMobileNoOverflow("Admin My work");

  await signOut();
  await signIn("manager");
  await navigate("My work", "My work", 'section[aria-label="Assigned workflow tasks"]');
  await assertMobileNoOverflow("Manager My work");
  if (workflowTaskTitleForInbox && await evaluate(`document.querySelector('section[aria-label="Assigned workflow tasks"]')?.textContent?.includes(${JSON.stringify(workflowTaskTitleForInbox)})`)) {
    throw new Error("Manager My work displayed a task assigned to the Employee.");
  }
  evidence.push("F10_MANAGER_MY_WORK_EXCLUDES_OTHER_ASSIGNEE=PASS");
}

async function runKnowledgeFreshnessAcceptance() {
  const users = (await expect("GET", "/api/admin/users", 200)).users;
  for (const role of roles) {
    const account = users.find((item) => item.email.toLowerCase() === credentials[role].email.toLowerCase() && item.role === role);
    if (!account) throw new Error("A protected synthetic role account is missing from the C09 candidate clone.");
    identity[role] = account.id;
  }

  await navigate("Knowledge", "", ".chat-panel");
  const sourceId = await uploadSyntheticPdf();
  const knowledgeStatement = "The synthetic QA recovery marker is ZEPHYR 42 under the annual retention policy.";
  const quote = "Synthetic QA recovery token ZEPHYR 42.";
  const entry = await expect("POST", "/api/knowledge", 201, {
    statement: knowledgeStatement,
    category: "general",
    visibility: "shared",
    evidence: [{ document_id: sourceId, quote }],
  });
  const revisionId = entry.revisions?.[0]?.id;
  if (!revisionId) throw new Error("C09 synthetic evidence draft did not create a revision.");
  if ((await expect("POST", `/api/knowledge/${entry.id}/submit`, 200)).revisions?.[0]?.status !== "submitted") {
    throw new Error("C09 synthetic knowledge draft did not enter the approval workflow.");
  }

  for (const [role, permission] of [["manager", "knowledge.review"], ["employee", "knowledge.publish"]]) {
    await expect("POST", "/api/admin/access/grants", 201, {
      email: credentials[role].email, permission, category: "general",
    });
  }
  await signOut();
  await signIn("manager");
  await expect("POST", `/api/governance/knowledge/${revisionId}/decisions`, 200, {
    stage: "review", rationale: "Checked the synthetic source passage and its category.",
  });
  await signOut();
  await signIn("employee");
  await expect("POST", `/api/governance/knowledge/${revisionId}/decisions`, 200, {
    stage: "publish", rationale: "Approved the independently reviewed synthetic statement.",
  });
  databaseBackdateKnowledgeRevision(revisionId);

  await signOut();
  await signIn("admin");
  const authorDenied = await request("POST", `/api/governance/knowledge/${revisionId}/freshness-review`, {
    rationale: "The original contributor cannot reconfirm their own statement.",
  });
  if (authorDenied.status !== 403) throw new Error("The knowledge author could review their own stale statement.");
  await signOut();
  await signIn("employee");
  const publisherQueue = await expect("GET", "/api/governance/knowledge/stale", 200);
  if (publisherQueue.items.some((item) => item.id === revisionId)) throw new Error("A publish-only Employee could read the freshness review queue.");
  const publisherDenied = await request("POST", `/api/governance/knowledge/${revisionId}/freshness-review`, {
    rationale: "This synthetic account has publish permission only.",
  });
  if (publisherDenied.status !== 403) throw new Error("A publish-only Employee could confirm freshness.");
  await signOut();
  await signIn("manager");
  const reviewerQueue = await expect("GET", "/api/governance/knowledge/stale", 200);
  const staleItem = reviewerQueue.items.find((item) => item.id === revisionId);
  if (!staleItem || staleItem.sources[0]?.filename !== "qa-browser-recovery.pdf") {
    throw new Error("Authorized Manager could not see the overdue statement and its source passage.");
  }
  await submitFreshnessReviewFromUi(knowledgeStatement, revisionId);
  evidence.push("C09_SCOPED_FRESHNESS_QUEUE_AUTHOR_SOURCE_AND_PUBLISHER_DENIALS=PASS");
  evidence.push("C09_MANAGER_BROWSER_REVIEW_APPEND_ONLY_EVENT_AND_DEADLINE=PASS");
}

async function submitFreshnessReviewFromUi(statement, revisionId) {
  await navigate("Approvals", "Approvals");
  if (!await waitFor(`document.body.innerText.includes(${JSON.stringify(statement)})`)) {
    throw new Error("C09 stale statement did not render in the approval UI.");
  }
  await evaluate(`(()=>{window.__qaFreshnessReviewCalls=0;window.__qaFreshnessReviewStatus=null;const original=window.fetch.bind(window);window.fetch=async(input,init)=>{const url=typeof input==='string'?input:input.url;const response=await original(input,init);if(new URL(url,location.href).pathname.endsWith('/freshness-review')){window.__qaFreshnessReviewCalls++;window.__qaFreshnessReviewStatus=response.status}return response}})()`);
  const fill = await evaluate(`(()=>{const card=[...document.querySelectorAll('article.approval-card')].find(item=>item.querySelector('.approval-statement')?.textContent?.trim()===${JSON.stringify(statement)});const field=card?.querySelector('textarea');if(!field)return false;Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(field,'Rechecked the cited synthetic source and confirmed it remains current.');field.dispatchEvent(new Event('input',{bubbles:true}));return true})()`);
  const enabled = fill && await waitFor(`(()=>{const card=[...document.querySelectorAll('article.approval-card')].find(item=>item.querySelector('.approval-statement')?.textContent?.trim()===${JSON.stringify(statement)});const button=[...(card?.querySelectorAll('button')??[])].find(item=>item.textContent.trim()==='Confirm still current');return Boolean(button&&!button.disabled)})()`);
  const clicked = enabled && await evaluate(`(()=>{const card=[...document.querySelectorAll('article.approval-card')].find(item=>item.querySelector('.approval-statement')?.textContent?.trim()===${JSON.stringify(statement)});const button=[...(card?.querySelectorAll('button')??[])].find(item=>item.textContent.trim()==='Confirm still current');if(!button||button.disabled)return false;button.click();return true})()`);
  const submitted = clicked && await waitFor("window.__qaFreshnessReviewCalls>0");
  const responseStatus = await evaluate("window.__qaFreshnessReviewStatus");
  if (!submitted || responseStatus !== 200) {
    throw new Error(`C09 freshness review UI failed (fill=${Boolean(fill)}, enabled=${Boolean(enabled)}, clicked=${Boolean(clicked)}, response=${responseStatus ?? "none"}).`);
  }
  if (!await waitFor(`(async()=>{const r=await fetch('/api/governance/knowledge/stale',{credentials:'same-origin'});if(!r.ok)return false;const body=await r.json();return !body.items.some(item=>item.id===${JSON.stringify(revisionId)})})()`)) {
    throw new Error("The reviewed item remained in the overdue queue after the browser action.");
  }
  databaseFreshnessReviewState(revisionId);
}

async function reviewExistingFreshnessItem() {
  await signIn("manager");
  const queue = await expect("GET", "/api/governance/knowledge/stale", 200);
  const item = queue.items.find((entry) => entry.sources.some((source) => source.filename === "qa-browser-recovery.pdf"));
  if (!item) throw new Error("No prepared synthetic overdue item is available in the disposable C09 clone.");
  await submitFreshnessReviewFromUi(item.statement, item.id);
  evidence.push("C09_MANAGER_BROWSER_REVIEW_APPEND_ONLY_EVENT_AND_DEADLINE=PASS");
}

async function runBrowserAcceptance() {
  const activePortFile = path.join(profile, "DevToolsActivePort");
  const deadline = Date.now() + 30_000;
  while (!existsSync(activePortFile) && Date.now() < deadline && browser.exitCode === null) await delay(100);
  if (!existsSync(activePortFile)) throw new Error("Browser did not publish its local debugging port.");
  let port = 0;
  let portFileError = null;
  while (!port && Date.now() < deadline && browser.exitCode === null) {
    try {
      const parsed = Number(readFileSync(activePortFile, "utf8").split(/\r?\n/)[0]);
      if (Number.isInteger(parsed) && parsed > 0 && parsed <= 65_535) port = parsed;
    } catch (error) {
      if (!["EBUSY", "EACCES", "EPERM", "ENOENT"].includes(error?.code)) throw error;
      portFileError = error.code;
    }
    if (!port) await delay(100);
  }
  if (!port) throw new Error(`Browser debugging port file remained unavailable${portFileError ? ` (${portFileError})` : ""}.`);
  let target;
  const targetDeadline = Date.now() + 30_000;
  while (!target && Date.now() < targetDeadline) {
    try {
      const response = await fetch(`http://127.0.0.1:${port}/json/list`);
      if (response.ok) target = (await response.json()).find((item) => item.type === "page" && item.webSocketDebuggerUrl);
    } catch {}
    if (!target) await delay(150);
  }
  if (!target) throw new Error("Browser page target did not become ready.");
  cdp = connectDebugger(target.webSocketDebuggerUrl);
  await cdp.ready();
  await cdp.command("Page.enable");
  await cdp.command("Runtime.enable");
  await delay(1_000);
  const loginVisible = await waitFor(`Boolean(document.querySelector('form.login-form'))`);
  if (!loginVisible) throw new Error("The candidate sign-in form was not rendered.");

  if (process.env.INFRASPINE_QA_LOGIN_ONLY === "1") {
    await signIn("admin");
    const basicPage = await evaluate(`(()=>({signedIn:!document.querySelector('input[name="email"]'),heading:document.querySelector('main h1, h1')?.textContent?.trim()??'',error:Boolean(document.querySelector('.banner-error,.inline-alert-error'))}))()`);
    if (!basicPage?.signedIn || !basicPage.heading || basicPage.error) throw new Error("Admin login succeeded but the basic workspace page did not render cleanly.");
    await signOut();
    evidence.push("SYNTHETIC_ADMIN_LOGIN_BASIC_PAGE=PASS");
    return;
  }

  if (process.env.INFRASPINE_QA_C03_LICENSE_ONLY === "1") {
    await auditOfflineLicenseAdminUI();
    return;
  }

  if (process.env.INFRASPINE_QA_F02_ONLY === "1") {
    await auditMeetingMinutesAcceptance();
    return;
  }

  if (process.env.INFRASPINE_QA_F10_F15_ONLY === "1") {
    await signIn("admin");
    const users = (await expect("GET", "/api/admin/users", 200)).users;
    for (const role of roles) {
      const account = users.find((item) => item.email.toLowerCase() === credentials[role].email.toLowerCase() && item.role === role);
      if (!account) throw new Error(`The protected synthetic ${role} account is missing from the candidate clone.`);
      identity[role] = account.id;
    }

    await signOut();
    await signIn("manager");
    const managerGroups = (await expect("GET", "/api/departments/managed", 200)).groups;
    await signOut();
    await signIn("employee");
    const employeeGroups = (await expect("GET", "/api/departments/mine", 200)).groups;
    let sharedDepartment = managerGroups.find((group) =>
      group.kind === "department" && employeeGroups.some((member) => member.id === group.id),
    );
    if (!sharedDepartment) {
      const managedDepartment = managerGroups.find((group) => group.kind === "department");
      if (!managedDepartment) throw new Error("The synthetic Manager has no managed department for F15 browser acceptance.");
      await signOut();
      await signIn("manager");
      await expect("POST", `/api/departments/${managedDepartment.id}/members`, 204, { email: credentials.employee.email });
      await signOut();
      await signIn("employee");
      const updatedEmployeeGroups = (await expect("GET", "/api/departments/mine", 200)).groups;
      sharedDepartment = managerGroups.find((group) =>
        group.kind === "department" && updatedEmployeeGroups.some((member) => member.id === group.id),
      );
      if (!sharedDepartment) throw new Error("The Manager could not establish the synthetic department fixture through the authorized membership API.");
    }

    await auditMyWorkAndExpertise(sharedDepartment);
    return;
  }

  if (process.env.INFRASPINE_QA_PEOPLE_ONLY === "1") {
    await auditPeopleInvitationControls();
    return;
  }

  if (process.env.INFRASPINE_QA_ROLE_TEMPLATES_ONLY === "1") {
    await auditCustomRoleTemplateControls();
    return;
  }

  if (process.env.INFRASPINE_QA_C04_VERSION_ONLY === "1") {
    await auditRoleTemplateVersionApproval();
    return;
  }

  if (process.env.INFRASPINE_QA_C08_ONLY === "1") {
    await signIn("admin");
    const financeDoc = (await expect("GET", "/api/rag/documents", 200)).documents
      .find((item) => item.filename === "qa-finance.pdf");
    if (!financeDoc) throw new Error("The restored clone does not contain its synthetic Finance source.");
    await navigate("Knowledge", "", ".chat-panel");
    const authorized = await expect("POST", "/api/rag/chat", 200, {
      question: "Summarize qa-finance.pdf.", document_ids: [financeDoc.id],
    });
    if (authorized.retrieved_chunks < 1 || authorized.insufficient_evidence ||
        !authorized.citations?.some((citation) => citation.document_id === financeDoc.id)) {
      throw new Error("Admin did not receive a cited answer to the authorized synthetic Finance source.");
    }
    evidence.push("C08_CURRENT_IMAGE_ADMIN_FINANCE_CITATION=PASS provider=mock");
    await signOut();

    await signIn("employee");
    await navigate("Knowledge", "", ".chat-panel");
    const denied = await expect("POST", "/api/rag/chat", 200, {
      question: "What was the reconciliation target for Finance batch F-42?",
      document_ids: [financeDoc.id],
    });
    if (denied.retrieved_chunks !== 0 || denied.citations?.length !== 0 ||
        denied.insufficient_evidence !== true || denied.general_knowledge !== false ||
        denied.strategy !== "none" || denied.provider !== null || denied.answer.includes("83,715")) {
      throw new Error("The current image did not refuse Employee Finance access without retrieval or provider use.");
    }
    evidence.push("C08_CURRENT_IMAGE_EMPLOYEE_REFUSAL_NO_PROVIDER=PASS");
    return;
  }

  if (process.env.INFRASPINE_QA_C06_VIDEO_ONLY === "1") {
    await auditVideoBrowserAcceptance();
    return;
  }

  if (process.env.INFRASPINE_QA_A07_CONFLICT_ONLY === "1") {
    await auditKnowledgeConflictAcceptance();
    return;
  }

  if (process.env.INFRASPINE_QA_A07_PROCESS_ONLY === "1") {
    await auditPolicyProcessImpactAcceptance();
    return;
  }

  if (process.env.INFRASPINE_QA_F01_ONLY === "1") {
    browserActionContext = "Admin workspace creation UI";
    await signIn("admin");
    await navigate("Client & project workspaces", "Client & project workspaces", "header.page-header h1");
    const suffix = Date.now().toString();
    const workspaceName = `QA project workspace ${suffix}`;
    const createSubmitted = await evaluate(`(()=>{
      const form=document.querySelectorAll('section.people-section form')[0];
      const kind=form?.querySelector('select');const name=form?.querySelector('input:not([type])');
      if(!form||!kind||!name)return false;
      Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set.call(kind,'project');kind.dispatchEvent(new Event('change',{bubbles:true}));
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(name,${JSON.stringify(workspaceName)});name.dispatchEvent(new Event('input',{bubbles:true}));
      const button=[...(form.querySelectorAll('button'))].find(item=>item.textContent.trim()==='Create workspace');
      if(!button||button.disabled)return false;button.click();return true;
    })()`);
    if (!createSubmitted || !await waitFor(`Boolean([...document.querySelectorAll('section.people-section option')].some(option=>option.textContent.includes(${JSON.stringify(workspaceName)})))`)) {
      throw new Error("Admin could not create a project workspace through the visible workspace form.");
    }
    const group = (await expect("GET", "/api/admin/access/groups", 200)).groups.find((item) => item.name === workspaceName);
    if (!group || group.kind !== "project" || group.source !== "local") throw new Error("The workspace UI did not persist a local project group.");

    browserActionContext = "workspace membership UI and role boundaries";
    const membershipSectionIndex = 1;
    const addSubmitted = await evaluate(`(()=>{
      const section=document.querySelectorAll('section.people-section')[${membershipSectionIndex}];const form=section?.querySelector('form');const input=form?.querySelector('input[type=email]');
      if(!form||!input)return false;
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,${JSON.stringify(credentials.employee.email)});input.dispatchEvent(new Event('input',{bubbles:true}));
      const button=[...(form.querySelectorAll('button'))].find(item=>item.textContent.trim()==='Add member');if(!button||button.disabled)return false;button.click();return true;
    })()`);
    if (!addSubmitted) throw new Error("Admin could not submit the existing Employee account through the workspace membership UI.");
    const employeeUser = (await expect("GET", "/api/admin/users", 200)).users.find((item) => item.email.toLowerCase() === credentials.employee.email.toLowerCase());
    if (!employeeUser) throw new Error("The protected synthetic Employee was absent from the Admin user list.");
    if (!await waitFor(`(async()=>{const r=await fetch(${JSON.stringify(`/api/admin/access/groups/${group.id}/members`)},{credentials:'same-origin'});if(!r.ok)return false;const b=await r.json();return b.members.some(item=>item.user_id===${JSON.stringify(employeeUser.id)})})()`)) {
      throw new Error("Employee workspace membership was not persisted after the Admin UI action.");
    }
    evidence.push("F01_ADMIN_CREATE_PROJECT_AND_ADD_EMPLOYEE_UI=PASS");

    await signOut();
    await signIn("manager");
    const managerGroupsDenied = await request("GET", "/api/admin/access/groups");
    if (![401, 403].includes(managerGroupsDenied.status) || await evaluate(`Boolean([...document.querySelectorAll('button.nav-item')].some(item=>item.textContent.trim()==='Client & project workspaces'))`)) {
      throw new Error("The Manager could see or administer client/project workspaces.");
    }
    await signOut();
    await signIn("employee");
    const employeeGroupsDenied = await request("GET", "/api/admin/access/groups");
    if (![401, 403].includes(employeeGroupsDenied.status) || await evaluate(`Boolean([...document.querySelectorAll('button.nav-item')].some(item=>item.textContent.trim()==='Client & project workspaces'))`)) {
      throw new Error("The Employee could see or administer client/project workspaces.");
    }
    await signOut();

    browserActionContext = "restricted workspace upload and worker";
    await signIn("admin");
    const filename = `qa-project-workspace-${suffix}.pdf`;
    const docId = await uploadRestrictedPdfFromUi(
      group.id,
      filename,
      "BT /F1 12 Tf 72 720 Td (Project workspace test token ORCHID 271. Synthetic restricted project evidence.) Tj ET",
    );
    evidence.push("F01_ADMIN_RESTRICTED_UPLOAD_UI_AND_WORKER=PASS");
    await signOut();

    browserActionContext = "workspace member search, citation and source preview";
    await signIn("employee");
    const memberDocs = (await expect("GET", "/api/rag/documents", 200)).documents;
    const memberDocument = memberDocs.find((item) => item.id === docId && item.filename === filename);
    if (!memberDocument) throw new Error("The assigned Employee could not see the workspace-restricted document.");
    const memberDetail = await expect("GET", `/api/rag/documents/${docId}`, 200);
    if (memberDetail.status !== "ready" || memberDetail.visibility !== "restricted") throw new Error("Employee document detail did not preserve workspace access metadata.");
    const answer = await expect("POST", "/api/rag/chat", 200, {
      question: "Summarize " + filename, document_ids: [docId],
    });
    if (answer.retrieved_chunks < 1 || !answer.citations?.some((citation) => citation.document_id === docId)) {
      throw new Error(`Workspace member answer metrics did not include this document (chunks=${answer.retrieved_chunks ?? "n/a"}, citations=${answer.citations?.length ?? "n/a"}, insufficient=${answer.insufficient_evidence}, provider=${answer.provider ?? "none"}).`);
    }
    const preview = await evaluate(`(async()=>{const r=await fetch(${JSON.stringify(`/api/rag/documents/${docId}/pages/1/image`)},{credentials:'same-origin'});const b=await r.arrayBuffer();return {status:r.status,type:r.headers.get('content-type')??'',bytes:b.byteLength}})()`);
    if (preview?.status !== 200 || preview.type !== "image/png" || preview.bytes < 100) throw new Error("The workspace member could not preview the original PDF page.");
    evidence.push(`F01_MEMBER_CITATION_AND_SOURCE_PREVIEW=PASS chunks=${answer.retrieved_chunks} preview_bytes=${preview.bytes}`);
    await signOut();

    browserActionContext = "nonmember and revoked-member refusal";
    await signIn("manager");
    const managerDocument = await request("GET", `/api/rag/documents/${docId}`);
    const managerAnswer = await expect("POST", "/api/rag/chat", 200, {
      question: "What is the project workspace test token?", document_ids: [docId],
    });
    if (managerDocument.status !== 404 || managerAnswer.retrieved_chunks !== 0 || managerAnswer.citations?.length !== 0 || managerAnswer.insufficient_evidence !== true) {
      throw new Error("A nonmember Manager received workspace document content or citations.");
    }
    await signOut();

    browserActionContext = "Admin workspace membership revocation UI";
    await signIn("admin");
    await navigate("Client & project workspaces", "Client & project workspaces", "header.page-header h1");
    if (!await waitFor(`(()=>{const section=document.querySelectorAll('section.people-section')[1];return Boolean([...((section?.querySelector('select'))?.options??[])].some(option=>option.value===${JSON.stringify(group.id)}))})()`)) {
      throw new Error("The Admin workspace selector did not load the newly created project group.");
    }
    const workspaceSelected = await evaluate(`(()=>{const section=document.querySelectorAll('section.people-section')[1];const select=section?.querySelector('select');if(!select)return false;Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set.call(select,${JSON.stringify(group.id)});select.dispatchEvent(new Event('change',{bubbles:true}));return true})()`);
    if (!workspaceSelected || !await waitFor(`Boolean([...document.querySelectorAll('.people-member-list li')].some(row=>row.textContent.toLowerCase().includes(${JSON.stringify(credentials.employee.email.toLowerCase())})))`)) {
      throw new Error("Admin workspace membership UI did not reload the synthetic Employee assignment.");
    }
    const removeClicked = await evaluate(`(()=>{const row=[...document.querySelectorAll('.people-member-list li')].find(item=>item.textContent.toLowerCase().includes(${JSON.stringify(credentials.employee.email.toLowerCase())}));const button=[...(row?.querySelectorAll('button')??[])].find(item=>item.textContent.trim()==='Remove member');if(!button)return false;button.click();return true})()`);
    const modalReady = removeClicked && await waitFor(`Boolean(document.querySelector('.modal[role=dialog] button.btn-danger'))`);
    const confirmed = modalReady && await evaluate(`(()=>{const button=[...document.querySelectorAll('.modal[role=dialog] button')].find(item=>item.textContent.trim()==='Remove member');if(!button)return false;button.click();return true})()`);
    if (!confirmed || !await waitFor(`(async()=>{const r=await fetch(${JSON.stringify(`/api/admin/access/groups/${group.id}/members`)},{credentials:'same-origin'});if(!r.ok)return false;const b=await r.json();return !b.members.some(item=>item.user_id===${JSON.stringify(employeeUser.id)})})()`)) {
      throw new Error("Admin could not revoke the Employee workspace assignment through the confirmed UI action.");
    }
    evidence.push("F01_ADMIN_REVOKE_WORKSPACE_MEMBERSHIP_UI=PASS");
    await signOut();

    await signIn("employee");
    const revokedDocument = await request("GET", `/api/rag/documents/${docId}`);
    const revokedAnswer = await expect("POST", "/api/rag/chat", 200, {
      question: "What is the project workspace test token?", document_ids: [docId],
    });
    if (revokedDocument.status !== 404 || revokedAnswer.retrieved_chunks !== 0 || revokedAnswer.citations?.length !== 0 || revokedAnswer.insufficient_evidence !== true || revokedAnswer.answer.includes("ORCHID 271")) {
      throw new Error("Removing workspace membership did not immediately revoke document retrieval and citations.");
    }
    evidence.push("F01_REVOKED_MEMBER_REFUSAL_NO_CITATION=PASS");
    return;
  }

  if (process.env.INFRASPINE_QA_REPAIR_MEMBERSHIP === "1") {
    await signIn("manager");
    const managed = (await expect("GET", "/api/departments/managed", 200)).groups;
    if (!managed.length) throw new Error("The protected Manager fixture has no department to repair.");
    await expect("POST", `/api/departments/${managed[0].id}/members`, 204, { email: credentials.employee.email });
    await signOut();
    evidence.push("SYNTHETIC_EMPLOYEE_DEPARTMENT_FIXTURE_RESTORED=PASS");
    return;
  }

  if (process.env.INFRASPINE_QA_C09_ONLY === "1") {
    await signIn("admin");
    await runKnowledgeFreshnessAcceptance();
    return;
  }

  if (process.env.INFRASPINE_QA_C09_REVIEW_EXISTING_ONLY === "1") {
    await reviewExistingFreshnessItem();
    return;
  }

  if (process.env.INFRASPINE_QA_COMPANY_SETUP_ONLY === "1") {
    await signIn("admin");
    await auditCompanySetup();
    return;
  }

  if (process.env.INFRASPINE_QA_WORKFLOW_ONLY === "1") {
    await signIn("admin");
    const users = (await expect("GET", "/api/admin/users", 200)).users;
    for (const role of roles) {
      const user = users.find((item) => item.email.toLowerCase() === credentials[role].email.toLowerCase() && item.role === role);
      if (!user) throw new Error("A protected synthetic role account is missing from the workflow QA database.");
      identity[role] = user.id;
    }
    await signOut();
    await signIn("manager");
    const managerGroups = (await expect("GET", "/api/departments/managed", 200)).groups;
    await signOut();
    await signIn("employee");
    const employeeGroups = (await expect("GET", "/api/departments/mine", 200)).groups;
    const sharedDepartment = managerGroups.find((group) =>
      group.kind === "department" && employeeGroups.some((member) => member.id === group.id));
    if (!sharedDepartment) throw new Error("Manager and Employee have no shared synthetic department for workflow acceptance.");
    await signOut();
    await signIn("manager");
    await navigate("Workflows & tasks", "Workflows & tasks");
    await auditWorkflowLifecycle(sharedDepartment.id);
    return;
  }

  await signIn("admin");
  await auditRoleSurfaces("admin");
  if (!await expect("GET", "/api/auth/me", 200).then((u) => u.role === "admin")) throw new Error("Admin role check failed.");
  const users = (await expect("GET", "/api/admin/users", 200)).users;
  const matchUser = (role) => users.find((user) => user.email.toLowerCase() === credentials[role].email.toLowerCase() && user.role === role);
  for (const role of roles) {
    const user = matchUser(role);
    if (!user) throw new Error(`The protected ${role} account is not present in the restored QA database.`);
    identity[role] = user.id;
  }
  if (!await evaluate(`Boolean([...document.querySelectorAll('button.nav-item')].some(x=>x.textContent.trim()==='People & access'))`)) {
    throw new Error("Admin-only navigation is missing for the Admin role.");
  }
  await auditCompanySetup();

  await uploadSyntheticAudio();

  const docs = (await expect("GET", "/api/rag/documents", 200)).documents;
  const financeDoc = docs.find((doc) => doc.filename.toLowerCase() === "qa-finance.pdf" && doc.status === "ready");
  const operationsDoc = docs.find((doc) => doc.filename.toLowerCase() === "qa-operations.pdf" && doc.status === "ready");
  if (!financeDoc || !operationsDoc) throw new Error("Expected restored Finance and Operations synthetic documents are not ready.");
  documentIds.finance = financeDoc.id;
  documentIds.operations = operationsDoc.id;

  await navigate("Knowledge", "", ".chat-panel");
  const uploadId = await uploadSyntheticPdf();
  documentIds.upload = uploadId;
  if (process.env.INFRASPINE_QA_TRAINING_SEED === "1") {
    adaptiveTrainingCourseId = seedAdaptiveTrainingCourse(identity.employee, uploadId);
    evidence.push("F13_SYNTHETIC_COURSE_SEEDED=PASS");
  }

  await signOut();
  await signIn("manager");
  await auditRoleSurfaces("manager");
  const managerGroups = (await expect("GET", "/api/departments/managed", 200)).groups;
  await signOut();
  await signIn("employee");
  await auditRoleSurfaces("employee");
  await auditAdaptiveTraining();
  const employeeGroups = (await expect("GET", "/api/departments/mine", 200)).groups;
  const sharedDepartment = managerGroups.find((group) => group.kind === "department" && employeeGroups.some((member) => member.id === group.id));
  if (!sharedDepartment) throw new Error("The restored Manager and Employee have no shared department for F11 acceptance.");
  documentIds.department = sharedDepartment.id;

  await signOut();
  await signIn("admin");
  const assigneeAccount = {
    email: `qa-assignee-${Date.now()}@example.test`,
    password: randomBytes(32).toString("base64url"),
  };
  temporaryAssignee = assigneeAccount;
  const invitation = await expect("POST", "/api/admin/users/invitations", 201, {
    email: assigneeAccount.email, role: "employee", department_ids: [sharedDepartment.id],
  });
  if (typeof invitation.token !== "string" || !invitation.token) throw new Error("Synthetic F11 assignee invitation lacked a token.");
  await expect("POST", "/api/onboarding/accept", 200, {
    token: invitation.token, password: assigneeAccount.password,
  });
  const usersAfterInvite = (await expect("GET", "/api/admin/users", 200)).users;
  const assignee = usersAfterInvite.find((user) => user.email.toLowerCase() === assigneeAccount.email.toLowerCase() && user.role === "employee");
  if (!assignee?.is_active || !assignee.departments.some((department) => department.id === sharedDepartment.id)) {
    throw new Error("Synthetic F11 assignee was not onboarded into the test department.");
  }
  identity.assignee = assignee.id;
  temporaryAssigneeId = assignee.id;
  evidence.push("SYNTHETIC_ASSIGNEE_ONBOARDING=PASS");

  await signOut();
  await signIn("employee");
  await navigate("Help & requests", "Help & requests");
  const unique = `QA browser request ${Date.now()}`;
  const requestBody = {
    category: "it", priority: "normal", department_group_id: sharedDepartment.id,
    subject: unique, description: "Synthetic QA browser acceptance request; contains no credentials or customer information.",
  };
  const serviceRequest = await expect("POST", "/api/service-requests", 201, requestBody);
  if (serviceRequest.status !== "submitted" || !serviceRequest.can_cancel || serviceRequest.events?.length !== 1) {
    throw new Error("Employee service request did not persist its initial state and event.");
  }
  const hrRequest = await expect("POST", "/api/service-requests", 201, {
    ...requestBody, category: "hr", subject: `${unique} HR`, description: "Private synthetic HR request for visibility testing.",
  });
  markServiceRequestsOverdue([serviceRequest.id, hrRequest.id]);
  evidence.push("F11_SLA_TEST_REQUESTS_PREPARED=PASS");
  evidence.push("F11_EMPLOYEE_CREATE=PASS");
  evidence.push("F11_INITIAL_HISTORY=PASS");

  await signOut();
  await signIn("manager");
  await navigate("Help & requests", "Help & requests");
  const managerList = (await expect("GET", "/api/service-requests", 200)).items;
  const managerRequest = managerList.find((item) => item.id === serviceRequest.id);
  if (!managerRequest?.can_manage || managerList.some((item) => item.id === hrRequest.id)) {
    throw new Error("Manager F11 scope or private HR visibility was incorrect.");
  }
  const managerNotificationReady = await waitFor(
    `(async()=>{const response=await fetch('/api/service-requests/notifications',{credentials:'same-origin'});if(!response.ok)return false;const body=await response.json();return body.items.some(item=>item.request_id===${JSON.stringify(serviceRequest.id)})})()`,
    45_000,
  );
  if (!managerNotificationReady) throw new Error("The background worker did not create the department manager's due-response alert.");
  const managerNotifications = (await expect("GET", "/api/service-requests/notifications", 200)).items;
  const managerAlert = managerNotifications.find((item) => item.request_id === serviceRequest.id);
  if (!managerAlert || managerNotifications.some((item) => item.request_id === hrRequest.id)) {
    throw new Error("SLA alert delivery leaked a private HR request or omitted the scoped manager alert.");
  }
  await navigate("Knowledge", "", ".chat-panel");
  await navigate("Help & requests", "Help & requests");
  const refreshAlerts = await evaluate(`(()=>{const button=[...document.querySelectorAll('button')].find(item=>item.textContent.trim()==='Refresh');if(!button)return false;button.click();return true})()`);
  if (!refreshAlerts || !await waitFor(`document.querySelector('section[aria-label="Service request notifications"]')?.textContent.includes(${JSON.stringify(serviceRequest.id.slice(0, 8))})`)) {
    throw new Error("The Manager Help & requests page did not render the background SLA alert.");
  }
  const markReadClicked = await evaluate(`(()=>{const section=document.querySelector('section[aria-label="Service request notifications"]');const row=[...(section?.querySelectorAll('li')??[])].find(item=>item.textContent.includes(${JSON.stringify(serviceRequest.id.slice(0, 8))}));const button=[...(row?.querySelectorAll('button')??[])].find(item=>item.textContent.trim()==='Mark read');if(!button)return false;button.click();return true})()`);
  if (!markReadClicked || !await waitFor(`(async()=>{const response=await fetch('/api/service-requests/notifications',{credentials:'same-origin'});if(!response.ok)return false;const body=await response.json();return body.items.some(item=>item.id===${JSON.stringify(managerAlert.id)}&&Boolean(item.read_at))})()`)) {
    throw new Error("Manager could not mark the SLA alert read through the browser UI.");
  }
  evidence.push("F11_SLA_WORKER_MANAGER_UI_ALERT_AND_HR_PRIVACY=PASS");
  const assigned = await expect("POST", `/api/service-requests/${serviceRequest.id}/triage`, 200, {
    status: "in_progress", assigned_to_user_id: identity.assignee, comment: "Synthetic request assigned during QA acceptance.",
  });
  const assignedActions = assigned.events?.map((event) => event.action) ?? [];
  if (assigned.assigned_to_user_id !== identity.assignee || assignedActions.length !== 3 ||
      !assignedActions.includes("submitted") || !assignedActions.includes("sla_escalated") ||
      assignedActions.filter((action) => action === "status_changed").length !== 1) {
    throw new Error("Manager assignment or request history did not persist.");
  }
  evidence.push("F11_MANAGER_ASSIGN=PASS");

  await signOut();
  await signInAccount(assigneeAccount, "employee");
  await navigate("Help & requests", "Help & requests");
  const assignedRead = await expect("GET", `/api/service-requests/${serviceRequest.id}`, 200);
  if (assignedRead.assigned_to_user_id !== identity.assignee) throw new Error("Assigned Employee could not read the request.");
  const comment = await expect("POST", `/api/service-requests/${serviceRequest.id}/comments`, 200, { comment: "Synthetic follow-up recorded by the assigned employee." });
  const commentActions = comment.events?.map((event) => event.action) ?? [];
  if (commentActions.length !== 4 || commentActions.at(-1) !== "commented" ||
      !commentActions.includes("sla_escalated")) throw new Error("Employee comment was not appended to the request timeline.");
  evidence.push("F11_ASSIGNED_USER_AND_HISTORY=PASS");

  await signOut();
  await signIn("manager");
  await expect("DELETE", `/api/departments/${sharedDepartment.id}/members/${identity.assignee}`, 204);
  await signOut();
  await signInAccount(assigneeAccount, "employee");
  const revoked = await request("GET", `/api/service-requests/${serviceRequest.id}`);
  const employeeListAfterRevocation = (await expect("GET", "/api/service-requests", 200)).items;
  if (revoked.status !== 404 || employeeListAfterRevocation.some((item) => item.id === serviceRequest.id)) {
    throw new Error("Department membership revocation did not remove assigned-user access.");
  }
  evidence.push("F11_MEMBERSHIP_REVOCATION=PASS");

  await signOut();
  await signIn("employee");
  await expect("GET", `/api/service-requests/${serviceRequest.id}`, 200);
  await navigate("Knowledge", "", ".chat-panel");

  await signOut();
  await signIn("admin");
  await navigate("Knowledge", "", ".chat-panel");
  const financeAnswer = await expect("POST", "/api/rag/chat", 200, {
    question: "Summarize qa-finance.pdf.",
    document_ids: [financeDoc.id],
  });
  const financeAuthorized = financeAnswer.citations?.some((citation) => citation.document_id === financeDoc.id && citation.filename === "qa-finance.pdf");
  if (!financeAuthorized || financeAnswer.retrieved_chunks < 1 || financeAnswer.insufficient_evidence) {
    throw new Error("Admin did not receive a citation to the authorized synthetic Finance source.");
  }
  evidence.push("C08_ADMIN_FINANCE_CITATION=PASS provider=mock");

  await signOut();
  await signIn("employee");
  await navigate("Knowledge", "", ".chat-panel");
  const deniedFinance = await expect("POST", "/api/rag/chat", 200, {
    question: "What was the reconciliation target for Finance batch F-42?",
    document_ids: [financeDoc.id],
  });
  if (deniedFinance.retrieved_chunks !== 0 || deniedFinance.citations?.length !== 0 ||
      deniedFinance.insufficient_evidence !== true || deniedFinance.general_knowledge !== false ||
      deniedFinance.strategy !== "none" || deniedFinance.provider !== null || deniedFinance.answer.includes("83,715")) {
    throw new Error("C08 authorization/refusal check did not fail closed for Employee Finance access.");
  }
  evidence.push("C08_EMPLOYEE_REFUSAL_NO_PROVIDER=PASS");

  const uploadAnswer = await expect("POST", "/api/rag/chat", 200, {
    question: "Summarize qa-browser-recovery.pdf.",
    document_ids: [uploadId],
  });
  const uploadCitation = uploadAnswer.citations?.find((citation) => citation.document_id === uploadId);
  if (!uploadCitation || uploadAnswer.retrieved_chunks < 1) throw new Error("Employee answer for the processed upload lacked an authorized citation.");
  const page = uploadCitation.page_number ?? 1;
  const preview = await evaluate(`(async()=>{const r=await fetch(${JSON.stringify(`/api/rag/documents/${uploadId}/pages/${page}/image`)},{credentials:'same-origin'});const b=await r.arrayBuffer();return {status:r.status,type:r.headers.get('content-type')??'',bytes:b.byteLength}})()`);
  if (preview?.status !== 200 || !preview.type.startsWith("image/") || preview.bytes < 1000) {
    throw new Error("Original source page preview did not return a rendered image.");
  }
  evidence.push(`RAG_CITATION_PREVIEW=PASS page=${page} bytes=${preview.bytes}`);

  if (documentIds.media) {
    const audioAnswer = await expect("POST", "/api/rag/chat", 200, {
      question: "What ticket number was mentioned in the maintenance window audio?",
      document_ids: [documentIds.media],
    }, 120_000);
    const audioCitation = audioAnswer.citations?.find((citation) => citation.document_id === documentIds.media);
    if (audioAnswer.provider === "mock") {
      // MockLLMClient deliberately does not extract the requested number from the transcript. The
      // secure behavior is to return insufficient evidence without inventing a grounded answer.
      if (audioAnswer.retrieved_chunks < 1 || audioAnswer.insufficient_evidence !== true ||
          audioAnswer.citations?.length !== 0 || /4821/.test(audioAnswer.answer ?? "")) {
        throw new Error("Mock-mode audio chat did not fail closed when its provider could not answer from the transcript.");
      }
      evidence.push("REAL_ASR_MOCK_REFUSAL_NO_FABRICATED_ANSWER=PASS");
    } else {
      if (!audioCitation || audioAnswer.retrieved_chunks < 1 || !/4821/.test(audioCitation.snippet) ||
          audioCitation.locator?.kind !== "time" || !/^\d{2}:\d{2}:\d{2}/.test(audioCitation.location ?? "")) {
        throw new Error("Employee audio answer did not contain a timestamped citation for the synthetic spoken ticket number.");
      }
      evidence.push(`REAL_ASR_TIMESTAMP_CITATION=PASS location=${audioCitation.location}`);
    }
    const mediaPreview = await evaluate(`(async()=>{const r=await fetch(${JSON.stringify(`/api/rag/documents/${documentIds.media}/media`)},{credentials:'same-origin',headers:{Range:'bytes=0-63'}});const b=await r.arrayBuffer();return {status:r.status,type:r.headers.get('content-type')??'',range:r.headers.get('content-range')??'',bytes:b.byteLength}})()`);
    if (mediaPreview?.status !== 206 || !mediaPreview.type.startsWith("audio/") || mediaPreview.bytes !== 64 ||
        !/^bytes 0-63\//.test(mediaPreview.range)) {
      throw new Error("Employee audio source preview did not return the expected authorized byte range.");
    }
    evidence.push(`REAL_ASR_SOURCE_PREVIEW=PASS bytes=${mediaPreview.bytes}`);
  }

  await signOut();
  await signIn("manager");
  await navigate("Workflows & tasks", "Workflows & tasks");
  const managerWorkflows = (await expect("GET", "/api/automation/workflows", 200)).workflows;
  const workflow = managerWorkflows.find((item) => item.department_group_id === sharedDepartment.id && item.enabled && item.can_manage);
  if (!workflow) throw new Error("No enabled manager-scoped workflow exists for F14 browser acceptance.");
  const managerBefore = databaseWorkflowCounts();
  const clicked = await evaluate(`(()=>{const card=[...document.querySelectorAll('.automation-item')].find(x=>x.querySelector('h3')?.textContent.trim()===${JSON.stringify(workflow.name)});const button=[...(card?.querySelectorAll('button')??[])].find(x=>x.textContent.trim()==='Validate flow');if(!button)return false;button.click();return true})()`);
  if (!clicked || !await waitFor(`Boolean(document.querySelector('.workflow-dry-run'))`)) throw new Error("Manager could not run the workflow editor's dry-run action.");
  const managerPlan = await expect("POST", `/api/automation/workflows/${workflow.id}/dry-run`, 200, {});
  if (!managerPlan.valid || managerPlan.side_effects_performed !== false) throw new Error("Manager dry-run response was not a read-only valid plan.");
  const managerAfter = databaseWorkflowCounts();
  if (managerBefore !== managerAfter) throw new Error("Manager dry-run changed workflow, approval, task or audit rows.");
  evidence.push("F14_MANAGER_UI_DRY_RUN_NO_DB_WRITES=PASS");
  if (process.env.INFRASPINE_QA_WORKFLOW_LIFECYCLE === "1") {
    await auditWorkflowLifecycle(sharedDepartment.id);
  }
  await auditMyWorkAndExpertise(sharedDepartment);

  // Exercise the real agent catalog UI and its per-request ACL boundary on the same synthetic clone.
  await navigate("Agent profiles", "Agent profiles");
  const agentName = `QA department researcher ${Date.now()}`;
  const manageableDepartments = (await expect("GET", "/api/agents/departments", 200)).departments;
  if (!manageableDepartments.some((item) => item.id === sharedDepartment.id)) {
    throw new Error("Manager agent catalog did not offer its managed department.");
  }
  if (!await waitFor(`Boolean([...document.querySelectorAll('form.automation-form')].find(f=>f.querySelector('input[maxlength="120"]')))`)) {
    throw new Error("Manager agent catalog did not finish loading its profile form.");
  }
  const createAgent = await evaluate(`(()=>{const form=[...document.querySelectorAll('form.automation-form')].find(f=>f.querySelector('input[maxlength="120"]'));if(!form)return false;const name=form.querySelector('input[maxlength="120"]');const select=form.querySelector('select');if(!name||!select)return false;const set=(el,value,proto)=>{Object.getOwnPropertyDescriptor(proto,'value').set.call(el,value);el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}))};set(name,${JSON.stringify(agentName)},HTMLInputElement.prototype);set(select,${JSON.stringify(sharedDepartment.id)},HTMLSelectElement.prototype);const button=[...form.querySelectorAll('button[type="submit"]')].find(b=>b.textContent.includes('Create agent profile'));if(!button||button.disabled)return false;button.click();return true})()`);
  if (!createAgent) {
    const fields = await evaluate(`(()=>{const form=[...document.querySelectorAll('form.automation-form')].find(f=>f.querySelector('input[maxlength="120"]'));const button=[...(form?.querySelectorAll('button[type="submit"]')??[])].find(b=>b.textContent.includes('Create agent profile'));return {form:Boolean(form),name:Boolean(form?.querySelector('input[maxlength="120"]')),department:Boolean(form?.querySelector('select')?.value),button:Boolean(button),disabled:Boolean(button?.disabled)}})()`);
    throw new Error(`Manager agent profile form was unavailable or disabled (${JSON.stringify(fields)}).`);
  }
  if (!await waitFor(`document.querySelector('.banner-success')?.textContent.includes('Agent profile saved')`)) {
    const message = await evaluate(`(()=>({success:document.querySelector('.banner-success')?.textContent?.trim()??'',error:document.querySelector('.banner-error')?.textContent?.trim()??''}))()`);
    throw new Error(`Manager agent profile creation did not succeed (${JSON.stringify(message)}).`);
  }
  const createdProfile = (await expect("GET", "/api/agents", 200)).agents.find((item) => item.name === agentName && item.enabled);
  if (!createdProfile?.can_manage || createdProfile.department_group_id !== sharedDepartment.id) {
    throw new Error("Created agent profile did not retain its department manager scope.");
  }
  const editClicked = await evaluate(`(()=>{const card=[...document.querySelectorAll('.automation-item')].find(x=>x.querySelector('h3')?.textContent.trim()===${JSON.stringify(agentName)});const button=[...(card?.querySelectorAll('button')??[])].find(x=>x.textContent.trim()==='Create next version');if(!button)return false;button.click();return true})()`);
  if (!editClicked || !await waitFor(`document.querySelector('.agent-catalog-note')&&[...document.querySelectorAll('h2')].some(x=>x.textContent.trim()==='Create version 2')`)) {
    throw new Error("Manager could not open the agent version editor.");
  }
  const versionName = `${agentName} v2`;
  const saveVersion = await evaluate(`(()=>{const form=[...document.querySelectorAll('form.automation-form')].find(f=>f.querySelector('input[maxlength="120"]'));const name=form?.querySelector('input[maxlength="120"]');if(!name)return false;Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(name,${JSON.stringify(versionName)});name.dispatchEvent(new Event('input',{bubbles:true}));const button=[...form.querySelectorAll('button[type="submit"]')].find(b=>b.textContent.includes('Save new version'));if(!button||button.disabled)return false;button.click();return true})()`);
  if (!saveVersion || !await waitFor(`document.querySelector('.banner-success')?.textContent.includes('new immutable version')`)) {
    throw new Error("Manager could not save a new immutable agent profile version through the browser UI.");
  }
  const managerVersions = (await expect("GET", "/api/agents", 200)).agents.filter((item) => item.agent_key === createdProfile.agent_key);
  if (managerVersions.length !== 2 || managerVersions.find((item) => item.version === 1)?.enabled !== false ||
      managerVersions.find((item) => item.version === 2)?.name !== versionName) {
    throw new Error("Agent profile version history was not immutable or did not activate the new version.");
  }
  evidence.push("AGENT_MANAGER_CREATE_AND_VERSION_UI=PASS");

  await signOut();
  await signIn("employee");
  await navigate("Workflows & tasks", "Workflows & tasks");
  const employeeHasValidate = await evaluate(`Boolean([...document.querySelectorAll('button')].some(x=>x.textContent.trim()==='Validate flow'))`);
  const employeeBefore = databaseWorkflowCounts();
  const deniedDryRun = await request("POST", `/api/automation/workflows/${workflow.id}/dry-run`, {});
  if (employeeHasValidate || deniedDryRun.status !== 403) throw new Error("Employee dry-run control or backend permission denial was incorrect.");
  if (employeeBefore !== databaseWorkflowCounts()) throw new Error("Denied Employee dry-run changed workflow rows.");
  evidence.push("F14_EMPLOYEE_DRY_RUN_DENIED=PASS");

  await navigate("Agent profiles", "Agent profiles");
  const employeeProfiles = (await expect("GET", "/api/agents", 200)).agents;
  const employeeAgent = employeeProfiles.find((item) => item.agent_key === createdProfile.agent_key && item.version === 2 && item.enabled);
  if (!employeeAgent || employeeAgent.can_manage) throw new Error("Employee did not receive only the active, read-only agent profile version.");
  const employeeCard = await evaluate(`(()=>{const card=[...document.querySelectorAll('.automation-item')].find(x=>x.querySelector('h3')?.textContent.trim()===${JSON.stringify(versionName)});if(!card)return false;return {run:[...card.querySelectorAll('button')].some(b=>b.textContent.trim()==='Run profile'),edit:[...card.querySelectorAll('button')].some(b=>b.textContent.trim()==='Create next version'||b.textContent.trim()==='Disable')}})()`);
  if (!employeeCard?.run || employeeCard.edit) throw new Error("Employee agent controls did not expose run-only access.");
  const runClicked = await evaluate(`(()=>{const card=[...document.querySelectorAll('.automation-item')].find(x=>x.querySelector('h3')?.textContent.trim()===${JSON.stringify(versionName)});const button=[...(card?.querySelectorAll('button')??[])].find(b=>b.textContent.trim()==='Run profile');if(!button)return false;button.click();return true})()`);
  if (!runClicked || !await waitFor(`Boolean(document.querySelector('.agent-runner'))`)) throw new Error("Employee could not open the permitted agent runner.");
  const runAgent = await evaluate(`(()=>{const form=document.querySelector('.agent-runner form');const input=form?.querySelector('textarea');if(!form||!input)return false;Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(input,'Summarize the safe restore procedure in qa-browser-recovery.pdf.');input.dispatchEvent(new Event('input',{bubbles:true}));const button=[...form.querySelectorAll('button[type="submit"]')].find(b=>b.textContent.includes('Run agent'));if(!button||button.disabled)return false;button.click();return true})()`);
  if (!runAgent || !await waitFor(`Boolean(document.querySelector('.agent-run-output'))`, 60_000)) throw new Error("Employee agent run did not render its result.");
  const agentRun = await expect("POST", `/api/agents/${employeeAgent.id}/runs`, 200, {
    capability: "knowledge_search", task: "Summarize the safe restore procedure in qa-browser-recovery.pdf.",
  });
  const employeeVisibleIds = new Set((await expect("GET", "/api/rag/documents", 200)).documents.map((item) => item.id));
  const citations = agentRun.result.citations ?? [];
  const renderedAgentSources = await evaluate(`document.querySelectorAll('.agent-run-output .research-source').length`);
  if (agentRun.result.retrieved_chunks < 1 || citations.length < 1 || citations.some((citation) => !employeeVisibleIds.has(citation.document_id)) ||
      citations.some((citation) => citation.document_id === financeDoc.id) || renderedAgentSources < 1) {
    throw new Error(`Employee agent citations were absent, unauthorized, or not rendered (chunks=${agentRun.result.retrieved_chunks}, citations=${citations.length}, visible=${citations.every((citation) => employeeVisibleIds.has(citation.document_id))}, finance=${citations.some((citation) => citation.document_id === financeDoc.id)}, rendered=${renderedAgentSources}).`);
  }
  const hiddenFinance = await expect("POST", `/api/agents/${employeeAgent.id}/runs`, 200, {
    capability: "knowledge_search", task: "What was the reconciliation target for Finance batch F-42?", document_ids: [financeDoc.id],
  });
  if (hiddenFinance.result.retrieved_chunks !== 0 || hiddenFinance.result.citations?.length !== 0 ||
      hiddenFinance.result.insufficient_evidence !== true || hiddenFinance.result.provider !== null ||
      hiddenFinance.result.answer.includes("83,715")) {
    throw new Error(`Employee agent Finance refusal failed (chunks=${hiddenFinance.result.retrieved_chunks}, citations=${hiddenFinance.result.citations?.length ?? 0}, insufficient=${hiddenFinance.result.insufficient_evidence}, provider=${hiddenFinance.result.provider ?? "none"}, restricted_fact=${hiddenFinance.result.answer.includes("83,715")}).`);
  }
  evidence.push("AGENT_EMPLOYEE_CITED_RUN_AND_FINANCE_REFUSAL=PASS");

  await signOut();
  await signIn("admin");
  const adminHrRequest = await expect("GET", `/api/service-requests/${hrRequest.id}`, 200);
  if (adminHrRequest.id !== hrRequest.id) throw new Error("Admin could not read the private HR request.");
  const adminNotifications = (await expect("GET", "/api/service-requests/notifications", 200)).items;
  if (!adminNotifications.some((item) => item.request_id === hrRequest.id)) {
    throw new Error("Admin did not receive the private HR response-deadline alert.");
  }
  evidence.push("F11_PRIVATE_HR_ADMIN_ONLY=PASS");
  evidence.push("F11_SLA_ADMIN_PRIVATE_HR_ALERT=PASS");
  await expect("POST", `/api/admin/users/${identity.assignee}/active`, 200, { active: false });
  temporaryAssigneeDeactivated = true;
  evidence.push("SYNTHETIC_ASSIGNEE_DEACTIVATED=PASS");
  await navigate("Agent profiles", "Agent profiles");
  if (!await waitFor(`Boolean([...document.querySelectorAll('.automation-item')].find(x=>x.querySelector('h3')?.textContent.trim()===${JSON.stringify(versionName)}))`)) {
    throw new Error("Admin agent profile cards did not load in the browser UI.");
  }
  const adminAgentVersions = (await expect("GET", "/api/agents", 200)).agents.filter((item) => item.agent_key === createdProfile.agent_key);
  const adminAgentCard = await evaluate(`(()=>{const card=[...document.querySelectorAll('.automation-item')].find(x=>x.querySelector('h3')?.textContent.trim()===${JSON.stringify(versionName)});return {version:card?.querySelector('.automation-item-heading .muted')?.textContent??'',canEdit:[...(card?.querySelectorAll('button')??[])].some(b=>b.textContent.trim()==='Create next version')}})()`);
  if (adminAgentVersions.length !== 2 || !adminAgentVersions.every((item) => item.can_manage) ||
      adminAgentVersions.find((item) => item.version === 1)?.enabled !== false || !adminAgentCard?.canEdit) {
    throw new Error("Admin could not review the immutable agent version history in the UI.");
  }
  evidence.push("AGENT_ADMIN_VERSION_HISTORY_UI=PASS");
  await navigate("Workflows & tasks", "Workflows & tasks");
  const adminWorkflows = (await expect("GET", "/api/automation/workflows", 200)).workflows;
  const adminWorkflow = adminWorkflows.find((item) => item.id === workflow.id && item.enabled);
  if (!adminWorkflow) throw new Error("Admin could not view the manager workflow.");
  const adminBefore = databaseWorkflowCounts();
  const adminClicked = await evaluate(`(()=>{const card=[...document.querySelectorAll('.automation-item')].find(x=>x.querySelector('h3')?.textContent.trim()===${JSON.stringify(workflow.name)});const button=[...(card?.querySelectorAll('button')??[])].find(x=>x.textContent.trim()==='Validate flow');if(!button)return false;button.click();return true})()`);
  if (!adminClicked || !await waitFor(`Boolean(document.querySelector('.workflow-dry-run'))`)) throw new Error("Admin could not run the workflow editor's dry-run action.");
  const adminPlan = await expect("POST", `/api/automation/workflows/${workflow.id}/dry-run`, 200, {});
  if (!adminPlan.valid || adminPlan.side_effects_performed !== false || adminBefore !== databaseWorkflowCounts()) {
    throw new Error("Admin dry-run failed validation or changed workflow data.");
  }
  evidence.push("F14_ADMIN_UI_DRY_RUN_NO_DB_WRITES=PASS");

  if (documentIds.media) {
    await expect("PUT", `/api/rag/documents/${documentIds.media}/access`, 200, {
      visibility: "private", user_emails: [], group_ids: [],
    });
    await signOut();
    await signIn("employee");
    await expect("GET", `/api/rag/documents/${documentIds.media}/media`, 404);
    const revokedAudio = await expect("POST", "/api/rag/chat", 200, {
      question: "What ticket number was mentioned in the maintenance window audio?",
      document_ids: [documentIds.media],
    });
    if (!revokedAudio.insufficient_evidence || revokedAudio.retrieved_chunks !== 0 || revokedAudio.citations.length !== 0) {
      throw new Error("Revoking audio access did not remove its transcript from employee retrieval.");
    }
    evidence.push("REAL_ASR_PERMISSION_REVOCATION=PASS");
  }

  const requestCounts = databaseRequestCounts();
  evidence.push(`F11_POSTGRES_REQUEST_EVENT_COUNTS=${requestCounts}`);
  if (cdp.exceptions.length) throw new Error(`Browser raised ${cdp.exceptions.length} uncaught page exception(s).`);
}

try {
  await runBrowserAcceptance();
  for (const line of evidence) console.log(line);
  console.log("BROWSER_ROLE_ACCEPTANCE=PASS");
} catch (error) {
  for (const line of evidence) console.log(line);
  console.error(`BROWSER_ROLE_ACCEPTANCE=FAIL ${error?.message ?? "unknown failure"}`);
  process.exitCode = 1;
} finally {
  if (temporaryAssignee && temporaryAssigneeId && !temporaryAssigneeDeactivated && cdp) {
    try {
      await signOut().catch(() => undefined);
      await signIn("admin");
      await expect("POST", `/api/admin/users/${temporaryAssigneeId}/active`, 200, { active: false });
      console.log("SYNTHETIC_ASSIGNEE_CLEANUP=PASS");
    } catch {
      console.error("SYNTHETIC_ASSIGNEE_CLEANUP=NEEDS_REVIEW");
    }
  }
  if (browser.exitCode === null && cdp) {
    try {
      await cdp.command("Browser.close", {}, 3_000);
    } catch {}
  }
  cdp?.socket.close();
  if (browser.exitCode === null) {
    browser.kill();
    await Promise.race([new Promise((resolve) => browser.once("exit", resolve)), delay(5_000)]);
  }
  const safeProfile = path.resolve(profile);
  const tempRoot = `${path.resolve(tmpdir())}${path.sep}`;
  if (safeProfile.startsWith(tempRoot) && path.basename(safeProfile).startsWith("infraspine-qa-browser-")) {
    let cleanupError;
    for (let attempt = 0; attempt < 20 && existsSync(safeProfile); attempt += 1) {
      try {
        rmSync(safeProfile, { recursive: true, force: true, maxRetries: 2, retryDelay: 100 });
        cleanupError = undefined;
      } catch (error) {
        cleanupError = error;
        if (!['EPERM', 'EBUSY', 'ENOTEMPTY'].includes(error?.code)) throw error;
        await delay(250);
      }
    }
    if (existsSync(safeProfile)) {
      throw new Error(`Could not clean the temporary browser profile${cleanupError?.code ? ` (${cleanupError.code})` : ''}.`);
    }
  }
}
