import { useCallback, useEffect, useState } from "react";
import { api, appUrl } from "../../shared/api";
import { Card, Field, plain, type Ctx } from "./common";

type Identity = { identity: { account: string; name: string }; countries: string[]; companies: { code: string; name: string }[] };

/** $login / $logout: which treasury or company the national commands act for (shared with Discord). */
export function IdentityCard({ ctx }: { ctx: Ctx }) {
  const { busy, run, say } = ctx;
  const [state, setState] = useState<Identity | null>(null);
  const [target, setTarget] = useState("");
  const load = useCallback(async () => setState(await api<Identity>("/api/admin/companies/identity")), []);
  useEffect(() => { load().catch((cause) => say(cause instanceof Error ? cause.message : "명의를 불러오지 못했습니다.", true)); }, [load]);

  function apply(next: string) {
    void run(() => api<{ message: string; success: boolean }>("/api/admin/companies/identity", { method: "POST", body: JSON.stringify({ target: next }) }),
      "명의를 바꾸지 못했습니다.", async (result) => { say(plain(result.message), !result.success); await load(); });
  }

  // $login on the web: sign in as that country or company account itself, with the user HTS.
  function signIn(next: string) {
    if (!window.confirm(`${next} 계정으로 로그인할까요?\n일반 사용자와 같은 HTS 화면으로 이 계정을 사용합니다. 로그아웃하면 관리자 계정으로 돌아옵니다.`)) return;
    void run(() => api<{ userId: string }>("/api/admin/login-as", { method: "POST", body: JSON.stringify({ target: next }) }),
      "계정으로 로그인하지 못했습니다.", () => location.assign(appUrl("hts")));
  }

  return <Card title="실행 명의 ($login · $logout)" caption={<>현재 <b>{state?.identity.name ?? "…"}</b> (<span className="mono">{state?.identity.account ?? "…"}</span>) 명의로 실행합니다. Discord의 $login과 같은 설정이라 양쪽에 함께 적용됩니다.</>}>
    <form className="form-grid" onSubmit={(event) => { event.preventDefault(); if (target) apply(target); }}>
      <Field label="국가 또는 법인"><select value={target} onChange={(event) => setTarget(event.target.value)}>
        <option value="">선택</option>
        <optgroup label="국가 (국고)">{state?.countries.map((country) => <option key={country} value={country}>{country}</option>)}</optgroup>
        <optgroup label="법인 (자본 계정)">{state?.companies.map((company) => <option key={company.code} value={company.code}>{company.name} ({company.code})</option>)}</optgroup>
      </select></Field>
      <button className="btn primary" disabled={busy || !target}>로그인</button>
      <button className="soft" type="button" disabled={busy} onClick={() => apply("")}>로그아웃 (낙원 국고)</button>
      <button className="soft" type="button" disabled={busy || !target} onClick={() => signIn(target)}>이 계정으로 HTS 로그인</button>
    </form>
  </Card>;
}
