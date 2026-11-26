import type { Money } from "../../shared/money";

export type FundComponent = { assetCode: string; weight: number; assetClass: string };
export type Instrument = { code: string; name: string; type: string; price: Money; status: string; exchange_id?: string; fund_category?: string | null; components?: FundComponent[] };
export type Exchange = { exchange_id: string; status: string; reason: string | null; trading_halted: number; updated_epoch: number | null; resume_epoch: number | null; circuit_stage: number | null };
export type Overview = { activeUsers: number; activeInstruments: number; openOrders: number; market: Exchange[]; bankDeficits?: { issuer: string; account: string; cash: Money; nationality: string }[]; priceFloor?: { floor: number; actions: FloorAction[] } };
export type FloorAction = { code: string; name: string; from: number; to: number; epoch: number };
export type Audit = { auditId: number; actorId: string; action: string; payload: Record<string, unknown>; time: number };
export type Event = { event_id: number; asset_code: string | null; event_type: string; severity: number; starts_epoch: number; ends_epoch: number; created_by: string; target_mode: string | null; target_value: number | null; active: boolean };
export type Flow = { history_id: number; asset_code: string; anchor_price: Money; target_price: Money; change_percent: number; start_epoch: number; end_epoch: number; active: boolean };
