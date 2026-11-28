// Drive, documents, approvals, boards, projects, attendance/leave.
import { randomBytes } from "node:crypto";
import { mkdirSync, readFileSync, unlinkSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { one, pool, rows, run } from "./db.ts";
import { bad, date, forbidden, HttpError, ids, int, notFound, oneOf, str } from "./http.ts";
import { admin, audit, notify, type Route } from "./routes1.ts";

const filesDir = process.env.WORK_FILES_DIR ?? join(dirname(fileURLToPath(import.meta.url)), "../../../database/work-files");
const MAX_FILE = 15 * 1024 * 1024;
const BLOCKED = /\.(exe|bat|cmd|com|scr|msi|js|jar|sh|app|html?|svg)$/i; // files are only ever downloaded as attachments, but nothing executable is stored

export const routes2: Route[] = [
  // ---- 드라이브 ----
  ["GET", /^\/drive$/, async ({ member, query }) => {
    const parent = query.get("parent") ? int(query.get("parent"), "parent") : null;
    const trash = query.get("trash") === "1";
    const visible = "(n.owner_id=? OR EXISTS(SELECT 1 FROM drive_shares s WHERE s.node_id=n.id AND (s.member_id=? OR s.department_id IN (SELECT department_id FROM department_members WHERE member_id=?))))";
    return rows(`SELECT n.id,n.parent_id,n.owner_id,n.kind,n.name,n.size_bytes,n.mime,n.created_at FROM drive_nodes n WHERE ${visible} AND ${trash ? "n.trashed_at IS NOT NULL" : "n.trashed_at IS NULL"} AND ${parent ? "n.parent_id=?" : trash ? "1=1" : "(n.parent_id IS NULL OR n.owner_id<>?)"} ORDER BY n.kind,n.name`,
      parent ? [member.id, member.id, member.id, parent] : trash ? [member.id, member.id, member.id] : [member.id, member.id, member.id, member.id]);
  }],
  ["POST", /^\/drive\/folders$/, async ({ member, body }) => {
    const parent = body.parentId ? int(body.parentId, "parentId") : null;
    if (parent && !(await one("SELECT 1 FROM drive_nodes WHERE id=? AND owner_id=? AND kind='folder'", [parent, member.id]))) throw forbidden();
    return { id: (await run("INSERT INTO drive_nodes (parent_id,owner_id,kind,name) VALUES (?,?,'folder',?)", [parent, member.id, str(body.name, 190, "name")])).insertId };
  }],
  ["POST", /^\/drive\/files$/, async ({ member, body }) => {
    const name = str(body.name, 190, "name")!;
    if (/[\\/\u0000-\u001f]/.test(name) || BLOCKED.test(name)) throw bad("name_invalid");
    const data = Buffer.from(str(body.dataBase64, MAX_FILE * 2, "data")!, "base64");
    if (!data.length || data.length > MAX_FILE) throw bad("size_invalid");
    const parent = body.parentId ? int(body.parentId, "parentId") : null;
    if (parent && !(await one("SELECT 1 FROM drive_nodes WHERE id=? AND owner_id=? AND kind='folder'", [parent, member.id]))) throw forbidden();
    const key = randomBytes(16).toString("hex");
    mkdirSync(filesDir, { recursive: true, mode: 0o700 });
    writeFileSync(join(filesDir, key), data, { mode: 0o600 });
    return { id: (await run("INSERT INTO drive_nodes (parent_id,owner_id,kind,name,size_bytes,mime,storage_key) VALUES (?,?,'file',?,?,?,?)", [parent, member.id, name, data.length, "application/octet-stream", key])).insertId };
  }],
  ["GET", /^\/drive\/(\d+)\/download$/, async ({ member, params }) => {
    const node = await one<{ name: string; storage_key: string }>("SELECT n.name,n.storage_key FROM drive_nodes n WHERE n.id=? AND n.kind='file' AND n.trashed_at IS NULL AND (n.owner_id=? OR EXISTS(SELECT 1 FROM drive_shares s WHERE s.node_id=n.id AND (s.member_id=? OR s.department_id IN (SELECT department_id FROM department_members WHERE member_id=?))))", [int(params[0], "id"), member.id, member.id, member.id]);
    if (!node) throw notFound();
    return { __file: true, name: node.name, data: readFileSync(join(filesDir, node.storage_key)) };
  }],
  ["POST", /^\/drive\/(\d+)\/share$/, async ({ member, params, body }) => {
    const id = int(params[0], "id");
    if (!(await one("SELECT 1 FROM drive_nodes WHERE id=? AND owner_id=?", [id, member.id]))) throw forbidden();
    await run("DELETE FROM drive_shares WHERE node_id=?", [id]);
    const permission = oneOf(body.permission ?? "view", ["view", "edit"], "permission");
    const m = ids(body.memberIds ?? [], "memberIds"), d = ids(body.departmentIds ?? [], "departmentIds");
    const list = [...m.map((x) => [id, x, null, permission]), ...d.map((x) => [id, null, x, permission])];
    if (list.length) await run("INSERT INTO drive_shares (node_id,member_id,department_id,permission) VALUES ?", [list]);
    return { ok: true };
  }],
  ["DELETE", /^\/drive\/(\d+)$/, async ({ member, params, query }) => {
    const id = int(params[0], "id");
    if (query.get("purge") === "1") {
      const node = await one<{ storage_key: string | null }>("SELECT storage_key FROM drive_nodes WHERE id=? AND owner_id=? AND trashed_at IS NOT NULL", [id, member.id]);
      if (!node) throw notFound();
      if (node.storage_key) { try { unlinkSync(join(filesDir, node.storage_key)); } catch { /* already gone */ } }
      await run("DELETE FROM drive_nodes WHERE id=?", [id]);
    } else await run("UPDATE drive_nodes SET trashed_at=NOW() WHERE id=? AND owner_id=?", [id, member.id]);
    return { ok: true };
  }],

  // ---- 문서 협업 ----
  ["GET", /^\/docs$/, async ({ member }) => rows("SELECT d.id,d.title,d.owner_id,x.display_name owner,d.version,d.updated_at FROM docs d JOIN members x ON x.id=d.owner_id WHERE d.owner_id=? OR EXISTS(SELECT 1 FROM doc_members m WHERE m.doc_id=d.id AND m.member_id=?) ORDER BY d.updated_at DESC", [member.id, member.id])],
  ["POST", /^\/docs$/, async ({ member, body }) => {
    const r = await run("INSERT INTO docs (title,owner_id,content) VALUES (?,?,?)", [str(body.title, 200, "title"), member.id, str(body.content ?? "", 1_000_000, "content", true) ?? ""]);
    await run("INSERT INTO doc_versions (doc_id,version,content,edited_by) VALUES (?,1,?,?)", [r.insertId, body.content ?? "", member.id]);
    return { id: r.insertId };
  }],
  ["GET", /^\/docs\/(\d+)$/, async ({ member, params }) => {
    const id = int(params[0], "id");
    const doc = await one("SELECT d.id,d.title,d.content,d.version,d.owner_id,d.updated_at,IF(d.owner_id=?,'edit',(SELECT permission FROM doc_members m WHERE m.doc_id=d.id AND m.member_id=?)) permission FROM docs d WHERE d.id=?", [member.id, member.id, id]);
    if (!doc || !doc.permission) throw notFound();
    return { ...doc, comments: await rows("SELECT c.id,x.display_name author,c.body,c.created_at FROM doc_comments c JOIN members x ON x.id=c.author_id WHERE c.doc_id=? ORDER BY c.id", [id]),
      members: await rows("SELECT m.member_id,x.display_name,m.permission FROM doc_members m JOIN members x ON x.id=m.member_id WHERE m.doc_id=?", [id]) };
  }],
  ["PUT", /^\/docs\/(\d+)$/, async ({ member, params, body }) => {
    const id = int(params[0], "id"), baseVersion = int(body.baseVersion, "baseVersion");
    const conn = await pool.getConnection();
    try {
      await conn.beginTransaction();
      const [found]: any = await conn.query("SELECT d.version,IF(d.owner_id=?,'edit',(SELECT permission FROM doc_members m WHERE m.doc_id=d.id AND m.member_id=?)) permission FROM docs d WHERE d.id=? FOR UPDATE", [member.id, member.id, id]);
      if (!found[0] || found[0].permission !== "edit") throw forbidden();
      if (found[0].version !== baseVersion) throw new HttpError(409, "version_conflict"); // someone saved first; the client reloads and merges
      const version = baseVersion + 1, content = str(body.content, 1_000_000, "content", true) ?? "";
      await conn.query("UPDATE docs SET content=?, version=?, title=COALESCE(?,title) WHERE id=?", [content, version, body.title ? str(body.title, 200, "title") : null, id]);
      await conn.query("INSERT INTO doc_versions (doc_id,version,content,edited_by) VALUES (?,?,?,?)", [id, version, content, member.id]);
      await conn.commit();
      return { version };
    } catch (error) { await conn.rollback(); throw error; } finally { conn.release(); }
  }],
  ["GET", /^\/docs\/(\d+)\/versions$/, async ({ member, params }) => {
    const id = int(params[0], "id");
    if (!(await one("SELECT 1 FROM docs d WHERE d.id=? AND (d.owner_id=? OR EXISTS(SELECT 1 FROM doc_members m WHERE m.doc_id=d.id AND m.member_id=?))", [id, member.id, member.id]))) throw notFound();
    return rows("SELECT v.version,x.display_name edited_by,v.edited_at FROM doc_versions v JOIN members x ON x.id=v.edited_by WHERE v.doc_id=? ORDER BY v.version DESC LIMIT 100", [id]);
  }],
  ["POST", /^\/docs\/(\d+)\/share$/, async ({ member, params, body }) => {
    const id = int(params[0], "id");
    if (!(await one("SELECT 1 FROM docs WHERE id=? AND owner_id=?", [id, member.id]))) throw forbidden();
    await run("DELETE FROM doc_members WHERE doc_id=?", [id]);
    const list = (Array.isArray(body.members) ? body.members : []).slice(0, 100).map((m: any) => [id, int(m.memberId, "memberId"), oneOf(m.permission, ["view", "comment", "edit"], "permission")]);
    if (list.length) await run("INSERT INTO doc_members (doc_id,member_id,permission) VALUES ?", [list]);
    await notify(list.map((l: any[]) => l[1]), "doc", id, "문서가 공유되었습니다", member.id);
    return { ok: true };
  }],
  ["POST", /^\/docs\/(\d+)\/comments$/, async ({ member, params, body }) => {
    const id = int(params[0], "id");
    const doc = await one<{ owner_id: number; permission: string | null }>("SELECT d.owner_id,IF(d.owner_id=?,'edit',(SELECT permission FROM doc_members m WHERE m.doc_id=d.id AND m.member_id=?)) permission FROM docs d WHERE d.id=?", [member.id, member.id, id]);
    if (!doc?.permission || doc.permission === "view") throw forbidden();
    const r = await run("INSERT INTO doc_comments (doc_id,author_id,body) VALUES (?,?,?)", [id, member.id, str(body.body, 4000, "body")]);
    await notify([doc.owner_id], "doc", id, `${member.name}님이 문서에 댓글을 남겼습니다`, member.id);
    return { id: r.insertId };
  }],
  ["DELETE", /^\/docs\/(\d+)$/, async ({ member, params }) => { await run("DELETE FROM docs WHERE id=? AND owner_id=?", [int(params[0], "id"), member.id]); return { ok: true }; }],

  // ---- 전자결재 ----
  ["GET", /^\/approvals\/forms$/, async () => rows("SELECT id,name,template FROM approval_forms WHERE active=1 ORDER BY id")],
  ["POST", /^\/approvals\/forms$/, admin(async ({ member, body }) => {
    const r = await run("INSERT INTO approval_forms (name,template) VALUES (?,?)", [str(body.name, 80, "name"), str(body.template ?? "", 20_000, "template", true) ?? ""]);
    await audit(member.id, "approval.form.create", String(r.insertId));
    return { id: r.insertId };
  })],
  ["GET", /^\/approvals$/, async ({ member, query }) => {
    const box = oneOf(query.get("box") ?? "todo", ["todo", "drafted", "done", "cc"], "box");
    const base = "SELECT a.id,a.title,a.status,a.submitted_at,x.display_name drafter FROM approvals a JOIN members x ON x.id=a.drafter_id";
    if (box === "drafted") return rows(`${base} WHERE a.drafter_id=? ORDER BY a.id DESC LIMIT 100`, [member.id]);
    if (box === "cc") return rows(`${base} WHERE a.status<>'draft' AND EXISTS(SELECT 1 FROM approval_steps s WHERE s.approval_id=a.id AND s.approver_id=? AND s.kind='cc') ORDER BY a.id DESC LIMIT 100`, [member.id]);
    if (box === "done") return rows(`${base} WHERE EXISTS(SELECT 1 FROM approval_steps s WHERE s.approval_id=a.id AND s.approver_id=? AND s.kind<>'cc' AND s.decision<>'waiting') ORDER BY a.id DESC LIMIT 100`, [member.id]);
    // todo: it is my turn = the lowest waiting step of a pending approval is mine
    return rows(`${base} WHERE a.status='pending' AND EXISTS(SELECT 1 FROM approval_steps s WHERE s.approval_id=a.id AND s.approver_id=? AND s.decision='waiting' AND s.kind<>'cc' AND s.seq=(SELECT MIN(w.seq) FROM approval_steps w WHERE w.approval_id=a.id AND w.decision='waiting' AND w.kind<>'cc')) ORDER BY a.id DESC LIMIT 100`, [member.id]);
  }],
  ["GET", /^\/approvals\/(\d+)$/, async ({ member, params }) => {
    const id = int(params[0], "id");
    const a = await one("SELECT a.id,a.form_id,a.drafter_id,x.display_name drafter,a.title,a.body,a.status,a.submitted_at,a.closed_at FROM approvals a JOIN members x ON x.id=a.drafter_id WHERE a.id=? AND (a.drafter_id=? OR (a.status<>'draft' AND EXISTS(SELECT 1 FROM approval_steps s WHERE s.approval_id=a.id AND s.approver_id=?)))", [id, member.id, member.id]);
    if (!a) throw notFound();
    return { ...a, steps: await rows("SELECT s.id,s.seq,s.approver_id,x.display_name approver,s.kind,s.decision,s.comment,s.decided_at FROM approval_steps s JOIN members x ON x.id=s.approver_id WHERE s.approval_id=? ORDER BY s.seq,s.id", [id]) };
  }],
  ["POST", /^\/approvals$/, async ({ member, body }) => {
    const steps = (Array.isArray(body.steps) ? body.steps : []).slice(0, 20).map((s: any, i: number) => ({ approverId: int(s.approverId, "approverId"), kind: oneOf(s.kind ?? "approve", ["approve", "agree", "cc"], "kind"), seq: Number.isInteger(s.seq) ? s.seq : i + 1 }));
    if (!steps.some((s: any) => s.kind !== "cc")) throw bad("approver_required");
    if (steps.some((s: any) => s.approverId === member.id && s.kind !== "cc")) throw bad("self_approval");
    const submit = body.submit !== false;
    const conn = await pool.getConnection();
    try {
      await conn.beginTransaction();
      const [r]: any = await conn.query("INSERT INTO approvals (form_id,drafter_id,title,body,status,submitted_at) VALUES (?,?,?,?,?,IF(?,NOW(),NULL))",
        [int(body.formId, "formId"), member.id, str(body.title, 200, "title"), str(body.body, 200_000, "body"), submit ? "pending" : "draft", submit ? 1 : 0]);
      await conn.query("INSERT INTO approval_steps (approval_id,seq,approver_id,kind) VALUES ?", [steps.map((s: any) => [r.insertId, s.seq, s.approverId, s.kind])]);
      await conn.commit();
      if (submit) await notify(steps.filter((s: any) => s.kind !== "cc" && s.seq === Math.min(...steps.filter((x: any) => x.kind !== "cc").map((x: any) => x.seq))).map((s: any) => s.approverId), "approval", r.insertId, `결재 요청: ${String(body.title).slice(0, 80)}`);
      return { id: r.insertId };
    } catch (error) { await conn.rollback(); throw error; } finally { conn.release(); }
  }],
  ["POST", /^\/approvals\/(\d+)\/decide$/, async ({ member, params, body }) => {
    const id = int(params[0], "id"), decision = oneOf(body.decision, ["approved", "rejected"], "decision");
    const conn = await pool.getConnection();
    try {
      await conn.beginTransaction();
      const [[a]]: any = await conn.query("SELECT id,drafter_id,title,status FROM approvals WHERE id=? FOR UPDATE", [id]);
      if (!a || a.status !== "pending") throw new HttpError(409, "not_pending");
      const [waiting]: any = await conn.query("SELECT id,seq,approver_id,kind FROM approval_steps WHERE approval_id=? AND decision='waiting' AND kind<>'cc' ORDER BY seq,id", [id]);
      const turn = waiting.filter((s: any) => s.seq === waiting[0]?.seq);
      const mine = turn.find((s: any) => s.approver_id === member.id);
      if (!mine) throw forbidden(); // not my turn
      await conn.query("UPDATE approval_steps SET decision=?, comment=?, decided_at=NOW() WHERE id=?", [decision, str(body.comment, 2000, "comment", decision === "approved"), mine.id]);
      const remaining = waiting.filter((s: any) => s.id !== mine.id);
      let status = "pending", next: number[] = [];
      if (decision === "rejected") status = "rejected";
      else if (!remaining.length) status = "approved";
      else if (!remaining.some((s: any) => s.seq === mine.seq)) next = remaining.filter((s: any) => s.seq === remaining[0].seq).map((s: any) => s.approver_id);
      if (status !== "pending") await conn.query("UPDATE approvals SET status=?, closed_at=NOW() WHERE id=?", [status, id]);
      await conn.commit();
      if (status === "pending") await notify(next, "approval", id, `결재 요청: ${a.title.slice(0, 80)}`);
      else await notify([a.drafter_id], "approval", id, `결재가 ${status === "approved" ? "승인" : "반려"}되었습니다: ${a.title.slice(0, 60)}`);
      return { status };
    } catch (error) { await conn.rollback(); throw error; } finally { conn.release(); }
  }],
  ["POST", /^\/approvals\/(\d+)\/withdraw$/, async ({ member, params }) => {
    const r = await run("UPDATE approvals SET status='withdrawn', closed_at=NOW() WHERE id=? AND drafter_id=? AND status IN ('draft','pending')", [int(params[0], "id"), member.id]);
    if (!r.affectedRows) throw new HttpError(409, "not_withdrawable");
    return { ok: true };
  }],

  // ---- 게시판/공지 ----
  ["GET", /^\/boards$/, async () => rows("SELECT id,name,notice_only FROM boards ORDER BY id")],
  ["POST", /^\/boards$/, admin(async ({ body }) => ({ id: (await run("INSERT INTO boards (name,notice_only) VALUES (?,?)", [str(body.name, 80, "name"), body.noticeOnly ? 1 : 0])).insertId }))],
  ["GET", /^\/boards\/(\d+)\/posts$/, async ({ member, params }) => rows("SELECT p.id,p.title,p.pinned,p.must_read,p.created_at,x.display_name author,(SELECT COUNT(*) FROM board_comments c WHERE c.post_id=p.id) comments,(SELECT COUNT(*) FROM board_reads r WHERE r.post_id=p.id AND r.member_id=?) read_by_me FROM board_posts p JOIN members x ON x.id=p.author_id WHERE p.board_id=? ORDER BY p.pinned DESC,p.id DESC LIMIT 100", [member.id, int(params[0], "id")])],
  ["POST", /^\/boards\/(\d+)\/posts$/, async ({ member, params, body }) => {
    const id = int(params[0], "id");
    const board = await one<{ notice_only: number }>("SELECT notice_only FROM boards WHERE id=?", [id]);
    if (!board) throw notFound();
    if (board.notice_only && member.role !== "admin") throw forbidden();
    const must = board.notice_only && body.mustRead ? 1 : 0;
    const r = await run("INSERT INTO board_posts (board_id,author_id,title,body,pinned,must_read) VALUES (?,?,?,?,?,?)", [id, member.id, str(body.title, 200, "title"), str(body.body, 200_000, "body"), board.notice_only && body.pinned ? 1 : 0, must]);
    if (board.notice_only) await notify((await rows<{ id: number }>("SELECT id FROM members WHERE status='active'")).map((m) => m.id), "notice", r.insertId, `공지: ${String(body.title).slice(0, 80)}`, member.id);
    return { id: r.insertId };
  }],
  ["GET", /^\/posts\/(\d+)$/, async ({ member, params }) => {
    const id = int(params[0], "id");
    const post = await one("SELECT p.id,p.board_id,p.title,p.body,p.pinned,p.must_read,p.created_at,p.author_id,x.display_name author FROM board_posts p JOIN members x ON x.id=p.author_id WHERE p.id=?", [id]);
    if (!post) throw notFound();
    await run("INSERT IGNORE INTO board_reads (post_id,member_id) VALUES (?,?)", [id, member.id]);
    return { ...post, comments: await rows("SELECT c.id,c.author_id,x.display_name author,c.body,c.created_at FROM board_comments c JOIN members x ON x.id=c.author_id WHERE c.post_id=? ORDER BY c.id", [id]) };
  }],
  ["POST", /^\/posts\/(\d+)\/comments$/, async ({ member, params, body }) => ({ id: (await run("INSERT INTO board_comments (post_id,author_id,body) VALUES (?,?,?)", [int(params[0], "id"), member.id, str(body.body, 4000, "body")])).insertId })],
  ["GET", /^\/posts\/(\d+)\/readers$/, admin(async ({ params }) => rows("SELECT x.id,x.display_name,r.read_at FROM members x LEFT JOIN board_reads r ON r.member_id=x.id AND r.post_id=? WHERE x.status='active' ORDER BY r.read_at IS NULL,x.display_name", [int(params[0], "id")]))],
  ["DELETE", /^\/posts\/(\d+)$/, async ({ member, params }) => { await run("DELETE FROM board_posts WHERE id=? AND (author_id=? OR ?)", [int(params[0], "id"), member.id, member.role === "admin" ? 1 : 0]); return { ok: true }; }],

  // ---- 프로젝트/업무 ----
  ["GET", /^\/projects$/, async ({ member }) => rows("SELECT p.id,p.name,p.status,p.owner_id,(SELECT COUNT(*) FROM tasks t WHERE t.project_id=p.id AND t.status<>'done') open_tasks FROM projects p WHERE p.owner_id=? OR EXISTS(SELECT 1 FROM project_members m WHERE m.project_id=p.id AND m.member_id=?) ORDER BY p.status,p.id DESC", [member.id, member.id])],
  ["POST", /^\/projects$/, async ({ member, body }) => {
    const r = await run("INSERT INTO projects (name,owner_id) VALUES (?,?)", [str(body.name, 120, "name"), member.id]);
    const people = [...new Set([member.id, ...ids(body.memberIds ?? [], "memberIds")])];
    await run("INSERT INTO project_members (project_id,member_id) VALUES ?", [people.map((i) => [r.insertId, i])]);
    return { id: r.insertId };
  }],
  ["GET", /^\/projects\/(\d+)$/, async ({ member, params }) => {
    const id = int(params[0], "id");
    const p = await one("SELECT id,name,status,owner_id FROM projects p WHERE id=? AND (owner_id=? OR EXISTS(SELECT 1 FROM project_members m WHERE m.project_id=p.id AND m.member_id=?))", [id, member.id, member.id]);
    if (!p) throw notFound();
    return { ...p, members: await rows("SELECT x.id,x.display_name FROM project_members m JOIN members x ON x.id=m.member_id WHERE m.project_id=?", [id]),
      tasks: await rows("SELECT t.id,t.title,t.description,t.assignee_id,t.status,t.due_date,t.label FROM tasks t WHERE t.project_id=? ORDER BY t.sort_order,t.id", [id]) };
  }],
  ["POST", /^\/projects\/(\d+)\/tasks$/, async ({ member, params, body }) => {
    const id = int(params[0], "id");
    if (!(await one("SELECT 1 FROM project_members WHERE project_id=? AND member_id=?", [id, member.id]))) throw forbidden();
    const assignee = body.assigneeId ? int(body.assigneeId, "assigneeId") : null;
    if (assignee && !(await one("SELECT 1 FROM project_members WHERE project_id=? AND member_id=?", [id, assignee]))) throw bad("assignee_not_member");
    const r = await run("INSERT INTO tasks (project_id,title,description,assignee_id,due_date,label) VALUES (?,?,?,?,?,?)", [id, str(body.title, 200, "title"), str(body.description, 8000, "description", true), assignee, date(body.dueDate, "dueDate", true), str(body.label, 40, "label", true)]);
    if (assignee) await notify([assignee], "task", r.insertId, `새 업무: ${String(body.title).slice(0, 80)}`, member.id);
    return { id: r.insertId };
  }],
  ["PATCH", /^\/tasks\/(\d+)$/, async ({ member, params, body }) => {
    const id = int(params[0], "id");
    const t = await one<{ project_id: number }>("SELECT t.project_id FROM tasks t JOIN project_members m ON m.project_id=t.project_id AND m.member_id=? WHERE t.id=?", [member.id, id]);
    if (!t) throw forbidden();
    const assignee = body.assigneeId ? int(body.assigneeId, "assigneeId") : null;
    if (assignee && !(await one("SELECT 1 FROM project_members WHERE project_id=? AND member_id=?", [t.project_id, assignee]))) throw bad("assignee_not_member");
    await run("UPDATE tasks SET status=?, assignee_id=?, title=COALESCE(?,title), due_date=? WHERE id=?", [oneOf(body.status, ["todo", "doing", "done"], "status"), assignee, body.title ? str(body.title, 200, "title") : null, date(body.dueDate, "dueDate", true), id]);
    return { ok: true };
  }],
  ["POST", /^\/tasks\/(\d+)\/comments$/, async ({ member, params, body }) => {
    const id = int(params[0], "id");
    if (!(await one("SELECT 1 FROM tasks t JOIN project_members m ON m.project_id=t.project_id AND m.member_id=? WHERE t.id=?", [member.id, id]))) throw forbidden();
    return { id: (await run("INSERT INTO task_comments (task_id,author_id,body) VALUES (?,?,?)", [id, member.id, str(body.body, 4000, "body")])).insertId };
  }],

  // ---- 근태/HR ----
  ["GET", /^\/attendance$/, async ({ member, query }) => {
    const month = /^\d{4}-\d{2}$/.test(query.get("month") ?? "") ? query.get("month") : new Date().toISOString().slice(0, 7);
    return rows("SELECT work_date,clock_in,clock_out FROM attendance WHERE member_id=? AND work_date LIKE ? ORDER BY work_date", [member.id, `${month}-%`]);
  }],
  ["POST", /^\/attendance\/clock-in$/, async ({ member }) => { await run("INSERT INTO attendance (member_id,work_date,clock_in) VALUES (?,CURDATE(),NOW()) ON DUPLICATE KEY UPDATE clock_in=COALESCE(clock_in,NOW())", [member.id]); return { ok: true }; }],
  ["POST", /^\/attendance\/clock-out$/, async ({ member }) => {
    const r = await run("UPDATE attendance SET clock_out=NOW() WHERE member_id=? AND work_date=CURDATE() AND clock_in IS NOT NULL", [member.id]);
    if (!r.affectedRows) throw bad("not_clocked_in");
    return { ok: true };
  }],
  ["GET", /^\/leave$/, async ({ member, query }) => query.get("all") === "1" && member.role === "admin"
    ? rows("SELECT l.id,x.display_name member,l.kind,l.starts_on,l.ends_on,l.reason,l.status FROM leave_requests l JOIN members x ON x.id=l.member_id ORDER BY l.id DESC LIMIT 200")
    : rows("SELECT id,kind,starts_on,ends_on,reason,status FROM leave_requests WHERE member_id=? ORDER BY id DESC LIMIT 100", [member.id])],
  ["POST", /^\/leave$/, async ({ member, body }) => {
    const starts = date(body.startsOn, "startsOn")!, ends = date(body.endsOn, "endsOn")!;
    if (ends < starts) throw bad("range_invalid");
    const r = await run("INSERT INTO leave_requests (member_id,kind,starts_on,ends_on,reason) VALUES (?,?,?,?,?)", [member.id, oneOf(body.kind, ["annual", "half", "sick", "other"], "kind"), starts, ends, str(body.reason, 300, "reason", true)]);
    await notify((await rows<{ id: number }>("SELECT id FROM members WHERE role='admin' AND status='active'")).map((m) => m.id), "leave", r.insertId, `${member.name}님의 휴가 신청`, member.id);
    return { id: r.insertId };
  }],
  ["POST", /^\/leave\/(\d+)\/decide$/, admin(async ({ member, params, body }) => {
    const id = int(params[0], "id");
    const leave = await one<{ member_id: number }>("SELECT member_id FROM leave_requests WHERE id=? AND status='pending'", [id]);
    if (!leave) throw new HttpError(409, "not_pending");
    if (leave.member_id === member.id) throw forbidden(); // nobody approves their own leave
    const status = oneOf(body.decision, ["approved", "rejected"], "decision");
    await run("UPDATE leave_requests SET status=? WHERE id=?", [status, id]);
    await audit(member.id, `leave.${status}`, String(id));
    await notify([leave.member_id], "leave", id, `휴가가 ${status === "approved" ? "승인" : "반려"}되었습니다`);
    return { ok: true };
  })],
  ["POST", /^\/leave\/(\d+)\/cancel$/, async ({ member, params }) => { await run("UPDATE leave_requests SET status='cancelled' WHERE id=? AND member_id=? AND status='pending'", [int(params[0], "id"), member.id]); return { ok: true }; }],
];
