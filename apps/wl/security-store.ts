// Passwords of the Wide-Learning viewer (port 4315), kept on the server in database/wl-security.json
// (ignored by git): the entry password, and the code that opens the hidden login-attempt log. Both
// are stored salted (PBKDF2-SHA256). Until the first change, the entry password is the built-in one in
// src/entry-password.ts and the log code is 0154.
import { createHash, pbkdf2Sync, randomBytes, timingSafeEqual } from "node:crypto";
import { chmodSync, existsSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { PASSWORD_SHA256 } from "./src/entry-password.ts";

const ITERATIONS = 210_000;
const DEFAULT_LOG_CODE = "0154";
export const ENTRY_MIN = 8, ENTRY_MAX = 64, CODE_MIN = 4, CODE_MAX = 8;

type Stored = { hash: string; updatedAt?: string };
type File = { entry?: Stored; logCode?: Stored & { length: number } };

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
    if (!iterations || !salt || !digest) return false;
    return same(pbkdf2Sync(secret, Buffer.from(salt, "hex"), Number(iterations), 32, "sha256").toString("hex"), digest);
  }
  return same(sha256(secret), stored);
}

export function wlSecurity(root: string, path = join(root, "database", "wl-security.json")) {
  const read = (): File => {
    try { return existsSync(path) ? JSON.parse(readFileSync(path, "utf8")) as File : {}; } catch { return {}; }
  };
  const write = (data: File) => {
    const temp = `${path}.${process.pid}.tmp`;
    writeFileSync(temp, JSON.stringify(data, null, 2), { mode: 0o600 });
    chmodSync(temp, 0o600);
    renameSync(temp, path);
  };
  const entryHash = () => read().entry?.hash ?? PASSWORD_SHA256;
  const logCode = (): Stored & { length: number } => read().logCode ?? { hash: sha256(DEFAULT_LOG_CODE), length: DEFAULT_LOG_CODE.length };

  return {
    verifyEntry: (password: string) => matches(entryHash(), password),
    /** `typed` is the digits pressed last (up to eight); the code is whatever the stored length of them ends the sequence. */
    verifyLogCode(typed: string): boolean {
      const stored = logCode();
      return /^\d+$/.test(typed) && typed.length >= stored.length && matches(stored.hash, typed.slice(-stored.length));
    },
    state: () => ({ entryCustom: Boolean(read().entry), codeCustom: Boolean(read().logCode), codeLength: logCode().length }),
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
