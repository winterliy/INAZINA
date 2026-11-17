# LAPLACE 내부 통제-증빙 추적표

내부 금융 시뮬레이터의 통제가 실제로 어떤 코드와 테스트로 뒷받침되는지 한 줄씩 대응시킨다. 실제 규제 인증이나 외부 감사 증빙이 아니며, 심사 준비용 구조만 정리한다. 표의 각 행은 `tests/test_control_evidence_matrix.py`가 검증한다. 구현 위치 파일과 증빙 테스트가 실제로 존재하지 않으면 그 테스트가 실패한다.

증빙 칸은 `파일::테스트이름` 형식이고 여러 개면 쉼표로 구분한다.

| ID | 통제 | 구현 위치 | 증빙 |
|---|---|---|---|
| C-01 | 불균형·분개선 없는 공통 분개는 통합 대사에서 실패로 드러난다 | managers/accounting_journal.py | tests/test_financial_close_control.py::test_unbalanced_and_lineless_entries_in_any_scope_fail_the_close |
| C-02 | 무결성 문제가 있는 원장 범위는 기간 마감을 할 수 없다 | managers/accounting_journal.py | tests/test_financial_close_control.py::test_period_close_is_refused_while_the_scope_has_integrity_findings |
| C-03 | 등록된 금액 표는 모두 분류되고, 미연결 표가 남으면 마감 준비가 되지 않는다 | managers/accounting_journal.py | tests/test_financial_close_control.py::test_every_registered_amount_table_is_classified_and_unconnected_ones_block_close, tests/test_financial_close_control.py::test_a_new_registered_amount_table_must_be_classified |
| C-04 | 환산 지급은 환율과 양쪽 금액을 수정 불가 기록으로 남긴다 | database/currency_db.py | tests/test_treasury_bond_fx_settlement.py::test_fx_settlement_rows_are_append_only, tests/test_game_funding.py::test_lottery_fx_settlements_are_append_only |
| C-05 | 지급 도중 실패하면 현금·세금·이벤트·분개가 함께 되돌려진다 | managers/treasury_bond_manager.py | tests/test_treasury_bond_fx_settlement.py::test_failed_fx_posting_rolls_back_the_whole_redemption |
| C-06 | 같은 지급을 다시 실행해도 두 번 지급하거나 환산하지 않는다 | managers/treasury_bond_manager.py | tests/test_treasury_bond_fx_settlement.py::test_rerun_does_not_pay_or_convert_twice |
| C-07 | 업무 기록은 자기가 움직인 지갑 행에만 연결되고 연결은 수정할 수 없다 | managers/accounting_journal.py | tests/test_business_cash_links.py::test_link_takes_exactly_the_wallet_rows_made_since_the_mark, tests/test_business_cash_links.py::test_links_and_events_are_append_only |
| C-08 | 금융기관 도매조달은 한도 안에서만, 담보 조건을 채워야 실행된다 | managers/funding_manager.py | tests/test_institution_funding.py::test_limit_is_required_and_enforced_per_source_and_currency, tests/test_institution_funding.py::test_secured_sources_need_collateral_after_haircut |
| C-09 | 도매조달 잔액은 기관 분개의 조달부채와 대사된다 | managers/bank_journal.py | tests/test_institution_funding.py::test_reconciliation_matches_and_detects_a_missing_or_tampered_row |
| C-10 | 민감한 관리자 조회와 기간 마감은 행위자와 함께 감사 기록에 남는다 | web_api/server.py | tests/test_web_api.py::test_sensitive_admin_report_access_is_audited, tests/test_web_api.py::test_bank_journal_route_records_actor_and_filters, tests/test_web_api.py::test_admin_accounting_period_close_is_authorized_and_audited |
| C-11 | KYC 미검증 고객은 송금·계좌 개설·대출 신청이 차단된다 | managers/compliance_manager.py | tests/test_currency_home.py::test_explicit_kyc_review_gates_outgoing_transfers_but_not_receiving, tests/test_currency_account_storage.py::test_pending_kyc_blocks_regular_and_foreign_account_opening, tests/test_realistic_rate_system.py::test_kyc_profile_gates_loan_origination |
| C-12 | AML 경보는 송금인·수취인 각자의 통화 기준으로 평가한다 | managers/compliance_manager.py | tests/test_currency_home.py::test_aml_monitors_sender_and_recipient_in_their_home_currencies |
| C-13 | 운영 DB 백업은 일관된 사본을 만들고 보존 개수를 지킨다 | database/backup.py | tests/test_backup.py::test_backup_copies_and_prunes |
| C-14 | 테스트는 운영 DB를 열지 않는다 | tests/conftest.py | tests/test_control_evidence_matrix.py::test_default_database_directory_is_quarantined_during_tests |

## 범위 밖

실명 자료·신분증 원본 수집, 외부 제재목록 연동, 실제 금융기관·세무기관 연결, 규제 인증 취득은 다루지 않는다. 비밀관리 점검, 접근 권한 주기 검토, 사고·복구 훈련 기록은 아직 이 표에 없다.
