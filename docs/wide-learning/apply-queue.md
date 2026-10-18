# 시스템 적용 대기열

정리된 지식을 LAPLACE에 반영할 후보입니다. 기준은 [REVIEW.md](REVIEW.md) 4절입니다. CLI는 `status: proposed`로만 올리고, **사용자가 `approved`로 바꾼 항목만** 구현합니다. 구현 후 `applied`로 바꾸고 커밋을 적습니다.

형식:

```markdown
### [A-001] 신용점수 연체 감점에 단기연체 유예 도입
- basis: C-009; G-012
- laplace: 4
- change: 납기 후 N일 미만 연체는 payment_missed로 기록하지 않는다.
- reuse: managers/credit_score_manager.py, credit_score_policy
- impact: 정기납부 연체 사용자 전체. 신규 연체부터 적용, 과거 이벤트 불변
- rollback: 정책값을 0으로 되돌림
- tests: tests/test_credit_score.py에 유예기간 경계 테스트 추가
- status: proposed
- commit: -
```

`status`: `proposed`, `approved`, `applied`, `rejected`. `basis` 항목은 모두 최종 상태가 `verified` 또는 `simulator-assumption`이어야 합니다(검사기가 확인).

---

(아직 후보가 없습니다.)
