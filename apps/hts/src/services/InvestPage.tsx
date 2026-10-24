import { useState } from "react";
import { ActionCard, type ServiceProps } from "./ServiceKit";

const SECTIONS = [
  ["options", "옵션"], ["futures", "선물"], ["swap", "금리스왑"], ["mezzanine", "메자닌 채권"], ["wrap", "랩어카운트"],
] as const;

/** 투자상품: derivatives, convertible bonds and wrap accounts as product sections. Trading itself stays on the HTS. */
export function InvestPage({ kit }: ServiceProps) {
  const [section, setSection] = useState<(typeof SECTIONS)[number][0]>("options");
  return <div className="svc-invest">
    <div className="svc-tabs" role="tablist" aria-label="투자상품">{SECTIONS.map(([id, label]) => <button key={id} type="button" role="tab" aria-selected={section === id} className={section === id ? "active" : ""} onClick={() => setSection(id)}>{label}</button>)}</div>
    {section === "options" && <div className="svc-two">
      <ActionCard kit={kit} name="옵션체인" title="옵션 시세표" description="거래할 수 있는 콜·풋 옵션입니다. 기초 주식을 고르면 그 종목만 봅니다." autoRun submit="시세 보기" />
      <ActionCard kit={kit} name="옵션작성" title="옵션 매도 (작성)" description="콜은 기초 주식을, 풋은 행사가만큼 현금을 담보로 맡기고 매도 호가를 냅니다. 만기까지 의무가 남습니다." submit="옵션 작성" />
    </div>}
    {section === "futures" && <ActionCard kit={kit} name="선물증거금" title="선물 증거금 추가" description="유지 증거금 아래로 떨어지면 청산될 수 있습니다. 여유 있게 넣어 두세요." submit="증거금 넣기" />}
    {section === "swap" && <div className="svc-two">
      <ActionCard kit={kit} name="스왑" title="금리스왑 체결" description="고정지급은 금리 상승에, 고정수취는 금리 하락에 대비합니다. 명목금액의 5%를 증거금으로 맡기고 매일 차액을 정산합니다." preset={{ action: "체결" }} only={["side", "notional", "days"]} submit="스왑 체결" />
      <ActionCard kit={kit} name="스왑" title="내 스왑" description="체결한 스왑과 평가 손익입니다. 해지하면 남은 기간의 금리 차이를 정산합니다." preset={{ action: "조회" }} only={[]} autoRun submit="새로고침" />
      <ActionCard kit={kit} name="스왑" title="스왑 해지" description="남은 기간의 금리 차이를 현재 시세로 정산하고 증거금을 돌려받습니다." preset={{ action: "해지" }} only={["swap"]} submit="해지하기" />
    </div>}
    {section === "mezzanine" && <div className="svc-two">
      <ActionCard kit={kit} name="전환청구" title="전환사채(CB) 주식 전환" description="액면 ÷ 전환가격만큼 신주를 받고 사채는 소멸합니다. 단주는 현금으로 받습니다." submit="전환 청구" />
      <ActionCard kit={kit} name="신주인수권행사" title="신주인수권(BW) 행사" description="사채 1매당 액면 ÷ 행사가격만큼 신주를 행사가로 삽니다. 사채는 그대로 남습니다." submit="권리 행사" />
    </div>}
    {section === "wrap" && <div className="svc-two">
      <ActionCard kit={kit} name="운용계좌" title="내가 운용하는 랩어카운트" autoRun submit="새로고침" />
      <ActionCard kit={kit} name="랩주문" title="랩 계좌로 주문" description="고객 랩 계좌의 현금과 보유자산으로만 사고팝니다. 계좌 통화로 결제하는 종목만 가능합니다." submit="주문 넣기" />
    </div>}
  </div>;
}
