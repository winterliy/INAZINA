import { useCallback, useEffect, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { api } from "../../shared/api";
import { Card, number, type Ctx } from "./common";

type Column = { name: string; type: string; notNull: boolean; primaryKey: boolean; default: string | null };
type TableInfo = { name: string; editable: boolean; columns: Column[] };
type Cell = string | number | null;
type Row = Record<string, Cell> & { __rowid__?: number };
type TablePage = { table: string; editable: boolean; columns: Column[]; total: number; offset: number; pageSize: number; rows: Row[] };

// DB Browser semantics: NULL is explicit, numeric columns store numbers.
function parseCell(input: string, column: Column | undefined): Cell {
  if (input === "NULL") return null;
  const numeric = /INT|REAL|NUM|FLOA|DOUB|DEC/i.test(column?.type ?? "");
  if (numeric && input.trim() !== "" && Number.isFinite(Number(input))) return Number(input);
  return input;
}
const showCell = (value: Cell | undefined) => value === null || value === undefined ? "NULL" : String(value);

function Icon({ size = 15, children }: { size?: number; children: ReactNode }) {
  return <svg aria-hidden="true" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">{children}</svg>;
}

/** DB 편집: a rail of tables beside a sheet of the chosen one. */
export function DatabasePage({ ctx }: { ctx: Ctx }) {
  const { busy, run, say, query } = ctx;
  const [tables, setTables] = useState<TableInfo[]>([]);
  const [table, setTable] = useState("settings");
  const [tablePage, setTablePage] = useState<TablePage | null>(null);
  const [offset, setOffset] = useState(0);
  const [edits, setEdits] = useState<Record<number, Record<string, string>>>({});
  const [newRow, setNewRow] = useState<Record<string, string> | null>(null);
  const [filter, setFilter] = useState("");

  const failed = (cause: unknown) => say(cause instanceof Error ? cause.message : "테이블을 불러오지 못했습니다.", true);
  useEffect(() => { api<{ tables: TableInfo[] }>("/api/admin/db/tables").then((list) => setTables(list.tables)).catch(failed); }, []);
  const load = useCallback(async () => {
    setTablePage(await api<TablePage>(`/api/admin/db/tables/${encodeURIComponent(table)}?${new URLSearchParams({ search: query, offset: String(offset) })}`));
    setEdits({});
  }, [table, query, offset]);
  useEffect(() => { load().catch(failed); }, [load]);
  useEffect(() => { setOffset(0); setNewRow(null); }, [table, query]);

  const tableInfo = tables.find((item) => item.name === table);
  const columnOf = (name: string) => tableInfo?.columns.find((column) => column.name === name);

  function saveRow(row: Row) {
    const rowid = row.__rowid__!;
    const values = Object.fromEntries(Object.entries(edits[rowid] ?? {}).map(([name, input]) => [name, parseCell(input, columnOf(name))]));
    if (!Object.keys(values).length) return;
    const summary = Object.entries(values).map(([name, value]) => `${name}: ${showCell(row[name])} → ${showCell(value)}`).join("\n");
    if (!window.confirm(`${table} #${rowid} 행을 저장할까요?\n\n${summary}`)) return;
    void run(() => api(`/api/admin/db/tables/${encodeURIComponent(table)}/rows/${rowid}`, { method: "POST", body: JSON.stringify({ values }) }),
      "행을 저장하지 못했습니다.", () => { say(`${table} #${rowid} 행을 저장했습니다.`); void load(); });
  }

  function deleteRow(row: Row) {
    const rowid = row.__rowid__!;
    if (!window.confirm(`${table} #${rowid} 행을 삭제할까요? 되돌릴 수 없으며 작업 기록에 남습니다.`)) return;
    void run(() => api(`/api/admin/db/tables/${encodeURIComponent(table)}/rows/${rowid}/delete`, { method: "POST" }),
      "행을 삭제하지 못했습니다.", () => { say(`${table} #${rowid} 행을 삭제했습니다.`); void load(); });
  }

  function insertRow(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const values = Object.fromEntries(Object.entries(newRow ?? {}).filter(([, input]) => input !== "")
      .map(([name, input]) => [name, parseCell(input, columnOf(name))]));
    if (!Object.keys(values).length) {
      say("한 개 이상의 열 값을 입력해주세요.", true);
      return;
    }
    if (!window.confirm(`${table}에 새 행을 추가할까요?`)) return;
    void run(() => api(`/api/admin/db/tables/${encodeURIComponent(table)}/rows`, { method: "POST", body: JSON.stringify({ values }) }),
      "행을 추가하지 못했습니다.", () => { setNewRow(null); say(`${table}에 행을 추가했습니다.`); void load(); });
  }

  const shownTables = tables.filter((item) => item.name.toLowerCase().includes(filter.trim().toLowerCase()));
  const pageSize = tablePage?.pageSize ?? 50;
  const first = tablePage && tablePage.total ? tablePage.offset + 1 : 0;
  const last = tablePage ? Math.min(tablePage.total, tablePage.offset + tablePage.rows.length) : 0;
  const editedRows = Object.values(edits);
  const editedCells = editedRows.reduce((sum, row) => sum + Object.keys(row).length, 0);
  const dropEdits = (rowid: number) => setEdits(Object.fromEntries(Object.entries(edits).filter(([key]) => Number(key) !== rowid)));

  return <div className="db-studio">
    <aside className="db-rail" aria-label="테이블 목록">
      <label className="search db-filter">
        <Icon size={14}><circle cx="11" cy="11" r="7" /><path d="M20 20l-3.5-3.5" /></Icon>
        <input type="search" aria-label="테이블 이름 필터" placeholder={`테이블 ${number(tables.length)}개`} value={filter} onChange={(event) => setFilter(event.target.value)} />
      </label>
      <div className="db-table-list">
        {shownTables.map((item) => <button key={item.name} type="button" className="db-table" aria-current={item.name === table ? "true" : undefined} onClick={() => setTable(item.name)}>
          <span className="mono">{item.name}</span>
          <small>{item.editable ? `${item.columns.length}열` : <><Icon size={11}><rect x="5" y="11" width="14" height="10" rx="2" /><path d="M8 11V7a4 4 0 0 1 8 0v4" /></Icon>읽기 전용</>}</small>
        </button>)}
        {!shownTables.length && <p className="empty">일치하는 테이블이 없습니다.</p>}
      </div>
    </aside>

    <section className="db-main" aria-label={`${table} 테이블`}>
      <header className="db-head">
        <div className="db-title">
          <p className="eyebrow">TABLE</p>
          <h2 className="mono">{table}</h2>
          <div className="db-meta">
            <span className="tag">{tablePage ? `${number(tablePage.total)}행` : "—"}</span>
            <span className="tag">{tableInfo ? `${tableInfo.columns.length}열` : "—"}</span>
            <span className={tablePage?.editable ? "tag ok" : "tag"}>{tablePage?.editable ? "편집 가능" : "읽기 전용"}</span>
          </div>
        </div>
        <div className="db-actions">
          <button className={newRow ? "outline" : "btn primary"} type="button" disabled={busy || !tablePage?.editable} onClick={() => setNewRow(newRow ? null : {})}>{newRow ? "추가 취소" : "행 추가"}</button>
          <div className="db-pager" role="group" aria-label="페이지">
            <button type="button" aria-label="이전 페이지" disabled={busy || first <= 1} onClick={() => setOffset(Math.max(0, offset - pageSize))}>‹</button>
            <span className="mono">{number(first)}–{number(last)}</span>
            <button type="button" aria-label="다음 페이지" disabled={busy || !tablePage || last >= tablePage.total} onClick={() => setOffset(offset + pageSize)}>›</button>
          </div>
        </div>
      </header>
      <p className="db-hint">칸을 고치면 그 행의 저장 버튼이 켜집니다. <code>NULL</code>을 입력하면 NULL로 저장합니다. DB 제약과 트리거 검증을 그대로 거치며 모든 변경은 작업 기록에 남습니다. 비밀번호·작업 기록 테이블은 표시하지 않습니다.</p>

      {newRow && tableInfo && <Card title="새 행" caption="비워 둔 열은 기본값을 씁니다." className="db-insert">
        <form onSubmit={insertRow}>
          <div className="db-insert-grid">
            {tableInfo.columns.map((column) => <label key={column.name} className="db-field">
              <span className="db-field-name mono">{column.name}{column.primaryKey && <em>PK</em>}{column.notNull && <em>필수</em>}</span>
              <input value={newRow[column.name] ?? ""} placeholder={column.default !== null ? `기본 ${column.default}` : column.type || "ANY"} onChange={(event) => setNewRow({ ...newRow, [column.name]: event.target.value })} />
              <small>{column.type || "ANY"}</small>
            </label>)}
          </div>
          <div className="db-insert-actions"><button type="button" className="outline" onClick={() => setNewRow(null)}>취소</button><button className="btn primary" disabled={busy}>행 추가</button></div>
        </form>
      </Card>}

      <div className="db-sheet">
        <table className="db-grid">
          <thead><tr>
            <th className="db-rowid">#</th>
            {tablePage?.columns.map((column) => <th key={column.name} title={column.type}>
              <span className="mono">{column.name}{column.primaryKey && <svg aria-label="기본 키" role="img" width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round"><circle cx="8" cy="15" r="4" /><path d="M11 12l8-8M16 7l3 3" /></svg>}</span>
              <small>{column.type || "ANY"}</small>
            </th>)}
            {tablePage?.editable && <th className="db-row-actions" aria-label="행 작업" />}
          </tr></thead>
          <tbody>
            {tablePage?.rows.map((row) => {
              const rowid = row.__rowid__ ?? -1;
              const rowEdits = edits[rowid] ?? {};
              const dirty = rowid in edits;
              return <tr key={rowid} className={dirty ? "dirty" : undefined}>
                <td className="db-rowid mono">{rowid >= 0 ? rowid : "—"}</td>
                {tablePage.columns.map((column) => {
                  const edited = column.name in rowEdits;
                  const isNull = row[column.name] === null && !edited;
                  const value = edited ? rowEdits[column.name] : showCell(row[column.name]);
                  return <td key={column.name} className={edited ? "edited" : isNull ? "null" : undefined}>
                    {tablePage.editable
                      ? <input aria-label={`${rowid} ${column.name}`} className="mono" value={value} onChange={(event) => setEdits({ ...edits, [rowid]: { ...rowEdits, [column.name]: event.target.value } })} />
                      : <span className="mono">{value}</span>}
                  </td>;
                })}
                {tablePage.editable && <td className="db-row-actions">
                  <button type="button" className="db-icon save" aria-label={`${rowid} 행 저장`} title="저장" disabled={busy || !dirty} onClick={() => saveRow(row)}><Icon><path d="M5 12.5l4.5 4.5L19 7.5" /></Icon></button>
                  <button type="button" className="db-icon" aria-label={`${rowid} 행 변경 취소`} title="변경 취소" disabled={busy || !dirty} onClick={() => dropEdits(rowid)}><Icon><path d="M9 14L4 9l5-5" /><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11" /></Icon></button>
                  <button type="button" className="db-icon danger" aria-label={`${rowid} 행 삭제`} title="삭제" disabled={busy} onClick={() => deleteRow(row)}><Icon><path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3" /></Icon></button>
                </td>}
              </tr>;
            })}
          </tbody>
        </table>
        {tablePage && !tablePage.rows.length && <p className="empty">{query ? "검색과 일치하는 행이 없습니다." : "행이 없습니다."}</p>}
      </div>

      {editedRows.length > 0 && <div className="db-pending" role="status">
        <span>저장하지 않은 변경 · {number(editedRows.length)}행 {number(editedCells)}칸</span>
        <button type="button" className="outline" onClick={() => setEdits({})}>모두 되돌리기</button>
      </div>}
    </section>
  </div>;
}
