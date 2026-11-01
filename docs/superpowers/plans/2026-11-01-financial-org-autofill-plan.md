# 재무 자동 채움 및 기업구조 개선 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 재무제표 추정 자동 채움, 기업집단·규모 설정 개선, 의결권 기준 모회사 표시, 조직도 포커스 이동 및 관련 조회 성능을 개선한다.

**Architecture:** 재무 추정치는 기존 원장에 월별 멱등 분개로 기록하고 journal API/UI에서 추정으로 구별한다. 기업 규모·집단·모회사 개선은 현재 조직 API와 관리자 UI의 계약을 확장하며, 그래프 포커스는 이미 받은 데이터와 DOM 위치만 사용한다. 회사 및 지분 계산은 요청 범위에서 DB 조회를 묶어 반복 쿼리를 줄인다.

**Tech Stack:** Python, SQLite compatibility layer, React, TypeScript, existing organization/financial managers and API.

**Spec:** `docs/superpowers/specs/2026-11-01-financial-org-autofill-design.md`

## Global Constraints

- 추정치는 실제 거래 또는 실제 공시 정보로 표현하지 않는다.
- 추정값으로 기존 분개를 덮어쓰거나 같은 달에 중복 생성하지 않는다.
- 기본 규모 기준은 제품 기준이며, 직원 원천 데이터가 없을 때 직원 기준은 비활성이다.
- 모회사 표시는 한 회사가 직접 의결권 과반을 보유하는 경우에만 한다.
- 현재 지분 경로에서 선택한 법인은 그래프 API를 다시 호출하지 않고 포커스한다.
- 최적화는 이 요청이 닿는 재무·법인 요약·소유 그래프 경로로 한정한다.
- 이 작업에서는 자동 테스트를 추가하거나 실행하지 않는다. 변경 후 diff와 구문/타입 검토만 수행한다.

## Review Focus

- 기존 규모 규칙·공시 기간이 기본값 갱신 중 덮이지 않는지: 조직 설정의 비어 있지 않은 값 보존을 코드 경로에서 확인한다.
- 이미 실제 계정 기록이 있는 월에 추정치가 덮거나 중복하지 않는지: idempotency key와 계정별 실제 라인 제외를 확인한다.
- 추정 분개 역분개가 현금 원장을 잘못 변경하지 않는지: 원본 분개와 동일한 비현금 계정 및 흐름 보존을 확인한다.
- treasury 및 자기주식이 의결권 분모·후보에서 제외되는지: 보유 집계 필터와 발행종류별 의결권 산식을 확인한다.
- 그래프 포커스가 같은 노드로 이동하고 새 API 요청을 만들지 않는지: 트리 선택 이벤트와 scroll container 로직을 확인한다.

---

### Task 1: 추정 재무제표 자동 채움

**Files:** `managers/financial_manager.py`, `web_api/server.py`, `apps/dashboard/src/CompanyLedger.tsx`

**Interfaces:** `FinancialManager`의 관리자 전용 현재월 자동 채움 메서드가 추정 원장 entry id와 적용 설명을 반환한다. 기존 `/api/admin/company/...` 재무 명령 API와 journal 응답 확장 방식을 따른다.

- [x] 현재월 한정, 마감 차단, 프로필 우선, 계정별 실제 기록 제외, 한도 검증 및 멱등 키를 구현한다.
- [x] 설계서 산업별 추정 계수·실제 이력 우선순위·비현금 분개·20% 추정 세율을 적용하고 근거를 저장한다.
- [x] 기존 관리자 회계 API에 자동 채움 동작을 연결하고 journal에서 `estimated`와 설명을 반환한다.
- [x] 재무 수정 화면에 “이번 달 추정 채우기”, 추정 표시와 추정 가정 안내를 추가한다.

### Task 2: 규모 기본값 및 기업집단 대표 지정

**Files:** `database/organization_db.py`, `managers/organization_manager.py`, `web_api/organizations.py`, `apps/dashboard/src/OrganizationPage.tsx`

**Interfaces:** 기본 규칙은 `mode=any`, 공시 604800초, NWX 매출·자산 100억/1000억 상한, 종업원 300/1000명이다. 집단 대표 지정 API는 활성 구성원인 법인 코드 또는 null을 받는다.

- [x] 비어 있는 규모 규칙에만 제품 기본값을 채우고 설정 화면에도 같은 기본값을 표시한다.
- [x] 대표기업 선택 시 생성 단계에서 구성원으로 추가하고, 기존 집단에서는 활성 구성원 간 대표를 변경/해제하도록 API 검증을 추가한다.
- [x] 화면의 대표기업 선택 UI 및 성공/오류 상태를 연결한다.

### Task 3: 의결권 기준 모회사 및 회사 요약 조회

**Files:** `managers/company_account_manager.py`, `apps/dashboard/src/CompaniesPage.tsx`

**Interfaces:** 회사 요약의 `parents`는 0개 또는 직접 의결권 과반을 가진 회사 한 곳만 담는다. 기존 화면 타입을 유지해 불필요한 응답 파손을 피한다.

- [x] 모든 발행종류의 주당 의결권과 회사별 보유량을 배치 조회해 회사별 총 의결권을 계산한다.
- [x] 거래소/국가 국고 및 발행 회사 자기 보유량을 제외하고 단일 법인의 50% 초과 지배만 반환한다.
- [x] 모회사·자회사 표시가 같은 단일 지배 결과를 사용하도록 요약 화면을 정리한다.

### Task 4: 지분 경로에서 그래프 노드 포커스

**Files:** `apps/dashboard/src/OrganizationOwnershipWindow.tsx`, `apps/shared/OwnershipGraph.tsx`, `apps/shared/OwnershipGraph.css`

**Interfaces:** `OwnershipGraph`에 선택적 `focusedNodeId?: string`을 전달한다. 현재 그래프 트리의 코드 선택은 API 호출 대신 focus state만 바꾸며, 전체 법인 검색 선택은 기존 그래프 로드 동작을 유지한다.

- [x] 소유 경로 트리 선택을 그래프 노드 id로 연결한다.
- [x] 해당 DOM 노드를 scroll container 중앙에 놓고 짧게 강조한다. 페이지 자체의 스크롤 위치는 변경하지 않는다.
- [x] `#ownership/organization/`의 끝 슬래시를 정규화하고 법인 미지정 진입 시 첫 법인을 불러온다.

### Task 5: 조직 목록 및 소유 그래프 DB 조회 최적화

**Files:** `web_api/organizations.py`, `managers/organization_manager.py`, `managers/company_account_manager.py`

**Interfaces:** 외부 API 응답은 유지하고, 요청 단위 배치 조회/caches만 도입한다.

- [x] 조직 목록 재무 매출·자산과 발행종류 조회를 회사 묶음 조회로 바꾼다.
- [x] 소유 그래프의 법인·발행종류·보유 수량 자료를 그래프 요청 시작 시 모아 edge 확장에서 재사용한다.
- [x] diff를 검토하고 `git diff --check`를 실행한다. 테스트 명령은 실행하지 않았다.
