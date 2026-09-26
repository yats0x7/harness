import { test } from "node:test";
import assert from "node:assert/strict";

import { maxSatisfying, satisfies, validRange } from "./src/index.js";

const yes = (v, r) => assert.ok(satisfies(v, r), `${v} should satisfy ${r}`);
const no = (v, r) => assert.ok(!satisfies(v, r), `${v} should not satisfy ${r}`);

test("^0.0.x only allows that exact patch", () => {
  yes("0.0.3", "^0.0.3");
  no("0.0.4", "^0.0.3");
  no("0.0.9", "^0.0.3");
  no("0.1.0", "^0.0.3");
  no("0.0.2", "^0.0.3");
});

test("^0.0 and ^0 keep their wider meaning", () => {
  yes("0.0.9", "^0.0");
  no("0.1.0", "^0.0");
  yes("0.5.0", "^0");
  no("1.0.0", "^0");
});

test("^0.x.y still allows patch updates", () => {
  yes("0.2.9", "^0.2.3");
  no("0.3.0", "^0.2.3");
});

test("desugared ranges", () => {
  assert.equal(validRange("^0.0.3"), ">=0.0.3 <0.0.4-0");
  assert.equal(validRange("^0.0"), ">=0.0.0 <0.1.0-0");
  assert.equal(validRange("^0.2.3"), ">=0.2.3 <0.3.0-0");
  assert.equal(validRange("^1.2.3"), ">=1.2.3 <2.0.0-0");
});

test("resolving a 0.0.x dependency picks the pinned patch", () => {
  const published = ["0.0.1", "0.0.3", "0.0.4", "0.0.12", "0.1.0"];
  assert.equal(maxSatisfying(published, "^0.0.3"), "0.0.3");
  assert.equal(maxSatisfying(published, "^0.0.3 || ^0.1.0"), "0.1.0");
});

test("prerelease of the pinned patch", () => {
  yes("0.0.3-rc.2", "^0.0.3-rc.1");
  no("0.0.4-rc.1", "^0.0.3-rc.1");
});
