import assert from 'node:assert/strict';
import { test } from 'node:test';
import { exactMoney, formatMoney, moneyAdd, moneyCompare, moneyMultiply, minorMoney, validMoney, moneyDivideFloor, moneyDivide } from './money.ts';
test('scientific and decimal input retain every native and fractional unit', () => {
  assert.equal(exactMoney('1e18'), '1000000000000000000');
  assert.equal(exactMoney('1.000000000000000001e18'), '1000000000000000001');
  assert.equal(exactMoney('1.00000000000000000001e18'), '1000000000000000000.01');
  assert.equal(moneyAdd('1e18', '.01'), '1000000000000000000.01');
  assert.equal(moneyCompare('1000000000000000001', '1e18'), 1);
  assert.equal(minorMoney('100000000000000000001', 2), '1000000000000000000.01');
  assert.equal(moneyMultiply('1000000000000000000.01', 2), '2000000000000000000.02');
  assert.equal(moneyDivideFloor('2000000000000000000.02', 2, 2), '1000000000000000000.01');
});
test('display and JSON payload retain exact review and readback value', () => {
  const value = '1000000000000000000.01';
  assert.match(formatMoney(value), /100경/);
  assert.match(formatMoney(value), /1,000,000,000,000,000,000\.01/);
  assert.equal(JSON.parse(JSON.stringify({ amount: value })).amount, value);
  assert.equal(moneyAdd(value, '0.01', true), '1000000000000000000.00');
});
test('reject nonfinite, excessive scale/exponent and range', () => {
  for (const value of ['NaN', 'Infinity', '1e999', '1e25', '0.0000001', '']) assert.equal(validMoney(value), false, value);
  assert.equal(validMoney('1e24'), true);
  assert.equal(validMoney('-1', true), false);
});

test('home conversion, transfer remainder and betting chips preserve 100경 plus one', () => {
  const amount = '1000000000000000001';
  assert.equal(moneyDivide(amount, '1', 0), amount);
  assert.equal(moneyDivide('2000000000000000000.02', '2', 2), '1000000000000000000.01');
  assert.equal(moneyDivide('-1.005', '1', 2), '-1.01');
  assert.equal(moneyDivide('1', '.5', 2), '2.00');
  assert.equal(moneyAdd(amount, 1000), '1000000000000001001');
  assert.equal(moneyAdd(amount, '1000000000000000000', true), '1');
  assert.equal(moneyCompare(amount, '1000000000000000000'), 1);
  assert.equal(moneyMultiply(amount, 3), '3000000000000000003');
  assert.throws(() => moneyDivide(amount, 0));
});
