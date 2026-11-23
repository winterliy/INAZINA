import assert from "node:assert/strict";
import test from "node:test";
import { totalInHome } from "./exactTotal.ts";
import { moneyDivide } from "../../shared/money.ts";

const rate = "0.00050025"; // 1 SCD in NWX (with the liquidity multiplier)
const inHome = (nwx: string | number) => moneyDivide(nwx, rate, 2);

test("cash only: the total equals the cash, not the cash rounded down through NWX", () => {
  const portfolio = { cash: "1000000", cash_value_nwx: 500 }; // 1,000,000 SCD is 500.25 NWX; the server keeps 500
  assert.equal(inHome(500), "999500.25", "converting the whole total loses the fraction");
  assert.equal(Number(totalInHome(500, portfolio, inHome)), 1000000);
});

test("other holdings are still converted from NWX and added to the exact cash", () => {
  const portfolio = { cash: "1000000", cash_value_nwx: 500 };
  assert.equal(Number(totalInHome(500 + 1000, portfolio, inHome)), 1000000 + Number(inHome(1000)));
});

test("loans reduce it; an older server without cash_value_nwx falls back to the plain conversion", () => {
  assert.equal(Number(totalInHome(400, { cash: "1000000", cash_value_nwx: 500 }, inHome)), 1000000 + Number(inHome(-100)));
  assert.equal(totalInHome(500, { cash: "1000000" }, inHome), inHome(500));
  assert.equal(totalInHome(500, null, inHome), inHome(500));
});
