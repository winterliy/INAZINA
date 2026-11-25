import assert from "node:assert/strict";
import test from "node:test";
import { netWorthInHome, totalInHome } from "./exactTotal.ts";
import { moneyDivide } from "../../shared/money.ts";

const rate = "0.00050025"; // 1 SCD in NWX (with the liquidity multiplier): 1,000,000 SCD is 500.25 NWX
const inHome = (nwx: string | number) => moneyDivide(nwx, rate, 2);
const n = (value: string | number) => Number(value);

// Cash only: the server keeps 500 of the 500.25 NWX; a loan of 100.5 SCD is 0.0503 NWX and is kept as 0.
const portfolio = { cash: "1000000", grand_floor_nwx: 500, grand_exact_home: "1000000", loan_floor_nwx: 0, loan_exact_home: "100.5", cash_value_nwx: 500 };

test("cash only: the total equals the cash, not the cash rounded down through NWX", () => {
  assert.equal(inHome(500), "999500.25", "converting the whole total loses the fraction");
  assert.equal(n(totalInHome(500, portfolio, inHome)), 1000000);
});

test("accounts, foreign accounts and loans are swapped for their exact home values too", () => {
  const many = { cash: "1000000", grand_floor_nwx: 500 + 30 + 20, grand_exact_home: "1060000.25", loan_floor_nwx: 7, loan_exact_home: "14000.5", cash_value_nwx: 500 };
  // NWX totals: 550 (floored cash, accounts, foreign accounts) + 1000 of shares; net also minus the loan 7
  assert.equal(n(totalInHome(550 + 1000, many, inHome)), 1060000.25 + n(inHome(1000)));
  assert.equal(n(netWorthInHome(550 + 1000 - 7, many, inHome)), 1060000.25 + n(inHome(1000)) - 14000.5);
});

test("a loan reduces the net worth by its exact home value", () => {
  assert.equal(n(netWorthInHome(500 - 0, portfolio, inHome)), 1000000 - 100.5);
});

test("an older server that sends only cash_value_nwx still gets the cash fixed; with nothing extra it is the plain conversion", () => {
  const old = { cash: "1000000", cash_value_nwx: 500 };
  assert.equal(n(totalInHome(500, old, inHome)), 1000000);
  assert.equal(n(netWorthInHome(500, old, inHome)), 1000000);
  assert.equal(totalInHome(500, { cash: "1000000" }, inHome), inHome(500));
  assert.equal(netWorthInHome(500, { cash: "1000000" }, inHome), inHome(500));
  assert.equal(totalInHome(500, null, inHome), inHome(500));
});
