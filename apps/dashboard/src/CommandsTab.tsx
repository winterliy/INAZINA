import { lazy } from "react";
import type { FormEvent } from "react";
import { api } from "../../shared/api";
import { CommandForm } from "./CommandForm";
import { confirmAction, date, plain, type Ctx } from "./common";
import { LogCard } from "./lazyLog";
import { HELP, NATIONAL, categoryOf, type CommandTab } from "./useCommandTab";
const GambleTile = lazy(() => import("./GambleTile").then((module) => ({ default: module.GambleTile })));
const IdentityCard = lazy(() => import("./IdentityCard").then((module) => ({ default: module.IdentityCard })));

type RunResult = { message: string; success: boolean };

export function CommandsTab({ ctx, tab, openProducts, onChanged }: { ctx: Ctx; tab: CommandTab; openProducts: (group: string) => void; onChanged: () => void }) {
  const { busy, run } = ctx;
  const { scope, command, nationalCommand, nationalValues, selectedNational, nationalCommands, helpPages, helpPage } = tab;
  const categoryCount = (category: string) => tab.commands.filter((item) => categoryOf(item.name) === category).length;

  async function runCommand(args: string) {
    if (!await confirmAction(`다음 명령어를 실행할까요?\n\n$${command}${args ? ` ${args}` : ""}`)) return;
    void run(() => api<RunResult>("/api/admin/commands", { method: "POST", body: JSON.stringify({ command, arguments: args }) }),
      "명령어를 실행하지 못했습니다.", (result) => {
        tab.addResult({ command, arguments: args, message: result.message, success: result.success, national: false });
        if (result.success) tab.setArgs("");
      });
  }

  async function runNationalCommand(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selectedNational) return;
    const summary = selectedNational.fields.map((field, index) => `${field.label}: ${nationalValues[index] || "—"}`).join("\n");
    if (!await confirmAction(`국가(국고) 명의로 $${nationalCommand}을(를) 실행할까요?\n\n${summary}`)) return;
    void run(() => api<RunResult>("/api/admin/commands", { method: "POST", body: JSON.stringify({ national: true, command: nationalCommand, values: nationalValues }) }),
      "명령어를 실행하지 못했습니다.", (result) => {
        tab.addResult({ command: nationalCommand, arguments: nationalValues.filter(Boolean).join(" "), message: result.message, success: result.success, national: true });
      });
  }

  return <div className="command-layout">
    <nav className="command-rail" aria-label="명령어 카테고리">
      <p>관리자 명령어</p>
      {tab.categories.map((category) => <button key={category} type="button" aria-current={tab.section === category ? "page" : undefined} onClick={() => tab.openCategory(category)}>{category}<span>{categoryCount(category)}</span></button>)}
      <p>국가 · 참고</p>
      <button type="button" aria-current={tab.section === NATIONAL ? "page" : undefined} onClick={() => tab.openCategory(NATIONAL)}>국가 명의 실행<span>{nationalCommands.length}</span></button>
      <button type="button" aria-current={tab.section === HELP ? "page" : undefined} onClick={() => tab.openCategory(HELP)}>도움말 ($도움말)</button>
    </nav>
    <div className="command-content">
      {scope === "admin" && <div className="feature-pair"><GambleTile ctx={ctx} /></div>}
      {scope === "admin" && <section className="command-category-head rise"><p className="eyebrow">관리자 명령어</p><h2>{tab.activeCategory || "불러오는 중…"}</h2><p className="cap">{tab.visibleCommands.length}개 명령어 · 고르면 아래에 입력 칸이 열립니다. 시장 사건($사건)은 시장 사건 화면에서 등록합니다.</p>
        <div className="command-list">{tab.visibleCommands.map((item) => <button key={item.name} type="button" className={item.name === command ? "command-chip active" : "command-chip"} aria-pressed={item.name === command} onClick={() => tab.chooseCommand(item.name)}><b>${item.name}</b><small>{item.usage || "인자 없음"}</small></button>)}</div>
      </section>}
      {scope === "admin" && command && <section className="glass card mb rise"><div className="card-head"><div><h2>${command}</h2><p className="cap">아래 미리보기는 Discord에 입력하는 `$명령어 인자`와 같습니다.</p></div></div>
        <CommandForm key={command} command={command} usage={tab.selectedCommand?.usage ?? ""} prefill={tab.args} busy={busy} onRun={runCommand} openProducts={openProducts} />
        {(command === "상품생성" || command === "상품수정") && <details className="help"><summary>상품 종류별 순서형 인자</summary><pre>{tab.creationHelp}</pre></details>}
      </section>}
      {scope === "help" && <section className="glass card mb rise"><div className="card-head"><div><h2>{helpPages[helpPage]?.label ?? "도움말"}</h2><p className="cap">Discord $도움말과 같은 내용입니다.</p></div><div className="actions"><select aria-label="도움말 페이지" value={helpPage} onChange={(event) => tab.setHelpPage(Number(event.target.value))}>{helpPages.map((item, index) => <option key={item.label} value={index}>{item.label}</option>)}</select></div></div>
        {helpPages[helpPage] && <div className="help-page"><pre>{plain(helpPages[helpPage].description)}</pre>{helpPages[helpPage].fields.map((field) => <div key={field.name} className="help-field"><h3>{field.name}</h3><pre>{plain(field.value)}</pre></div>)}</div>}
        {!helpPages.length && <p className="empty">도움말을 불러오는 중입니다.</p>}
      </section>}
      {scope === "national" && <IdentityCard ctx={ctx} />}
      {scope === "national" && <section className="glass card mb rise"><div className="card-head"><div><h2>국가(국고) 명의 실행</h2><p className="cap">Discord `$매수`·`$송금` 등과 같이 국고 계정으로 실행하고 작업 기록에 남깁니다. {tab.discordOnly.length > 0 && `${tab.discordOnly.map((name) => `$${name}`).join(", ")}은(는) Discord 메시지를 보내야 해서 Discord에서만 사용할 수 있습니다.`}</p></div></div>
        <form className="form-grid" onSubmit={runNationalCommand}>
          <label>명령어<select value={nationalCommand} onChange={(event) => tab.setNationalCommand(event.target.value)}>{nationalCommands.map((item) => <option key={item.name} value={item.name}>${item.name}</option>)}</select></label>
          {selectedNational?.fields.map((field, index) => <label key={`${nationalCommand}-${field.label}`} className={field.long ? "span-wide" : undefined}>{field.label}{!field.required && <small>선택</small>}<input className={field.long ? "mono" : undefined} value={nationalValues[index] ?? ""} required={field.required} onChange={(event) => tab.setNationalValues(nationalValues.map((value, position) => position === index ? event.target.value : value))} /></label>)}
          <button className="btn primary" disabled={busy || !selectedNational}>{busy ? "실행 중…" : selectedNational?.fields.length ? "실행" : "조회"}</button>
        </form>
      </section>}
      {scope === "national" && <LogCard ctx={ctx} log="jobs" title="Discord 봇 처리 대기열" caption="$주총처럼 Discord 메시지를 보내야 하는 작업은 실행 중인 봇이 처리합니다. 봇이 꺼져 있으면 대기 상태로 남습니다."
        rows={tab.jobs} idOf={(job) => job.job_id} deletable={(job) => job.status === "done" || job.status === "failed"} keepNote="대기·처리 중인 작업은 남습니다."
        empty="요청한 작업이 없습니다." onChanged={onChanged}
        head={<><th>#</th><th>작업</th><th>상태</th><th>요청</th><th>결과</th><th>시각</th></>}
        row={(job) => <><td className="mono">#{job.job_id}</td><td>{job.kind === "shareholder_meeting" ? "$주총" : job.kind}</td><td><span className={job.status === "done" ? "tag ok" : job.status === "failed" ? "tag warn" : "tag"}>{{ pending: "대기", running: "처리 중", done: "완료", failed: "실패" }[job.status]}</span></td><td className="mono audit-payload">{job.payload}</td><td className="audit-payload">{job.result ? plain(job.result) : "—"}</td><td>{date(job.updated_epoch)}</td></>} />}
      {scope === "national" && <section className="glass card mb rise"><div className="card-head"><div><h2>국가 명의 명령어</h2><p className="cap">{nationalCommands.length}개 · 선택하면 위 입력에 적용됩니다.</p></div></div><div className="command-list">{nationalCommands.map((item) => <button key={item.name} type="button" className={item.name === nationalCommand ? "command-chip active" : "command-chip"} onClick={() => tab.setNationalCommand(item.name)}><b>${item.name}</b><small>{item.fields.map((field) => field.label).join(" · ") || "조회"}</small></button>)}</div></section>}
      {scope !== "help" && <section className="glass card mb rise"><div className="card-head"><div><h2>실행 결과</h2><p className="cap">이 화면에서 실행한 최근 20건 · 전체 기록은 작업 기록에서 확인합니다.</p></div></div>
        {tab.results.map((item) => <div key={item.id} className={item.success ? "result" : "result failed"}><div className="result-head"><span className="mono">{item.national && <span className="tag inline-start">국가</span>}${item.command} {item.arguments}</span><span>{date(item.time)}</span></div><pre>{plain(item.message)}</pre></div>)}
        {!tab.results.length && <p className="empty">아직 실행한 명령어가 없습니다.</p>}
      </section>}
    </div>
  </div>;
}
