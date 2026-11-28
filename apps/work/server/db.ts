import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import mysql from "mysql2/promise";

export const pool = mysql.createPool({
  host: "127.0.0.1", port: Number(process.env.WORK_DB_PORT ?? 3307), user: "inazina_work", password: process.env.WORK_DB_PASSWORD,
  database: "inazina_work", waitForConnections: true, connectionLimit: 10, charset: "utf8mb4", multipleStatements: true,
  dateStrings: true, timezone: "+09:00", supportBigNumbers: true, bigNumberStrings: false,
});

const here = dirname(fileURLToPath(import.meta.url));

// Create (or complete) the tables and the withdrawal procedure. Both files are idempotent, so this runs at every start.
export async function migrate(): Promise<void> {
  await pool.query(readFileSync(join(here, "../db/schema.sql"), "utf8"));
  const conn = await pool.getConnection();
  try {
    await conn.query("DROP PROCEDURE IF EXISTS work_withdraw");
    await conn.query(readFileSync(join(here, "../db/withdraw.sql"), "utf8"));
  } finally { conn.release(); }
}

export async function rows<T = any>(sql: string, args: unknown[] = []): Promise<T[]> {
  const [result] = await pool.query(sql, args);
  return result as T[];
}
export async function one<T = any>(sql: string, args: unknown[] = []): Promise<T | undefined> { return (await rows<T>(sql, args))[0]; }
export async function run(sql: string, args: unknown[] = []): Promise<{ insertId: number; affectedRows: number }> {
  const [result] = await pool.query(sql, args);
  return result as any;
}
