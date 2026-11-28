// Run on the Mac, not over the web:  npm run admin --workspace @inazina/work -- create-admin <id> [name]
//                                    npm run admin --workspace @inazina/work -- reset-password <id>
import { spawnSync } from "node:child_process";
import { createInterface } from "node:readline";
import { checkLoginId, checkPassword, hashPassword } from "./auth.ts";
import { migrate, one, pool, run } from "./db.ts";

// One line from the keyboard, not echoed (on a terminal); piped input (tests, scripts) is read as it is.
const lines: string[] = [];
let waiting: ((line: string) => void) | undefined;
const rl = createInterface({ input: process.stdin, terminal: false });
rl.on("line", (line) => (waiting ? (([w]) => { waiting = undefined; w(line); })([waiting]) : lines.push(line)));
function ask(question: string): Promise<string> {
  process.stdout.write(question);
  const tty = process.stdin.isTTY;
  const echo = (on: boolean) => { if (tty) spawnSync("stty", [on ? "echo" : "-echo"], { stdio: ["inherit", "ignore", "ignore"] }); }; // the terminal's own echo
  return new Promise((done) => {
    const next = lines.shift();
    if (next !== undefined) return done(next);
    echo(false);
    waiting = (line) => { echo(true); if (tty) process.stdout.write("\n"); done(line); };
  });
}

const [command, rawId, ...rest] = process.argv.slice(2);
if (!process.env.WORK_DB_PASSWORD) { console.error("WORK_DB_PASSWORD is not set (run from the project root with .env loaded, e.g. via start_all's environment)"); process.exit(1); }
try {
  await migrate();
  const id = checkLoginId(rawId);
  if (command === "create-admin" || command === "reset-password") {
    const password = checkPassword(await ask("새 비밀번호(10자 이상): "), id);
    if (password !== (await ask("한 번 더: "))) throw new Error("비밀번호가 서로 다릅니다.");
    const hash = await hashPassword(password);
    if (command === "create-admin") {
      if (await one("SELECT 1 FROM members WHERE login_id=?", [id])) throw new Error("이미 있는 아이디입니다.");
      await run("INSERT INTO members (user_ref,display_name,login_id,password_hash,role) VALUES (CONCAT('m-',SUBSTRING(MD5(RAND()),1,16)),?,?,?,'admin')", [(rest.join(" ") || id).slice(0, 80), id, hash]);
      console.log(`관리자 ${id} 를 만들었습니다.`);
    } else {
      const r = await run("UPDATE members SET password_hash=?, must_change_password=0, failed_logins=0, locked_until=NULL WHERE login_id=?", [hash, id]);
      if (!r.affectedRows) throw new Error("없는 아이디입니다.");
      await run("DELETE FROM sessions WHERE member_id=(SELECT id FROM (SELECT id FROM members WHERE login_id=?) x)", [id]);
      console.log(`${id} 의 비밀번호를 바꿨습니다.`);
    }
  } else console.error("usage: create-admin <id> [name] | reset-password <id>");
} catch (error) { console.error(error instanceof Error ? error.message : error); process.exitCode = 1; } finally { rl.close(); await pool.end(); }
