import { test } from "node:test";
import assert from "node:assert/strict";

import { maxSatisfying, minSatisfying, satisfies, validRange } from "../src/index.js";

const yes = (v, r) => assert.ok(satisfies(v, r), `${v} should satisfy ${r}`);
const no = (v, r) => assert.ok(!satisfies(v, r), `${v} should not satisfy ${r}`);

test("primitive comparators", () => {
  yes("1.2.3", ">=1.2.3");
  no("1.2.2", ">=1.2.3");
  yes("1.2.3", "1.2.3");
  yes("1.9.0", ">1.2.3 <2.0.0");
  no("2.0.0", ">1.2.3 <2.0.0");
  yes("1.0.0", ">= 1.0.0");
});

test("unions", () => {
  yes("3.1.0", "^1.0.0 || ^3.0.0");
  no("2.1.0", "^1.0.0 || ^3.0.0");
});

test("x-ranges", () => {
  yes("1.2.9", "1.2.x");
  no("1.3.0", "1.2.x");
  yes("1.99.0", "1.x");
  no("2.0.0", "1");
  yes("0.0.1", "*");
  yes("5.0.0", "");
});

test("tilde ranges", () => {
  yes("1.2.9", "~1.2.3");
  no("1.3.0", "~1.2.3");
  no("1.2.2", "~1.2.3");
  yes("1.9.0", "~1");
  yes("0.2.5", "~0.2.3");
  no("0.3.0", "~0.2.3");
});

test("caret ranges, major >= 1", () => {
  yes("1.2.3", "^1.2.3");
  yes("1.9.9", "^1.2.3");
  no("2.0.0", "^1.2.3");
  no("1.2.2", "^1.2.3");
  yes("1.5.0", "^1.2");
  yes("1.0.0", "^1");
  no("2.0.0", "^1");
});

test("caret ranges, 0.x", () => {
  yes("0.2.3", "^0.2.3");
  yes("0.2.9", "^0.2.3");
  no("0.3.0", "^0.2.3");
  no("0.2.2", "^0.2.3");
  yes("0.9.0", "^0");
  no("1.0.0", "^0");
});

test("hyphen ranges", () => {
  yes("1.2.3", "1.2.3 - 2.3.4");
  yes("2.3.4", "1.2.3 - 2.3.4");
  no("2.3.5", "1.2.3 - 2.3.4");
  yes("2.3.9", "1.2 - 2.3");
  no("2.4.0", "1.2 - 2.3");
});

test("prereleases are excluded unless the range opts in", () => {
  no("1.3.0-beta.1", "^1.2.0");
  assert.ok(satisfies("1.3.0-beta.1", "^1.2.0", { includePrerelease: true }));
  yes("1.2.0-rc.2", ">=1.2.0-rc.1 <1.3.0");
  no("1.2.1-rc.2", ">=1.2.0-rc.1 <1.3.0");
});

test("invalid input", () => {
  assert.equal(satisfies("not.a.version", "^1.0.0"), false);
  assert.equal(satisfies("1.0.0", "^1.2.3.4"), false);
  assert.equal(validRange("^1.2.3"), ">=1.2.3 <2.0.0-0");
  assert.equal(validRange("~0.2"), ">=0.2.0 <0.3.0-0");
  assert.equal(validRange("nonsense"), null);
});

test("selecting versions", () => {
  const list = ["1.0.0", "1.2.3", "1.4.0", "2.0.0", "2.1.0-beta"];
  assert.equal(maxSatisfying(list, "^1.2.0"), "1.4.0");
  assert.equal(minSatisfying(list, "^1.2.0"), "1.2.3");
  assert.equal(maxSatisfying(list, ">=2"), "2.0.0");
  assert.equal(maxSatisfying(list, "^3"), null);
});
