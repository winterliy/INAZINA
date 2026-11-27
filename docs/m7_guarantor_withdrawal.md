# 보증인 탈퇴 처리 (M7)

보증 기록(`loan_guarantees`)과 보증인 지급(`loan_guarantor_payments`), 회수 배분 부모 기록(`recovery_waterfall_events`)은 추가 전용이다. 데이터베이스 트리거가 UPDATE·DELETE를 거부하므로 탈퇴 때 사용자 ID를 임의 ID로 바꿀 수 없다.

## 선택
- **`rekey` 분류 + 미결 보증만 차단.** 세 표의 사용자 ID 열(`guarantor_user_id`, `user_id`)은 `COLUMN_BUCKETS`에서 `rekey`로 두었다. 추가 전용 트리거가 거부하면 기존 처리(`retained`)대로 그 행은 ID를 유지하고 탈퇴가 진행된다. 이름과 연결되는 정보는 없고 장부(누가 얼마를 갚았는지)가 남는 쪽이 회계상 맞다.
- `block` 버킷은 쓰지 않았다. `block`은 "행이 있는 동안 거부"라서 추가 전용 표에서는 한 번이라도 보증을 선 사람이 영영 탈퇴할 수 없게 된다.
- 대신 `BLOCKERS`에 "아직 청구될 수 있는 대출 보증" 항목을 넣었다: 보증 한도가 남아 있고(한도 > 지급 합계) 대출이 아직 정리되지 않은(`active`·`defaulted`·`charged_off`) 보증인은 탈퇴할 수 없고, 안내 문구가 사유를 보여 준다. 한도를 다 쓰거나 대출이 상환되면 막히지 않는다.
- 작성자 열(`created_by`, `actor_id`)은 운영자의 행위라 `staff`다.

## 시험
`tests/test_withdrawal.py`의 분류 시험은 이제 첫 사용 때 생기는 표(`guarantor_recovery`, `recovery_waterfall_posting`, `insolvency_manager`의 `ensure_tables`)를 먼저 만든 뒤 열을 검사하고, `ensure_tables(conn)`을 정의한 새 매니저가 목록에 없으면 실패한다. 시나리오 시험은 `tests/test_m7_guarantor_recovery.py::GuarantorWithdrawalTests`다.
