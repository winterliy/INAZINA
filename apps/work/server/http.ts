import type { IncomingMessage, ServerResponse } from "node:http";

export type Member = { id: number; userRef: string; name: string; role: "admin" | "member" | "guest" };
export type Ctx = { member: Member; params: string[]; query: URLSearchParams; body: any };
export type Handler = (ctx: Ctx) => Promise<unknown>;
export class HttpError extends Error { constructor(public status: number, message: string) { super(message); } }

export const bad = (message: string) => new HttpError(400, message);
export const forbidden = () => new HttpError(403, "forbidden");
export const notFound = () => new HttpError(404, "not_found");

export function str(value: unknown, max: number, field: string, optional = false): string | null {
  if (value === undefined || value === null || value === "") { if (optional) return null; throw bad(`${field}_required`); }
  if (typeof value !== "string" || value.length > max) throw bad(`${field}_invalid`);
  return value;
}
export function int(value: unknown, field: string): number {
  const n = Number(value);
  if (!Number.isSafeInteger(n) || n <= 0) throw bad(`${field}_invalid`);
  return n;
}
export function ids(value: unknown, field: string, max = 200): number[] {
  if (!Array.isArray(value) || value.length > max) throw bad(`${field}_invalid`);
  return [...new Set(value.map((v) => int(v, field)))];
}
export function oneOf<T extends string>(value: unknown, allowed: readonly T[], field: string): T {
  if (typeof value !== "string" || !(allowed as readonly string[]).includes(value)) throw bad(`${field}_invalid`);
  return value as T;
}
export function dateTime(value: unknown, field: string): string {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2})?$/.test(value)) throw bad(`${field}_invalid`);
  return value.replace("T", " ");
}
export function date(value: unknown, field: string, optional = false): string | null {
  if (value === undefined || value === null || value === "") { if (optional) return null; throw bad(`${field}_required`); }
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) throw bad(`${field}_invalid`);
  return value;
}

export function send(response: ServerResponse, status: number, body: unknown, headers: Record<string, string> = {}): void {
  const text = JSON.stringify(body);
  response.writeHead(status, { "content-type": "application/json; charset=utf-8", "cache-control": "no-store", "x-content-type-options": "nosniff", ...headers });
  response.end(text);
}

export async function readBody(request: IncomingMessage, limit: number): Promise<any> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of request) {
    size += (chunk as Buffer).length;
    if (size > limit) throw new HttpError(413, "too_large");
    chunks.push(chunk as Buffer);
  }
  if (!size) return {};
  try { return JSON.parse(Buffer.concat(chunks).toString("utf8")); } catch { throw bad("invalid_json"); }
}
