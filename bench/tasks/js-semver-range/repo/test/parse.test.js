import { test } from "node:test";
import assert from "node:assert/strict";

import { compare, parse, rsort, sort, valid } from "../src/index.js";
import { parsePartial } from "../src/parse.js";

test("parse full versions", () => {
  const v = parse("1.2.3-beta.4+build.5");
  assert.equal(v.major, 1);
  assert.equal(v.minor, 2);
  assert.equal(v.patch, 3);
  assert.deepEqual(v.prerelease, ["beta", 4]);
  assert.deepEqual(v.build, ["build", 5]);
  assert.equal(valid("v2.0.0"), "2.0.0");
  assert.equal(valid("1.2"), null);
  assert.equal(valid("01.2.3"), null);
});

test("parse partial versions", () => {
  assert.deepEqual(parsePartial("1.x"), { major: 1, minor: null, patch: null, prerelease: [] });
  assert.deepEqual(parsePartial("*"), { major: null, minor: null, patch: null, prerelease: [] });
  assert.deepEqual(parsePartial("0.2"), { major: 0, minor: 2, patch: null, prerelease: [] });
  assert.throws(() => parsePartial("1.2.3.4"), TypeError);
});

test("precedence", () => {
  assert.equal(compare("1.0.0", "2.0.0"), -1);
  assert.equal(compare("1.10.0", "1.9.0"), 1);
  assert.equal(compare("1.0.0-alpha", "1.0.0"), -1);
  assert.equal(compare("1.0.0-alpha.1", "1.0.0-alpha.beta"), -1);
  assert.equal(compare("1.0.0-2", "1.0.0-10"), -1);
  assert.equal(compare("1.0.0+a", "1.0.0+b"), 0);
});

test("sorting", () => {
  const list = ["1.0.0", "0.9.1", "1.0.0-rc.1", "0.10.0"];
  assert.deepEqual(sort(list), ["0.9.1", "0.10.0", "1.0.0-rc.1", "1.0.0"]);
  assert.deepEqual(rsort(list), ["1.0.0", "1.0.0-rc.1", "0.10.0", "0.9.1"]);
});
