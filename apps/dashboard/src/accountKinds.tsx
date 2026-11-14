/** 통장 종류 guide for the product wizard; keys match managers/deposit_accounts.py ACCOUNT_KINDS. */
type Guide = { label: string; summary: string; detail: string; recommended: Record<string, string> };

const DAY = "86400";
const YEAR = "31536000";
const MONTH = "2592000";

export const ACCOUNT_KIND_GUIDE: Record<string, Guide> = {
  demand: {
    label: "입출금통장", summary: "언제든 넣고 빼는 기본 통장",
    detail: "자유 입출금 · 이자는 거의 없음. 배당·정산·환전·대출 상환·정기적금 납입 같은 자동 처리는 입출금 계열 통장만 씁니다.",
    recommended: { interest_rate: "0.1", deposit_amount: "0", maturity_days: "0", interest_cycle: DAY, compound_type: "0" },
  },
  parking: {
    label: "파킹통장", summary: "입출금 자유 + 한도까지 우대금리",
    detail: "잔액 중 우대 한도(preferred_limit)까지만 우대금리, 넘는 부분은 excess_rate. 매일 이자.",
    recommended: { interest_rate: "2.5", deposit_amount: "0", maturity_days: "0", interest_cycle: DAY, compound_type: "1" },
  },
  cma: {
    label: "CMA", summary: "증권사 수시입출 계좌 · 매일 이자",
    detail: "하루만 맡겨도 이자(이자주기 1일 고정), 만기 없음. 예금자보호 대상이 아니라 발행 기관이 파산하면 손실이 날 수 있습니다.",
    recommended: { interest_rate: "2.8", deposit_amount: "0", maturity_days: "0", interest_cycle: DAY, compound_type: "1" },
  },
  time_deposit: {
    label: "정기예금", summary: "목돈을 한 번에 넣고 만기까지 묶음",
    detail: "가입할 때 한 번만 예치, 추가 입금·일부 출금 불가(해지만 가능). 중도해지 이율 적용. 만기 후엔 만기후이율(1개월 50% → 3개월 30% → 연 0.2%).",
    recommended: { interest_rate: "3.0", deposit_amount: "1000000", maturity_days: "365", interest_cycle: YEAR, compound_type: "0" },
  },
  fixed_installment: {
    label: "정기적금", summary: "매 주기 같은 금액을 자동 납입",
    detail: "약정 금액만 납입(주기마다 지갑 → 입출금 계열 통장에서 자동), 미납 기록, 출금 불가, 만기 후 납입 중단. 만기후이율 1개월 50% → 6개월 30% → 20%.",
    recommended: { interest_rate: "3.5", deposit_amount: "0", maturity_days: "365", interest_cycle: YEAR, compound_type: "0",
                   installment_amount: "100000", installment_cycle_seconds: MONTH },
  },
  free_installment: {
    label: "자유적금", summary: "만기까지 원할 때 원하는 만큼 납입",
    detail: "납입 기간(30일)마다 한도(period_limit)까지 자유 납입, 출금 불가(해지만), 만기 후 입금 불가. 만기후이율은 정기적금과 같음.",
    recommended: { interest_rate: "3.2", deposit_amount: "0", maturity_days: "365", interest_cycle: YEAR, compound_type: "0" },
  },
  housing: {
    label: "주택청약", summary: "청약 자격을 위한 장기 저축 · 1인 1계좌",
    detail: "회당 2만~50만(한 달 50만까지), 회차당 25만 인정. 출금 불가. 이자는 해지할 때 가입기간 금리로 지급: 1개월 이내 0%, 1년 미만 2.3%, 2년 미만 2.8%, 2년 이상 3.1%.",
    recommended: { interest_rate: "0", deposit_amount: "20000", maturity_days: "0", interest_cycle: YEAR, compound_type: "0" },
  },
  combined: {
    label: "종합통장", summary: "입출금통장 + 마이너스 대출",
    detail: "잔액이 모자라면 한도 안에서 자동 대출, 입금되면 자동 상환. 하루 최고 사용액에 연 이자, 매달 대출 잔액에 가산. 한도 초과는 연체 가산금리. 3개월 후 사용률 50% 미만이면 매달 10%씩(최대 50%) 한도 축소. 신용점수 기준 이상만 개설.",
    recommended: { interest_rate: "0.1", deposit_amount: "0", maturity_days: "0", interest_cycle: DAY, compound_type: "0" },
  },
  isa: {
    label: "ISA", summary: "절세형 종합 자산관리 계좌 · 1인 1계좌",
    detail: "연 2,000만(미사용분 이월)·총 1억 납입, 납입 원금 범위 안에서만 중도인출. 이자는 세금 없이 쌓였다가 해지 때 정산: 3년 이후면 200만 비과세·초과분 9.9%, 이전이면 일반 세율.",
    recommended: { interest_rate: "3.0", deposit_amount: "0", maturity_days: "0", interest_cycle: YEAR, compound_type: "1" },
  },
  irp: {
    label: "IRP", summary: "개인형 퇴직연금",
    detail: "연 1,800만 납입, 출금 불가(해지만). 연말정산 때 전년 납입분 900만까지 세액공제 환급(총급여 5,500만 이하 16.5%, 초과 13.2%). 해지 때 공제받은 납입액+이자에 5년 전 16.5%, 이후 5.5%.",
    recommended: { interest_rate: "3.0", deposit_amount: "0", maturity_days: "0", interest_cycle: YEAR, compound_type: "1" },
  },
};

