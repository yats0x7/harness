// Parsing and normalising version strings.

const VERSION_RE =
  /^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$/;

export class SemVer {
  constructor(major, minor, patch, prerelease = [], build = []) {
    this.major = major;
    this.minor = minor;
    this.patch = patch;
    this.prerelease = prerelease;
    this.build = build;
  }

  toString() {
    let out = `${this.major}.${this.minor}.${this.patch}`;
    if (this.prerelease.length) out += `-${this.prerelease.join(".")}`;
    return out;
  }
}

function parseIdentifiers(text) {
  if (!text) return [];
  return text.split(".").map((id) => (/^(0|[1-9]\d*)$/.test(id) ? Number(id) : id));
}

/** Parse a full version string. Returns null when it is not valid semver. */
export function parse(input) {
  if (input instanceof SemVer) return input;
  if (typeof input !== "string") return null;
  const m = VERSION_RE.exec(input.trim());
  if (!m) return null;
  return new SemVer(Number(m[1]), Number(m[2]), Number(m[3]), parseIdentifiers(m[4]), parseIdentifiers(m[5]));
}

/** Like parse() but throws a TypeError for invalid input. */
export function parseStrict(input) {
  const v = parse(input);
  if (!v) throw new TypeError(`Invalid version: ${input}`);
  return v;
}

export function valid(input) {
  const v = parse(input);
  return v ? v.toString() : null;
}

const PARTIAL_RE = /^v?(\*|x|X|0|[1-9]\d*)(?:\.(\*|x|X|0|[1-9]\d*))?(?:\.(\*|x|X|0|[1-9]\d*))?(?:-([0-9A-Za-z-.]+))?(?:\+[0-9A-Za-z-.]+)?$/;

const isWild = (part) => part === undefined || part === "*" || part === "x" || part === "X";

/**
 * Parse a possibly partial version used inside ranges, e.g. "1", "1.2",
 * "1.x", "*". Missing or wildcard parts come back as null.
 */
export function parsePartial(input) {
  const m = PARTIAL_RE.exec(input.trim());
  if (!m) throw new TypeError(`Invalid version in range: ${input}`);
  const major = isWild(m[1]) ? null : Number(m[1]);
  const minor = major === null || isWild(m[2]) ? null : Number(m[2]);
  const patch = minor === null || isWild(m[3]) ? null : Number(m[3]);
  const prerelease = patch === null ? [] : parseIdentifiers(m[4]);
  return { major, minor, patch, prerelease };
}
