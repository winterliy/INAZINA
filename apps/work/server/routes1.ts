// Accounts/organisation, home, contacts, mail, messenger, calendar. Every query is parameterised; every read is limited to what the member may see.
import { one, pool, rows, run } from "./db.ts";
import { bad, dateTime, forbidden, ids, int, notFound, oneOf, str, type Handler } from "./http.ts";

export type Route = [method: string, pattern: RegExp, handler: Handler];

export async function notify(memberIds: number[], kind: string, refId: number | null, text: string, except?: number): Promise<void> {
  const targets = memberIds.filter((id) => id !== except);
  if (!targets.length) return;
  await run("INSERT INTO notifications (member_id, kind, ref_id, text) VALUES ?", [targets.map((id) => [id, kind, refId, text.slice(0, 300)])]);
}
export const audit = (memberId: number, action: string, target?: string) => run("INSERT INTO audit_log (member_id, action, target) VALUES (?,?,?)", [memberId, action, target ?? null]);
export const admin: (h: Handler) => Handler = (h) => async (ctx) => { if (ctx.member.role !== "admin") throw forbidden(); return h(ctx); };
const activeIds = async (list: number[]) => (list.length ? (await rows<{ id: number }>("SELECT id FROM members WHERE status<>'retired' AND id IN (?)", [list])).map((r) => r.id) : []);