const TERM_NAMES: Record<string, string> = {
  preferred_limit: "우대금리 한도", excess_rate: "한도 초과분 금리 %", deposit_insured: "예금자보호",
  post_maturity: "만기후이율", period_days: "납입 기간(일)", period_limit: "기간당 납입 한도",
  min_payment: "1회 최소 납입", max_payment: "기간당 최대 납입", recognized_payment: "회차당 인정 금액",
  rate_tiers: "가입기간별 금리", overdraft_limit: "마이너스 한도", overdraft_rate: "대출 금리 %",
  min_credit_score: "최소 신용점수", late_spread: "연체 가산 %p", review_after_days: "한도 재검토 시작(일)",
  usage_floor_percent: "최소 사용률 %", reduce_percent: "축소율 %", min_limit_percent: "최저 한도 %",
  annual_limit: "연간 납입한도", total_limit: "총 납입한도", carry_over: "한도 이월", lock_days: "의무가입(일)",
  tax_free: "비과세 한도", separate_tax_percent: "분리과세 %", credit_limit: "세액공제 대상 한도",
  credit_percent_low_income: "공제율(저소득) %", credit_percent: "공제율 %", income_threshold: "저소득 기준 연소득",
  early_tax_percent: "중도해지 세율 %", pension_after_days: "연금 수령 가능(일)", pension_tax_percent: "연금소득세 %",
};
type Tier = { days?: number; percent?: number; rate?: number };
const showTerm = (key: string, value: unknown): string => {
  if (Array.isArray(value)) return (value as Tier[]).map((tier) =>
    `${tier.days != null ? `~${tier.days}일` : "이후"} ${tier.percent != null ? `약정 ${tier.percent}%` : `연 ${tier.rate}%`}`).join(" → ");
  if (key === "deposit_insured") return value ? "대상" : "제외";
  if (key === "carry_over") return value ? "가능" : "불가";
  return typeof value === "number" ? value.toLocaleString("ko-KR") : String(value);
};

const FIELD_NAMES: Record<string, string> = {
  interest_rate: "연 이율", deposit_amount: "최소 예치금", maturity_days: "만기",
  interest_cycle: "이자 주기", compound_type: "이자 방식", installment_amount: "자동 납입 금액",
  installment_cycle_seconds: "자동 납입 주기",
};
const PERIODS: Record<string, string> = { [DAY]: "1일", [MONTH]: "1달", [YEAR]: "1년" };
const showValue = (key: string, value: string) =>
  key === "interest_rate" ? `${value}%`
  : key === "compound_type" ? (value === "1" ? "복리" : "단리")
  : key === "interest_cycle" || key === "installment_cycle_seconds" ? (PERIODS[value] ?? `${value}초`)
  : key === "maturity_days" ? (value === "0" ? "없음" : `${value}일`)
  : Number(value).toLocaleString("ko-KR");

/** Place the tooltip in the viewport (16px gutter): below the (i), or above when there is no room. */
function placeTip(info: HTMLElement) {
  const tip = info.querySelector<HTMLElement>(".kind-tip");
  if (!tip) return;
  const anchor = info.getBoundingClientRect();
  const width = tip.offsetWidth, height = tip.offsetHeight;
  const viewWidth = document.documentElement.clientWidth, viewHeight = window.innerHeight;
  const x = Math.min(Math.max(16, anchor.left - 8), viewWidth - 16 - width);
  const below = anchor.bottom + 8;
  const above = anchor.top - 8 - height;
  // Below when it fits, else above, else as high as the viewport allows.
  const y = below + height <= viewHeight - 16 ? below : above >= 16 ? above : Math.max(16, viewHeight - 16 - height);
  tip.style.setProperty("--tip-x", `${Math.max(16, x)}px`);
  tip.style.setProperty("--tip-y", `${y}px`);
}

/** Kind chips; the (i) on each explains the kind and its recommended terms on hover or focus. */
export function AccountKindPicker({ value, terms, onPick }: { value: string; terms?: Record<string, Record<string, unknown>>; onPick: (kind: string, recommended: Record<string, string>) => void }) {
  return <div className="kind-picker" role="radiogroup" aria-label="통장 종류">
    {Object.entries(ACCOUNT_KIND_GUIDE).map(([kind, guide]) => <div key={kind} className="kind-chip-wrap">
      <button type="button" role="radio" aria-checked={value === kind} className="kind-chip" onClick={() => onPick(kind, guide.recommended)}>
        {guide.label}
      </button>
      <span className="kind-info" tabIndex={0} onMouseEnter={(event) => placeTip(event.currentTarget)} onFocus={(event) => placeTip(event.currentTarget)} aria-label={`${guide.label} 설명과 추천값`}>i
        <span className={terms?.[kind] && Object.keys(terms[kind]).length ? "kind-tip has-terms" : "kind-tip"} role="tooltip">
          <span className="kind-tip-intro"><b>{guide.label}</b> · {guide.summary}
            <span className="kind-tip-detail">{guide.detail}</span></span>
          <span className="kind-tip-col">
            <span className="kind-tip-head">추천값</span>
            <dl>{Object.entries(guide.recommended).map(([key, raw]) => <div key={key}><dt>{FIELD_NAMES[key] ?? key}</dt><dd>{showValue(key, raw)}</dd></div>)}</dl>
          </span>
          {terms?.[kind] && Object.keys(terms[kind]).length > 0 && <span className="kind-tip-col">
            <span className="kind-tip-head">약관 기본값 (kind_terms로 변경)</span>
            <dl>{Object.entries(terms[kind]).map(([key, raw]) => <div key={key}><dt>{TERM_NAMES[key] ?? key}</dt><dd>{showTerm(key, raw)}</dd></div>)}</dl>
          </span>}
        </span>
      </span>
    </div>)}
  </div>;
}
