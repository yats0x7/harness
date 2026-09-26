// npm-style range parsing and matching.
import { compare } from "./compare.js";
import { parse, parsePartial } from "./parse.js";

const OPS = [">=", "<=", ">", "<", "="];

const fmt = (major, minor, patch, pre = []) =>
  `${major}.${minor}.${patch}${pre.length ? "-" + pre.join(".") : ""}`;

function comparator(op, version) {
  return { op, version };
}

function desugarXRange(op, p) {
  // "*", "x", "" -> any version
  if (p.major === null) {
    return op === "<" || op === ">" ? [comparator("<", "0.0.0-0")] : [];
  }
  if (p.minor === null) {
    if (op === "" || op === "=") return [comparator(">=", fmt(p.major, 0, 0)), comparator("<", fmt(p.major + 1, 0, 0, [0]))];
    if (op === ">") return [comparator(">=", fmt(p.major + 1, 0, 0))];
    if (op === "<=") return [comparator("<", fmt(p.major + 1, 0, 0, [0]))];
    if (op === ">=") return [comparator(">=", fmt(p.major, 0, 0))];
    return [comparator("<", fmt(p.major, 0, 0, [0]))];
  }
  if (p.patch === null) {
    if (op === "" || op === "=") return [comparator(">=", fmt(p.major, p.minor, 0)), comparator("<", fmt(p.major, p.minor + 1, 0, [0]))];
    if (op === ">") return [comparator(">=", fmt(p.major, p.minor + 1, 0))];
    if (op === "<=") return [comparator("<", fmt(p.major, p.minor + 1, 0, [0]))];
    if (op === ">=") return [comparator(">=", fmt(p.major, p.minor, 0))];
    return [comparator("<", fmt(p.major, p.minor, 0, [0]))];
  }
  return [comparator(op || "=", fmt(p.major, p.minor, p.patch, p.prerelease))];
}

function desugarTilde(p) {
  if (p.major === null) return [];
  const lower = fmt(p.major, p.minor ?? 0, p.patch ?? 0, p.prerelease);
  const upper = p.minor === null ? fmt(p.major + 1, 0, 0, [0]) : fmt(p.major, p.minor + 1, 0, [0]);
  return [comparator(">=", lower), comparator("<", upper)];
}

function desugarCaret(p) {
  if (p.major === null) return [];
  const lower = fmt(p.major, p.minor ?? 0, p.patch ?? 0, p.prerelease);
  let upper;
  if (p.minor === null || p.major !== 0) {
    // ^1.2.3 := >=1.2.3 <2.0.0
    upper = fmt(p.major + 1, 0, 0, [0]);
  } else {
    // ^0.2.3 := >=0.2.3 <0.3.0
    upper = fmt(0, p.minor + 1, 0, [0]);
  }
  return [comparator(">=", lower), comparator("<", upper)];
}

function parseComparatorToken(token) {
  if (token.startsWith("^")) return desugarCaret(parsePartial(token.slice(1)));
  if (token.startsWith("~")) return desugarTilde(parsePartial(token.slice(1).replace(/^>/, "")));
  const op = OPS.find((o) => token.startsWith(o)) ?? "";
  return desugarXRange(op, parsePartial(token.slice(op.length)));
}

function parseHyphen(from, to) {
  const lo = parsePartial(from);
  const hi = parsePartial(to);
  const out = [];
  if (lo.major !== null) out.push(comparator(">=", fmt(lo.major, lo.minor ?? 0, lo.patch ?? 0, lo.prerelease)));
  if (hi.major !== null) {
    if (hi.minor === null) out.push(comparator("<", fmt(hi.major + 1, 0, 0, [0])));
    else if (hi.patch === null) out.push(comparator("<", fmt(hi.major, hi.minor + 1, 0, [0])));
    else out.push(comparator("<=", fmt(hi.major, hi.minor, hi.patch, hi.prerelease)));
  }
  return out;
}

function parseComparatorSet(text) {
  const trimmed = text.trim().replace(/(>=|<=|>|<|=|~|\^)\s+/g, "$1");
  if (trimmed === "") return [];
  const hyphen = /^(\S+)\s+-\s+(\S+)$/.exec(trimmed);
  if (hyphen) return parseHyphen(hyphen[1], hyphen[2]);
  return trimmed.split(/\s+/).flatMap(parseComparatorToken);
}

export class Range {
  constructor(raw) {
    this.raw = raw;
    this.set = String(raw).split("||").map(parseComparatorSet);
  }

  toString() {
    return this.set
      .map((cs) => (cs.length ? cs.map((c) => c.op + c.version).join(" ") : "*"))
      .join(" || ");
  }

  test(version, { includePrerelease = false } = {}) {
    const v = parse(version);
    if (!v) return false;
    return this.set.some((cs) => testSet(cs, v, includePrerelease));
  }
}

function holds(c, v) {
  const r = compare(v, c.version);
  switch (c.op) {
    case ">": return r > 0;
    case ">=": return r >= 0;
    case "<": return r < 0;
    case "<=": return r <= 0;
    default: return r === 0;
  }
}

function testSet(comparators, v, includePrerelease) {
  if (!comparators.every((c) => holds(c, v))) return false;
  if (!v.prerelease.length || includePrerelease) return true;
  // A prerelease only matches if some comparator in the set explicitly names
  // a prerelease of the same major.minor.patch.
  return comparators.some((c) => {
    const cv = parse(c.version);
    return (
      cv.prerelease.length > 0 &&
      !(cv.prerelease.length === 1 && cv.prerelease[0] === 0) &&
      cv.major === v.major && cv.minor === v.minor && cv.patch === v.patch
    );
  });
}

export function satisfies(version, range, options) {
  try {
    return new Range(range).test(version, options);
  } catch (err) {
    if (err instanceof TypeError) return false;
    throw err;
  }
}

export function validRange(range) {
  try {
    return new Range(range).toString();
  } catch {
    return null;
  }
}
