// Passwords of the Wide-Learning viewer (port 4315), kept on the server in database/wl-security.json
// (ignored by git): the entry password, and the code that opens the hidden login-attempt log. Both
// are stored salted (PBKDF2-SHA256). There are no built-in defaults: on first use a random entry password and a random log code are
// generated, stored (hashed) in the store file and printed ONCE to the server console. An already-stored file keeps working.
import { createHash, pbkdf2Sync, randomBytes, randomInt, timingSafeEqual } from "node:crypto";
import { chmodSync, existsSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const ITERATIONS = 210_000;
const ITER_MIN = 10_000, ITER_MAX = 1_000_000;
export const ENTRY_MIN = 8, ENTRY_MAX = 64, CODE_MIN = 4, CODE_MAX = 8;

type Stored = { hash: string; updatedAt?: string; generated?: boolean };
type Store = { entry?: Stored; logCode?: Stored & { length: number } };

const same = (left: string, right: string): boolean => {
  const a = Buffer.from(left, "hex"), b = Buffer.from(right, "hex");
  return a.length === b.length && a.length > 0 && timingSafeEqual(a, b);
};
const sha256 = (text: string) => createHash("sha256").update(text).digest("hex");

export function hashSecret(secret: string): string {
  const salt = randomBytes(16).toString("hex");
  return `pbkdf2$${ITERATIONS}$${salt}$${pbkdf2Sync(secret, Buffer.from(salt, "hex"), ITERATIONS, 32, "sha256").toString("hex")}`;
}

export function matches(stored: string, secret: string): boolean {
  if (stored.startsWith("pbkdf2$")) {
    const [, iterations, salt, digest] = stored.split("$");
    if (!iterations || !salt || !digest || !/^\d{1,9}$/.test(iterations)) return false;
    // The store file is trusted only so far: a tampered iteration count must not stall the server.
    const rounds = Number(iterations);
    if (rounds < ITER_MIN || rounds > ITER_MAX) return false;
    return same(pbkdf2Sync(secret, Buffer.from(salt, "hex"), rounds, 32, "sha256").toString("hex"), digest);
  }
  return same(sha256(secret), stored);
}

export function wlSecurity(root: string, path = join(root, "database", "wl-security.json")) {
  const read = (): Store => {
    try { return existsSync(path) ? JSON.parse(readFileSync(path, "utf8")) as Store : {}; } catch { return {}; }
  };
  const write = (data: Store) => {
    const temp = `${path}.${process.pid}.tmp`;
    writeFileSync(temp, JSON.stringify(data, null, 2), { mode: 0o600 });
    chmodSync(temp, 0o600);
    renameSync(temp, path);
  };
  const entryHash = (): string => {
    const data = read();
    if (data.entry) return data.entry.hash;
    const password = randomBytes(12).toString("base64url");
    data.entry = { hash: hashSecret(password), generated: true };
    try { write(data); } catch { return data.entry.hash; }
    console.warn(`[wl] 입장 비밀번호가 새로 만들어졌습니다 (한 번만 표시, 설정 탭에서 바꿀 수 있음): ${password}`);
    return data.entry.hash;
  };
  // No stored code yet: make a random 6-digit one, keep only its hash, and show the code once on the console.
  const logCode = (): Stored & { length: number } => {
    const data = read();
    if (data.logCode) return data.logCode;
    const code = String(randomInt(0, 1_000_000)).padStart(6, "0");
    data.logCode = { hash: hashSecret(code), length: code.length, generated: true };
    try { write(data); } catch { /* unwritable store: the code would be lost, so do not announce it */ return data.logCode; }
    console.warn(`[wl] 로그 확인 코드가 새로 만들어졌습니다 (한 번만 표시): ${code}`);
    return data.logCode;
  };

  return {
    verifyEntry: (password: string) => matches(entryHash(), password),
    /** `typed` is the digits pressed last (up to eight); the code is whatever the stored length of them ends the sequence. */
    verifyLogCode(typed: string): boolean {
      const stored = logCode();
      return /^\d+$/.test(typed) && typed.length >= stored.length && matches(stored.hash, typed.slice(-stored.length));
    },
    state: () => ({ entryCustom: Boolean(read().entry && !read().entry?.generated), codeCustom: Boolean(read().logCode && !read().logCode?.generated), codeLength: logCode().length }),
    /** Needs the current entry password. Either new value may be left out to keep it. */
    change(current: string, entryPassword?: string, code?: string): void {
      if (!matches(entryHash(), current)) throw new Error("현재 입장 비밀번호가 맞지 않습니다.");
      if (entryPassword === undefined && code === undefined) throw new Error("바꿀 값을 입력해주세요.");
      const data = read();
      if (entryPassword !== undefined) {
        if (entryPassword.length < ENTRY_MIN || entryPassword.length > ENTRY_MAX) throw new Error(`새 입장 비밀번호는 ${ENTRY_MIN}~${ENTRY_MAX}자로 입력해주세요.`);
        if (entryPassword === current) throw new Error("현재와 다른 비밀번호를 입력해주세요.");
        data.entry = { hash: hashSecret(entryPassword), updatedAt: new Date().toISOString() };
      }
      if (code !== undefined) {
        if (!new RegExp(`^\\d{${CODE_MIN},${CODE_MAX}}$`).test(code)) throw new Error(`확인 코드는 숫자 ${CODE_MIN}~${CODE_MAX}자리로 입력해주세요.`);
        data.logCode = { hash: hashSecret(code), length: code.length, updatedAt: new Date().toISOString() };
      }
      write(data);
    },
  };
}
