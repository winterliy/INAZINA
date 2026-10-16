import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";
import { api } from "../../shared/api";
import { Card, Field, Tabs, changed, date, number, plain, show, text, type Ctx } from "./common";

type Citizen = { userId: string; nickname: string; nationality: string; cash: number; creditScore: number; status: string; role: string; accountType: "discord" | "country" | "company"; companyCode: string | null };
type WebLogin = { hts_id: string | null; created_epoch: number; updated_epoch: number; auth_version: number };
type UserDetail = { user: Record<string, string | number | null>; accountType: "discord" | "country" | "company"; webLogin: WebLogin | null; accounts: Record<string, unknown>[]; loans: Record<string, unknown>[]; holdings: { asset_code: string; quantity: number }[] };
type Country = { nationality: string; currency_code: string; currency_name: string | null; base_rate: number; legal_max_rate: number; tax_percent: number; treasuryId: string | null; treasuryCash: number | null; citizens: number; guilds: string[]; exchanges: string[]; account: { status: string; role: string; hts_id: string | null } | null };
type Result = { message: string; success: boolean };

const USER_FIELDS: [string, string, string?][] = [
  ["nickname", "닉네임"], ["cash", "현금", "국적 통화 기준"],
  ["credit_score", "신용점수", "0~1000"], ["annual_income", "연소득", "국적 통화 기준"], ["income_stability", "소득 안정성", "0~1"],
];

export function UsersPage({ ctx, currentUserId }: { ctx: Ctx; currentUserId: string }) {
  const [tab, setTab] = useState<"users" | "companies" | "countries">("users");
  const [countries, setCountries] = useState<Country[]>([]);
  const loadCountries = useCallback(async () => setCountries((await api<{ countries: Country[] }>("/api/admin/countries")).countries), []);
  useEffect(() => { loadCountries().catch((cause) => ctx.say(cause instanceof Error ? cause.message : "국가를 불러오지 못했습니다.", true)); }, [loadCountries]);
  return <>
    <Tabs label="사용자 종류" value={tab} onChange={setTab} options={[["users", "사용자"], ["companies", "법인"], ["countries", "국가 · 국가 계정", countries.length]]} />
    {tab === "countries" ? <CountryTab ctx={ctx} countries={countries} reload={loadCountries} />
      : <UserTab key={tab} ctx={ctx} countries={countries} currentUserId={currentUserId} companies={tab === "companies"} />}
  </>;
}

