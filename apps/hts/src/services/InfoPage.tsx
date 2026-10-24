import { ActionCard, type ServiceProps } from "./ServiceKit";

/** 정보 · 고객센터: lookups and requests to the administrators. */
export function InfoPage({ kit }: ServiceProps) {
  return <div className="svc-info">
    <h3 className="svc-section-title">금융 정보</h3>
    <div className="svc-two">
      <ActionCard kit={kit} name="재무" title="기업 재무제표" description="회사의 자산·부채·자본, 손익과 주요 비율입니다." submit="재무제표 보기" />
      <ActionCard kit={kit} name="법정최고이자" title="법정 최고이자율" description="대출 이율은 국가별 상한을 넘을 수 없습니다." submit="조회" />
    </div>
    <h3 className="svc-section-title">고객센터</h3>
    <a className="svc-help-link" href="https://dramatic-bread-b4a.notion.site/NAKWON-BOTS-3ea27b50c7bf80e0904ef783e0e5dfc4" target="_blank" rel="noreferrer"><b>📚 도움말 (/도움말)</b><small>봇·HTS 이용 안내와 정책 문서</small></a>
    <div className="svc-two">
      <ActionCard kit={kit} name="주총소집" title="주주총회 소집" description="최대주주만 소집할 수 있습니다. 주주들에게 Discord로 참석 여부를 묻습니다." submit="소집 요청" />
      <ActionCard kit={kit} name="파산" title="파산 신청" description="현재 자산 현황이 관리자에게 전달됩니다. 관리자 검토 후 처리됩니다." submit="파산 신청하기" />
    </div>
  </div>;
}