export const routes1: Route[] = [
  // ---- 계정/조직 ----
  ["GET", /^\/me$/, async ({ member }) => ({ member, profile: await one("SELECT id,display_name,job_title,phone,role,status FROM members WHERE id=?", [member.id]) })],
  ["PATCH", /^\/me$/, async ({ member, body }) => {
    await run("UPDATE members SET display_name=?, job_title=?, phone=? WHERE id=?", [str(body.displayName, 80, "displayName"), str(body.jobTitle, 80, "jobTitle", true), str(body.phone, 40, "phone", true), member.id]);
    return { ok: true };
  }],
  ["GET", /^\/members$/, async ({ member }) => rows(`SELECT id,display_name,job_title,phone,role,status${member.role === "admin" ? ",login_id" : ""} FROM members WHERE status<>'retired' ORDER BY display_name`)],
  ["PATCH", /^\/members\/(\d+)$/, admin(async ({ member, params, body }) => {
    const id = int(params[0], "id");
    if (id === member.id) throw forbidden(); // nobody changes their own role (the last administrator could lock everyone out)
    await run("UPDATE members SET role=?, status=? WHERE id=? AND status<>'retired'", [oneOf(body.role, ["admin", "member", "guest"], "role"), oneOf(body.status, ["active", "leave", "retired"], "status"), id]);
    await audit(member.id, "member.update", String(id));
    return { ok: true };
  })],
  ["GET", /^\/departments$/, async () => ({
    departments: await rows("SELECT id,parent_id,name,sort_order FROM departments ORDER BY sort_order,id"),
    members: await rows("SELECT dm.department_id,dm.member_id,dm.is_head FROM department_members dm JOIN members m ON m.id=dm.member_id WHERE m.status<>'retired'"),
  })],
  ["POST", /^\/departments$/, admin(async ({ member, body }) => {
    const parent = body.parentId ? int(body.parentId, "parentId") : null;
    const result = await run("INSERT INTO departments (parent_id,name,sort_order) VALUES (?,?,?)", [parent, str(body.name, 80, "name"), Number(body.sortOrder) | 0]);
    await audit(member.id, "department.create", String(result.insertId));
    return { id: result.insertId };
  })],
  ["POST", /^\/departments\/(\d+)\/members$/, admin(async ({ params, body }) => {
    await run("INSERT INTO department_members (department_id,member_id,is_head) VALUES (?,?,?) ON DUPLICATE KEY UPDATE is_head=VALUES(is_head)", [int(params[0], "id"), int(body.memberId, "memberId"), body.isHead ? 1 : 0]);
    return { ok: true };
  })],
  ["DELETE", /^\/departments\/(\d+)\/members\/(\d+)$/, admin(async ({ params }) => { await run("DELETE FROM department_members WHERE department_id=? AND member_id=?", [int(params[0], "id"), int(params[1], "memberId")]); return { ok: true }; })],

  // ---- 홈 ----
  ["GET", /^\/home$/, async ({ member }) => ({
    unreadMail: (await one<{ n: number }>("SELECT COUNT(*) n FROM mail_recipients WHERE member_id=? AND folder='inbox' AND read_at IS NULL", [member.id]))!.n,
    pendingApprovals: (await one<{ n: number }>("SELECT COUNT(*) n FROM approval_steps s JOIN approvals a ON a.id=s.approval_id WHERE s.approver_id=? AND s.decision='waiting' AND a.status='pending' AND s.kind<>'cc' AND s.seq=(SELECT MIN(seq) FROM approval_steps w WHERE w.approval_id=a.id AND w.decision='waiting' AND w.kind<>'cc')", [member.id]))!.n,
    myTasks: await rows("SELECT t.id,t.title,t.status,t.due_date,p.name project FROM tasks t JOIN projects p ON p.id=t.project_id WHERE t.assignee_id=? AND t.status<>'done' ORDER BY t.due_date IS NULL, t.due_date LIMIT 8", [member.id]),
    events: await rows("SELECT e.id,e.title,e.starts_at,e.ends_at FROM calendar_events e JOIN calendars c ON c.id=e.calendar_id WHERE e.ends_at>=NOW() AND e.starts_at<DATE_ADD(NOW(), INTERVAL 7 DAY) AND (c.owner_id=? OR c.kind<>'personal' OR EXISTS(SELECT 1 FROM calendar_attendees a WHERE a.event_id=e.id AND a.member_id=?)) ORDER BY e.starts_at LIMIT 8", [member.id, member.id]),
    notices: await rows("SELECT p.id,p.title,p.created_at,p.must_read,(SELECT COUNT(*) FROM board_reads r WHERE r.post_id=p.id AND r.member_id=?) read_by_me FROM board_posts p JOIN boards b ON b.id=p.board_id WHERE b.notice_only=1 ORDER BY p.pinned DESC,p.id DESC LIMIT 5", [member.id]),
    notifications: await rows("SELECT id,kind,ref_id,text,created_at,read_at FROM notifications WHERE member_id=? ORDER BY id DESC LIMIT 20", [member.id]),
  })],
  ["POST", /^\/notifications\/read$/, async ({ member }) => { await run("UPDATE notifications SET read_at=NOW() WHERE member_id=? AND read_at IS NULL", [member.id]); return { ok: true }; }],
  ["GET", /^\/search$/, async ({ member, query }) => {
    const q = `%${(query.get("q") ?? "").slice(0, 60).replace(/[%_\\]/g, "\\$&")}%`;
    if (q.length < 4) throw bad("query_too_short");
    return {
      members: await rows("SELECT id,display_name,job_title FROM members WHERE status<>'retired' AND display_name LIKE ? LIMIT 10", [q]),
      docs: await rows("SELECT d.id,d.title FROM docs d WHERE d.title LIKE ? AND (d.owner_id=? OR EXISTS(SELECT 1 FROM doc_members m WHERE m.doc_id=d.id AND m.member_id=?)) LIMIT 10", [q, member.id, member.id]),
      posts: await rows("SELECT id,title FROM board_posts WHERE title LIKE ? ORDER BY id DESC LIMIT 10", [q]),
      mails: await rows("SELECT m.id,m.subject FROM mail_messages m LEFT JOIN mail_recipients r ON r.message_id=m.id AND r.member_id=? WHERE m.subject LIKE ? AND (r.member_id IS NOT NULL OR m.sender_id=?) LIMIT 10", [member.id, q, member.id]),
    };
  }],

  // ---- 연락처 ----
  ["GET", /^\/contacts$/, async ({ member }) => rows("SELECT id,owner_id,name,organization,email,phone,memo,shared FROM contacts WHERE owner_id=? OR shared=1 ORDER BY name", [member.id])],
  ["POST", /^\/contacts$/, async ({ member, body }) => {
    const r = await run("INSERT INTO contacts (owner_id,name,organization,email,phone,memo,shared) VALUES (?,?,?,?,?,?,?)",
      [member.id, str(body.name, 80, "name"), str(body.organization, 120, "organization", true), str(body.email, 190, "email", true), str(body.phone, 40, "phone", true), str(body.memo, 2000, "memo", true), body.shared ? 1 : 0]);
    return { id: r.insertId };
  }],
  ["DELETE", /^\/contacts\/(\d+)$/, async ({ member, params }) => { await run("DELETE FROM contacts WHERE id=? AND owner_id=?", [int(params[0], "id"), member.id]); return { ok: true }; }],

  // ---- 내부 메일 ----
  ["GET", /^\/mail$/, async ({ member, query }) => {
    const box = oneOf(query.get("box") ?? "inbox", ["inbox", "sent", "trash"], "box");
    if (box === "sent") return rows("SELECT m.id,m.subject,m.sent_at,(SELECT GROUP_CONCAT(x.display_name SEPARATOR ', ') FROM mail_recipients r JOIN members x ON x.id=r.member_id WHERE r.message_id=m.id) recipients FROM mail_messages m WHERE m.sender_id=? ORDER BY m.id DESC LIMIT 100", [member.id]);
    return rows("SELECT m.id,m.subject,m.sent_at,s.display_name sender,r.read_at FROM mail_recipients r JOIN mail_messages m ON m.id=r.message_id JOIN members s ON s.id=m.sender_id WHERE r.member_id=? AND r.folder=? ORDER BY m.id DESC LIMIT 100", [member.id, box]);
  }],
  ["GET", /^\/mail\/(\d+)$/, async ({ member, params }) => {
    const id = int(params[0], "id");
    const mail = await one("SELECT m.id,m.subject,m.body,m.sent_at,m.sender_id,s.display_name sender FROM mail_messages m JOIN members s ON s.id=m.sender_id WHERE m.id=? AND (m.sender_id=? OR EXISTS(SELECT 1 FROM mail_recipients r WHERE r.message_id=m.id AND r.member_id=?))", [id, member.id, member.id]);
    if (!mail) throw notFound();
    await run("UPDATE mail_recipients SET read_at=COALESCE(read_at,NOW()) WHERE message_id=? AND member_id=?", [id, member.id]);
    return { ...mail, recipients: await rows("SELECT x.id,x.display_name,r.kind FROM mail_recipients r JOIN members x ON x.id=r.member_id WHERE r.message_id=?", [id]) };
  }],
  ["POST", /^\/mail$/, async ({ member, body }) => {
    const to = await activeIds(ids(body.to ?? [], "to", 50));
    const cc = (await activeIds(ids(body.cc ?? [], "cc", 50))).filter((i) => !to.includes(i));
    if (!to.length) throw bad("to_required");
    const subject = str(body.subject, 200, "subject")!;
    const conn = await pool.getConnection();
    try {
      await conn.beginTransaction();
      const [result]: any = await conn.query("INSERT INTO mail_messages (sender_id,subject,body) VALUES (?,?,?)", [member.id, subject, str(body.body, 200_000, "body")]);
      await conn.query("INSERT INTO mail_recipients (message_id,member_id,kind) VALUES ?", [[...to.map((i) => [result.insertId, i, "to"]), ...cc.map((i) => [result.insertId, i, "cc"])]]);
      await conn.commit();
      await notify([...to, ...cc], "mail", result.insertId, `새 메일: ${subject}`, member.id);
      return { id: result.insertId };
    } catch (error) { await conn.rollback(); throw error; } finally { conn.release(); }
  }],
  ["DELETE", /^\/mail\/(\d+)$/, async ({ member, params }) => { await run("UPDATE mail_recipients SET folder='trash' WHERE message_id=? AND member_id=?", [int(params[0], "id"), member.id]); return { ok: true }; }],

  // ---- 메신저 ----
  ["GET", /^\/chat\/channels$/, async ({ member }) => rows(
    `SELECT c.id,c.kind,
       COALESCE(c.name,(SELECT GROUP_CONCAT(x.display_name SEPARATOR ', ') FROM chat_members cm2 JOIN members x ON x.id=cm2.member_id WHERE cm2.channel_id=c.id AND cm2.member_id<>?)) name,
       (SELECT COUNT(*) FROM chat_messages m WHERE m.channel_id=c.id AND m.id>cm.last_read_message_id AND m.sender_id<>? AND m.deleted_at IS NULL) unread,
       (SELECT MAX(id) FROM chat_messages m WHERE m.channel_id=c.id) last_id
     FROM chat_channels c JOIN chat_members cm ON cm.channel_id=c.id AND cm.member_id=? ORDER BY last_id IS NULL, last_id DESC`, [member.id, member.id, member.id])],
  ["POST", /^\/chat\/channels$/, async ({ member, body }) => {
    const kind = oneOf(body.kind, ["direct", "group"], "kind");
    const others = (await activeIds(ids(body.memberIds, "memberIds", 100))).filter((i) => i !== member.id);
    if (!others.length || (kind === "direct" && others.length !== 1)) throw bad("members_invalid");
    if (kind === "direct") {
      const exist = await one<{ id: number }>("SELECT c.id FROM chat_channels c JOIN chat_members a ON a.channel_id=c.id AND a.member_id=? JOIN chat_members b ON b.channel_id=c.id AND b.member_id=? WHERE c.kind='direct' LIMIT 1", [member.id, others[0]]);
      if (exist) return { id: exist.id };
    }
    const r = await run("INSERT INTO chat_channels (kind,name,created_by) VALUES (?,?,?)", [kind, kind === "group" ? str(body.name, 80, "name") : null, member.id]);
    await run("INSERT INTO chat_members (channel_id,member_id) VALUES ?", [[member.id, ...others].map((i) => [r.insertId, i])]);
    return { id: r.insertId };
  }],
  ["GET", /^\/chat\/channels\/(\d+)\/messages$/, async ({ member, params, query }) => {
    const id = int(params[0], "id");
    if (!(await one("SELECT 1 FROM chat_members WHERE channel_id=? AND member_id=?", [id, member.id]))) throw forbidden();
    const after = Number(query.get("after") ?? 0) | 0;
    const list = await rows("SELECT m.id,m.sender_id,x.display_name sender,m.parent_id,IF(m.deleted_at IS NULL,m.body,NULL) body,m.created_at FROM chat_messages m JOIN members x ON x.id=m.sender_id WHERE m.channel_id=? AND m.id>? ORDER BY m.id DESC LIMIT 200", [id, after]);
    const top = list[0]?.id;
    if (top) await run("UPDATE chat_members SET last_read_message_id=GREATEST(last_read_message_id,?) WHERE channel_id=? AND member_id=?", [top, id, member.id]);
    return list.reverse();
  }],
  ["POST", /^\/chat\/channels\/(\d+)\/messages$/, async ({ member, params, body }) => {
    const id = int(params[0], "id");
    if (!(await one("SELECT 1 FROM chat_members WHERE channel_id=? AND member_id=?", [id, member.id]))) throw forbidden();
    const text = str(body.body, 4000, "body")!;
    const r = await run("INSERT INTO chat_messages (channel_id,sender_id,parent_id,body) VALUES (?,?,?,?)", [id, member.id, body.parentId ? int(body.parentId, "parentId") : null, text]);
    const mates = (await rows<{ member_id: number }>("SELECT member_id FROM chat_members WHERE channel_id=?", [id])).map((m) => m.member_id);
    await notify(mates, "chat", id, `${member.name}: ${text.slice(0, 60)}`, member.id);
    return { id: r.insertId };
  }],
  ["DELETE", /^\/chat\/messages\/(\d+)$/, async ({ member, params }) => { await run("UPDATE chat_messages SET deleted_at=NOW() WHERE id=? AND sender_id=?", [int(params[0], "id"), member.id]); return { ok: true }; }],

  // ---- 캘린더 ----
  ["GET", /^\/calendar\/calendars$/, async ({ member }) => rows("SELECT id,owner_id,name,kind FROM calendars WHERE owner_id=? OR kind<>'personal' ORDER BY kind,name", [member.id])],
  ["POST", /^\/calendar\/calendars$/, async ({ member, body }) => {
    const kind = oneOf(body.kind, ["personal", "shared", "resource"], "kind");
    if (kind !== "personal" && member.role !== "admin") throw forbidden();
    const r = await run("INSERT INTO calendars (owner_id,name,kind) VALUES (?,?,?)", [kind === "personal" ? member.id : null, str(body.name, 80, "name"), kind]);
    return { id: r.insertId };
  }],
  ["GET", /^\/calendar\/events$/, async ({ member, query }) => {
    const from = dateTime(`${query.get("from") ?? ""}`.length === 10 ? `${query.get("from")} 00:00` : query.get("from"), "from");
    const to = dateTime(`${query.get("to") ?? ""}`.length === 10 ? `${query.get("to")} 23:59` : query.get("to"), "to");
    return rows(`SELECT e.id,e.calendar_id,c.name calendar,e.title,e.starts_at,e.ends_at,e.all_day,e.location,e.memo,e.created_by,
      (SELECT response FROM calendar_attendees a WHERE a.event_id=e.id AND a.member_id=?) my_response,
      (SELECT GROUP_CONCAT(x.display_name SEPARATOR ', ') FROM calendar_attendees a JOIN members x ON x.id=a.member_id WHERE a.event_id=e.id) attendees
      FROM calendar_events e JOIN calendars c ON c.id=e.calendar_id
      WHERE e.starts_at<=? AND e.ends_at>=? AND (c.owner_id=? OR c.kind<>'personal' OR EXISTS(SELECT 1 FROM calendar_attendees a WHERE a.event_id=e.id AND a.member_id=?)) ORDER BY e.starts_at LIMIT 500`, [member.id, to, from, member.id, member.id]);
  }],
  ["POST", /^\/calendar\/events$/, async ({ member, body }) => {
    const calendarId = int(body.calendarId, "calendarId");
    const cal = await one<{ kind: string; owner_id: number | null }>("SELECT kind,owner_id FROM calendars WHERE id=?", [calendarId]);
    if (!cal || (cal.kind === "personal" && cal.owner_id !== member.id)) throw forbidden();
    const starts = dateTime(body.startsAt, "startsAt"), ends = dateTime(body.endsAt, "endsAt");
    if (ends < starts) throw bad("range_invalid");
    if (cal.kind === "resource" && (await one("SELECT 1 FROM calendar_events WHERE calendar_id=? AND starts_at<? AND ends_at>?", [calendarId, ends, starts]))) throw new (await import("./http.ts")).HttpError(409, "resource_busy");
    const r = await run("INSERT INTO calendar_events (calendar_id,title,starts_at,ends_at,all_day,location,memo,created_by) VALUES (?,?,?,?,?,?,?,?)",
      [calendarId, str(body.title, 200, "title"), starts, ends, body.allDay ? 1 : 0, str(body.location, 160, "location", true), str(body.memo, 4000, "memo", true), member.id]);
    const guests = (await activeIds(ids(body.attendees ?? [], "attendees", 100))).filter((i) => i !== member.id);
    if (guests.length) {
      await run("INSERT INTO calendar_attendees (event_id,member_id) VALUES ?", [guests.map((i) => [r.insertId, i])]);
      await notify(guests, "event", r.insertId, `일정 초대: ${String(body.title).slice(0, 80)}`);
    }
    return { id: r.insertId };
  }],
  ["POST", /^\/calendar\/events\/(\d+)\/respond$/, async ({ member, params, body }) => {
    await run("UPDATE calendar_attendees SET response=? WHERE event_id=? AND member_id=?", [oneOf(body.response, ["yes", "no", "maybe"], "response"), int(params[0], "id"), member.id]);
    return { ok: true };
  }],
  ["DELETE", /^\/calendar\/events\/(\d+)$/, async ({ member, params }) => { await run("DELETE FROM calendar_events WHERE id=? AND (created_by=? OR ?)", [int(params[0], "id"), member.id, member.role === "admin" ? 1 : 0]); return { ok: true }; }],
];