function UserTab({ ctx, countries, currentUserId, companies }: { ctx: Ctx; countries: Country[]; currentUserId: string; companies: boolean }) {
  const { busy, run, say, query, openCommand } = ctx;
  const [citizens, setCitizens] = useState<Citizen[]>([]);
  const [detail, setDetail] = useState<UserDetail | null>(null);
  const [form, setForm] = useState<Record<string, string>>({});
  const [password, setPassword] = useState("");
  const [htsId, setHtsId] = useState("");
  const [nationality, setNationality] = useState("");

  const loadList = useCallback(async () => setCitizens((await api<{ users: Citizen[] }>(`/api/admin/users?search=${encodeURIComponent(query)}${companies ? "&kind=company" : ""}`)).users), [query, companies]);
  useEffect(() => { loadList().catch((cause) => say(cause instanceof Error ? cause.message : "사용자를 불러오지 못했습니다.", true)); }, [loadList]);

  async function open(userId: string) {
    const result = await api<UserDetail>(`/api/admin/user/${encodeURIComponent(userId)}`);
    setDetail(result);
    setForm(Object.fromEntries([...USER_FIELDS.map(([key]) => key), "status", "role"].map((key) => [key, text(result.user[key])])));
    setHtsId(result.webLogin?.hts_id ?? "");
    setNationality(String(result.user.nationality ?? ""));
    setPassword("");
  }
  const select = (userId: string) => run(() => open(userId), "사용자 정보를 불러오지 못했습니다.");

  function saveUser(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!detail) return;
    const changes = changed(form, detail.user);
    if (!Object.keys(changes).length) { say("변경된 값이 없습니다.", true); return; }
    const id = String(detail.user.user_id);
    if (!window.confirm(`${detail.user.nickname} (${id}) 설정을 저장할까요?\n\n${Object.entries(changes).map(([key, value]) => `${key}: ${show(detail.user[key])} → ${value}`).join("\n")}`)) return;
    void run(() => api<Result>(`/api/admin/user/${encodeURIComponent(id)}`, { method: "POST", body: JSON.stringify({ changes }) }),
      "사용자 설정을 저장하지 못했습니다.", async (result) => { say(plain(result.message)); await open(id); await loadList(); });
  }

  function changeNationality() {
    if (!detail) return;
    const id = String(detail.user.user_id);
    const from = countries.find((country) => country.nationality === detail.user.nationality);
    const to = countries.find((country) => country.nationality === nationality);
    if (!to || nationality === detail.user.nationality) return;
    if (!window.confirm(`${detail.user.nickname}의 국적을 ${detail.user.nationality} → ${nationality}(으)로 바꿀까요?\n\n` +
      "· 미체결 주문을 모두 취소합니다\n· 보유 종목·국채를 현재가로 이전 국가 국고에 넘기고 현금화합니다\n· 모든 통장(입출금·외환)을 해지합니다\n· 대출을 현금으로 상환합니다 (부족하면 중단)\n" +
      `· 남은 현금을 ${from?.currency_code ?? "이전 통화"} → ${to.currency_code}로 환전해 지갑에 넣습니다 (환전 수수료는 이전 국가 국고)\n\n` +
      "선물·작성 옵션·ELS가 남아 있으면 거부됩니다. 되돌릴 수 없습니다.")) return;
    void run(() => api<Result>(`/api/admin/user/${encodeURIComponent(id)}`, { method: "POST", body: JSON.stringify({ changes: { nationality } }) }),
      "국적을 바꾸지 못했습니다.", async (result) => { say(plain(result.message)); await open(id); await loadList(); });
  }

  function web(action: "password" | "hts_id" | "logout" | "remove") {
    if (!detail) return;
    const id = String(detail.user.user_id);
    const labels = { password: "웹 비밀번호를 설정", hts_id: htsId ? `HTS ID를 ${htsId}(으)로 변경` : "HTS ID를 삭제", logout: "모든 웹 로그인을 만료", remove: "웹 로그인을 삭제" };
    if (action === "password" && (password.length < 12 || !/[A-Za-z]/.test(password) || !/\d/.test(password))) { say("비밀번호는 영문과 숫자를 포함해 12~128자로 입력해주세요.", true); return; }
    if (!window.confirm(`${detail.user.nickname} (${id})의 ${labels[action]}할까요?`)) return;
    void run(() => api<Result>(`/api/admin/user/${encodeURIComponent(id)}/web`, { method: "POST", body: JSON.stringify({ action, password, htsId }) }),
      "웹 로그인을 변경하지 못했습니다.", async (result) => { say(plain(result.message)); setPassword(""); await open(id); });
  }

  const id = detail ? String(detail.user.user_id) : "";
  return <>
    {detail && <>
      <Card title={`${detail.user.nickname} 설정`} caption={<><span className="mono">{id}</span> · {detail.accountType === "country" ? "국가 계정" : detail.accountType === "company" ? "법인 자본 계정" : "Discord 사용자"}</>}
        actions={<><button className="soft" type="button" onClick={() => openCommand("잔고수정", id)}>잔고 가감 ($잔고수정)</button><button className="soft" type="button" onClick={() => setDetail(null)}>닫기</button></>}>
        <form className="form-grid" onSubmit={saveUser}>
          {USER_FIELDS.map(([key, label, hint]) => <Field key={key} label={label} hint={hint}>
            <input className={key === "nickname" ? undefined : "mono"} value={form[key] ?? ""} onChange={(event) => setForm({ ...form, [key]: event.target.value })} />
          </Field>)}
          <Field label="상태"><select value={form.status ?? ""} disabled={id === currentUserId} onChange={(event) => setForm({ ...form, status: event.target.value })}><option value="active">active (활성)</option><option value="inactive">inactive (비활성)</option><option value="bankrupt">bankrupt (파산)</option></select></Field>
          <Field label="권한"><select value={form.role ?? ""} disabled={id === currentUserId} onChange={(event) => setForm({ ...form, role: event.target.value })}><option value="user">user</option><option value="admin">admin</option><option value="nation">nation (국가)</option></select></Field>
          <button className="btn primary" disabled={busy}>저장</button>
        </form>
      </Card>
      {detail.accountType !== "company" && <><Card title="국적 변경" caption="국적을 바꾸면 모든 자산을 이전 국적 통화로 정리한 뒤 새 국적 통화로 환전합니다.">
        <div className="form-grid">
          <Field label="새 국적" hint={`현재 ${detail.user.nationality}`}><select value={nationality} onChange={(event) => setNationality(event.target.value)}>{countries.map((country) => <option key={country.nationality} value={country.nationality}>{country.nationality} · {country.currency_code}</option>)}</select></Field>
          <button className="btn primary" type="button" disabled={busy || !nationality || nationality === detail.user.nationality || detail.accountType === "country"} onClick={changeNationality}>국적 변경</button>
        </div>
        <ol className="steps">
          <li>미체결 주문 취소 → 보유 종목·국채를 현재가로 이전 국가 국고에 매도</li>
          <li>입출금·외환 통장 해지 (현재는 해지 위약금 없음)</li>
          <li>대출 상환 — 현금이 부족하면 아무것도 바꾸지 않고 중단</li>
          <li>남은 현금을 변경 시점 환율로 새 통화로 환전 (수수료는 이전 국가 국고)</li>
        </ol>
        <p className="hint">선물 포지션·작성한 옵션·ELS가 남아 있으면 변경할 수 없습니다.{detail.accountType === "country" ? " 국가 계정은 국적을 바꿀 수 없습니다." : ""}</p>
      </Card>
      <Card title="웹 로그인 · HTS" caption={detail.webLogin ? `가입 ${date(detail.webLogin.created_epoch)} · 최근 변경 ${date(detail.webLogin.updated_epoch)}` : "웹 계정이 없습니다. 비밀번호를 설정하면 이 ID로 로그인할 수 있습니다."}>
        <div className="form-grid">
          <Field label={detail.webLogin ? "새 비밀번호" : "비밀번호 설정"} hint="영문+숫자 12자 이상"><input type="password" autoComplete="new-password" value={password} onChange={(event) => setPassword(event.target.value)} /></Field>
          <button className="btn primary" type="button" disabled={busy || !password} onClick={() => web("password")}>비밀번호 설정</button>
          {detail.webLogin && <>
            <Field label="HTS ID" hint="비우면 삭제"><input className="mono" value={htsId} onChange={(event) => setHtsId(event.target.value)} /></Field>
            <button className="soft" type="button" disabled={busy} onClick={() => web("hts_id")}>HTS ID 저장</button>
            <button className="soft" type="button" disabled={busy} onClick={() => web("logout")}>모든 로그인 만료</button>
            <button className="soft danger" type="button" disabled={busy || id === currentUserId} onClick={() => web("remove")}>웹 로그인 삭제</button>
          </>}
        </div>
      </Card></>}
      <Card title="계좌 · 대출 · 보유 자산" caption="읽기 전용입니다. 계좌 잔액은 $잔고수정, 그 밖의 값은 DB 편집에서 바꿉니다.">
        <div className="scroll-x"><table><thead><tr><th>계좌번호</th><th>상품</th><th>종류</th><th>통화</th><th className="r">잔액</th><th /></tr></thead><tbody>{detail.accounts.map((account) => <tr key={String(account.account_number)}><td className="mono">{show(account.account_number)}</td><td>{show(account.product_code)}</td><td>{show(account.account_type)}</td><td>{show(account.quote_currency)}</td><td className="r mono">{show(account.balance)}</td><td className="row-actions"><button className="soft small" onClick={() => openCommand("잔고수정", String(account.account_number))}>잔고</button></td></tr>)}</tbody></table>{!detail.accounts.length && <p className="empty">계좌가 없습니다.</p>}</div>
        <div className="scroll-x"><table><thead><tr><th>대출</th><th>상품</th><th>상태</th><th className="r">원금</th><th className="r">잔액</th><th>만기</th></tr></thead><tbody>{detail.loans.map((loan) => <tr key={String(loan.loan_id)}><td className="mono">#{show(loan.loan_id)}</td><td>{show(loan.product_code)}</td><td>{show(loan.status)}</td><td className="r mono">{show(loan.principal)}</td><td className="r mono">{show(loan.outstanding_balance)}</td><td>{loan.maturity_epoch ? date(Number(loan.maturity_epoch)) : "—"}</td></tr>)}</tbody></table>{!detail.loans.length && <p className="empty">대출이 없습니다.</p>}</div>
        <div className="chips">{detail.holdings.map((item) => <span key={item.asset_code} className="tag">{item.asset_code} · {number(item.quantity)}</span>)}{!detail.holdings.length && <span className="hint">보유 자산이 없습니다.</span>}</div>
      </Card>
    </>}
    <Card title={companies ? "법인 계정" : "사용자 계정"} caption={companies ? "법인 자본 계정은 회사가 처음 거래·예금·대출할 때 만들어집니다. 행을 누르면 계좌·대출·보유 자산을 봅니다. 자본 변경은 법인 자본 화면에서 합니다." : "최대 100명 · 행을 누르면 모든 설정을 엽니다. 서버 시민 등록은 관리자 명령어의 $등록·$서버설치를 사용합니다."}>
      <div className="scroll-x tall"><table><thead><tr><th>{companies ? "법인" : "사용자"}</th><th>{companies ? "회사 코드" : "유형"}</th><th>ID</th><th>국적</th><th className="r">현금</th><th className="r">신용</th><th>권한</th><th>상태</th></tr></thead><tbody>
        {citizens.map((citizen) => <tr key={citizen.userId} className={citizen.userId === id ? "clickable selected" : "clickable"} tabIndex={0} onClick={() => void select(citizen.userId)} onKeyDown={(event) => { if (event.key === "Enter") void select(citizen.userId); }}>
          <td>{citizen.nickname}</td><td className={companies ? "mono" : undefined}>{companies ? citizen.companyCode : citizen.accountType === "country" ? "국가" : "Discord"}</td><td className="mono">{citizen.userId}</td><td>{citizen.nationality}</td><td className="r mono">{number(citizen.cash)}</td><td className="r">{number(citizen.creditScore)}</td><td>{citizen.role}</td><td><span className={citizen.status === "active" ? "tag ok" : "tag warn"}>{citizen.status}</span></td>
        </tr>)}
      </tbody></table>{!citizens.length && <p className="empty">{companies ? "법인 계정이 없습니다." : "사용자를 찾지 못했습니다."}</p>}</div>
    </Card>
  </>;
}

function CountryTab({ ctx, countries, reload }: { ctx: Ctx; countries: Country[]; reload: () => Promise<void> }) {
  const { busy, run, say } = ctx;
  const [selected, setSelected] = useState("");
  const [rates, setRates] = useState({ baseRate: "", legalMaxRate: "", taxPercent: "" });
  const [treasury, setTreasury] = useState({ amount: "", reason: "" });
  const [password, setPassword] = useState("");
  const [htsId, setHtsId] = useState("");
  const [draft, setDraft] = useState({ nationality: "", currencyCode: "NWX", baseRate: "", legalMaxRate: "", taxPercent: "" });
  const country = countries.find((item) => item.nationality === selected);

  useEffect(() => {
    if (!country) return;
    setRates({ baseRate: String(country.base_rate), legalMaxRate: String(country.legal_max_rate), taxPercent: String(country.tax_percent) });
    setTreasury({ amount: country.treasuryCash === null ? "" : String(country.treasuryCash), reason: "" });
    setHtsId(country.account?.hts_id ?? "");
    setPassword("");
  }, [country]);

  function act(nationality: string, body: Record<string, unknown>, question: string) {
    if (!window.confirm(question)) return;
    void run(() => api<Result>(`/api/admin/countries/${encodeURIComponent(nationality)}`, { method: "POST", body: JSON.stringify(body) }),
      "국가 설정을 저장하지 못했습니다.", async (result) => { say(plain(result.message), !result.success); await reload(); });
  }

  function saveRates(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!country) return;
    const original = { baseRate: country.base_rate, legalMaxRate: country.legal_max_rate, taxPercent: country.tax_percent };
    const diff = changed(rates, original);
    if (!Object.keys(diff).length) { say("변경된 값이 없습니다.", true); return; }
    act(country.nationality, { action: "rates", ...diff }, `${country.nationality} 금리·세율을 바꿀까요? 연동된 변동금리 상품도 재산정됩니다.\n\n${Object.entries(diff).map(([key, value]) => `${key}: ${show(original[key as keyof typeof original])} → ${value}`).join("\n")}`);
  }

  function countryLogin(action: "create" | "password" | "hts_id" | "logout" | "remove") {
    if (!country) return;
    if ((action === "create" || action === "password") && (password.length < 12 || !/[A-Za-z]/.test(password) || !/\d/.test(password))) { say("비밀번호는 영문과 숫자를 포함해 12~128자로 입력해주세요.", true); return; }
    const labels = { create: "국가 계정을 만들", password: "국가 계정 비밀번호를 바꿀", hts_id: "HTS ID를 저장할", logout: "모든 로그인을 만료할", remove: "국가 계정 웹 로그인을 삭제할" };
    if (!window.confirm(`${country.nationality} ${labels[action]}까요?`)) return;
    const request = action === "create"
      ? api<Result>("/api/admin/users/country-account", { method: "POST", body: JSON.stringify({ countryId: country.nationality, password }) })
      : api<Result>(`/api/admin/user/${encodeURIComponent(country.nationality)}/web`, { method: "POST", body: JSON.stringify({ action, password, htsId }) });
    void run(() => request, "국가 계정을 변경하지 못했습니다.", async (result) => { say(result?.message ? plain(result.message) : `${country.nationality} 국가 계정을 만들었습니다.`); setPassword(""); await reload(); });
  }

  return <>
    <Card title="국가 만들기" caption="$국가생성과 같습니다. 통화는 먼저 금융 상품 탭에서 통화(currency)로 만들어야 합니다.">
      <form className="form-grid" onSubmit={(event) => { event.preventDefault(); act(draft.nationality.trim(), { action: "create", ...draft }, `${draft.nationality} 국가를 만들까요?`); }}>
        <Field label="국가명"><input value={draft.nationality} required onChange={(event) => setDraft({ ...draft, nationality: event.target.value })} /></Field>
        <Field label="통화 코드"><input className="mono" value={draft.currencyCode} required onChange={(event) => setDraft({ ...draft, currencyCode: event.target.value.toUpperCase() })} /></Field>
        <Field label="기준금리 %" hint="선택 · 기본 3"><input className="mono" value={draft.baseRate} onChange={(event) => setDraft({ ...draft, baseRate: event.target.value })} /></Field>
        <Field label="법정최고이자 %" hint="선택 · 기본 20"><input className="mono" value={draft.legalMaxRate} onChange={(event) => setDraft({ ...draft, legalMaxRate: event.target.value })} /></Field>
        <Field label="세율 %" hint="선택 · 기본 3"><input className="mono" value={draft.taxPercent} onChange={(event) => setDraft({ ...draft, taxPercent: event.target.value })} /></Field>
        <button className="btn primary" disabled={busy || !draft.nationality.trim()}>국가 생성</button>
      </form>
    </Card>

    {country && <>
      <Card title={`${country.nationality} 설정`} caption={`통화 ${country.currency_code}${country.currency_name ? ` (${country.currency_name})` : ""} · 시민 ${number(country.citizens)}명 · 거래소 ${country.exchanges.join(", ") || "없음"} · 연결 서버 ${country.guilds.length}개`}
        actions={<><button className="soft danger" type="button" disabled={busy} onClick={() => act(country.nationality, { action: "delete" }, `${country.nationality} 국가를 삭제할까요? 서버·시민·거래소·금리상품이 연결되어 있으면 거부됩니다.`)}>국가 삭제</button><button className="soft" type="button" onClick={() => setSelected("")}>닫기</button></>}>
        <form className="form-grid" onSubmit={saveRates}>
          <Field label="기준금리 %" hint="-100~100 · $금리수정"><input className="mono" value={rates.baseRate} onChange={(event) => setRates({ ...rates, baseRate: event.target.value })} /></Field>
          <Field label="법정최고이자 %" hint="0~100 · $법정최고이자수정"><input className="mono" value={rates.legalMaxRate} onChange={(event) => setRates({ ...rates, legalMaxRate: event.target.value })} /></Field>
          <Field label="세율 %" hint="0~100 · $세율"><input className="mono" value={rates.taxPercent} onChange={(event) => setRates({ ...rates, taxPercent: event.target.value })} /></Field>
          <button className="btn primary" disabled={busy}>금리 · 세율 저장</button>
        </form>
        <form className="form-grid section-gap" onSubmit={(event) => { event.preventDefault(); act(country.nationality, { action: "treasury", ...treasury }, `${country.nationality} 국고 현금을 ${treasury.amount}(으)로 설정할까요?`); }}>
          <Field label="국고 현금" hint={`현재 ${country.treasuryCash === null ? "—" : number(country.treasuryCash)} · 설정값`}><input className="mono" value={treasury.amount} required onChange={(event) => setTreasury({ ...treasury, amount: event.target.value })} /></Field>
          <Field label="사유" wide><input value={treasury.reason} onChange={(event) => setTreasury({ ...treasury, reason: event.target.value })} /></Field>
          <button className="btn primary" disabled={busy}>국고 저장</button>
        </form>
      </Card>
      <Card title="국가 계정 (HTS 로그인)" caption={country.account ? `국가 ID ${country.nationality}로 로그인하면 국고로 거래합니다. 상태 ${country.account.status} · 권한 ${country.account.role}` : "아직 국가 계정이 없습니다. 비밀번호를 정하면 국가 ID로 HTS에 로그인할 수 있습니다."}>
        <div className="form-grid">
          <Field label={country.account ? "새 비밀번호" : "비밀번호"} hint="영문+숫자 12자 이상"><input type="password" autoComplete="new-password" value={password} onChange={(event) => setPassword(event.target.value)} /></Field>
          {country.account ? <>
            <button className="btn primary" type="button" disabled={busy || !password} onClick={() => countryLogin("password")}>비밀번호 변경</button>
            <Field label="HTS ID" hint="비우면 삭제"><input className="mono" value={htsId} onChange={(event) => setHtsId(event.target.value)} /></Field>
            <button className="soft" type="button" disabled={busy} onClick={() => countryLogin("hts_id")}>HTS ID 저장</button>
            <button className="soft" type="button" disabled={busy} onClick={() => countryLogin("logout")}>모든 로그인 만료</button>
            <button className="soft danger" type="button" disabled={busy} onClick={() => countryLogin("remove")}>웹 로그인 삭제</button>
          </> : <button className="btn primary" type="button" disabled={busy || !password} onClick={() => countryLogin("create")}>국가 계정 만들기</button>}
        </div>
      </Card>
    </>}

    <Card title="국가" caption="행을 누르면 금리·세율·국고·국가 계정을 설정합니다. 통화 연결 변경은 DB 편집의 country_currencies에서 합니다.">
      <div className="scroll-x tall"><table><thead><tr><th>국가</th><th>통화</th><th className="r">기준금리</th><th className="r">최고이자</th><th className="r">세율</th><th className="r">국고</th><th className="r">시민</th><th>거래소</th><th>국가 계정</th></tr></thead><tbody>
        {countries.map((item) => <tr key={item.nationality} className={item.nationality === selected ? "clickable selected" : "clickable"} tabIndex={0} onClick={() => setSelected(item.nationality)} onKeyDown={(event) => { if (event.key === "Enter") setSelected(item.nationality); }}>
          <td>{item.nationality}</td><td className="mono">{item.currency_code}</td><td className="r">{item.base_rate}%</td><td className="r">{item.legal_max_rate}%</td><td className="r">{item.tax_percent}%</td><td className="r mono">{item.treasuryCash === null ? "—" : number(item.treasuryCash)}</td><td className="r">{number(item.citizens)}</td><td>{item.exchanges.join(", ") || "—"}</td><td>{item.account ? <span className="tag ok">있음</span> : <span className="tag">없음</span>}</td>
        </tr>)}
      </tbody></table>{!countries.length && <p className="empty">국가가 없습니다.</p>}</div>
    </Card>
  </>;
}
