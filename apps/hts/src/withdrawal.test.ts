import assert from "node:assert/strict";
import test from "node:test";
import { canRequestWithdrawal } from "./withdrawal.ts";

test("the withdrawal request is offered to an ordinary member only, not to other account kinds or a delegated session", () => {
  assert.equal(canRequestWithdrawal({ role: "user" }), true);
  for (const role of ["admin", "nation", "company"]) assert.equal(canRequestWithdrawal({ role }), false, role);
  assert.equal(canRequestWithdrawal({ role: "user", delegatedBy: "admin-1" }), false, "an administrator signed in as the member");
  assert.equal(canRequestWithdrawal({ role: "user", delegatedBy: "" }), true);
});
