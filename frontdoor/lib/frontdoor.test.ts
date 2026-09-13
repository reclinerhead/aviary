import { describe, expect, it } from "vitest";

import {
  buildTarget,
  describeAge,
  isPrivateIPv4,
  isValidSlug,
  parseRoster,
  stationForToken,
  tokensMatch,
} from "./frontdoor";

const TOKEN_A = "aaaaaaaaaaaaaaaaaaaaaaaa";
const TOKEN_B = "bbbbbbbbbbbbbbbbbbbbbbbb";

describe("isValidSlug", () => {
  it("accepts lowercase hostname-safe ids", () => {
    for (const s of ["birdhouse", "kestrel", "lakehouse", "station-3", "a1"]) {
      expect(isValidSlug(s)).toBe(true);
    }
  });
  it("rejects everything else", () => {
    for (const s of ["", "Birdhouse", "bird_house", "-lead", "a.b", "a/b", "..", "api ", 42, null]) {
      expect(isValidSlug(s)).toBe(false);
    }
  });
});

describe("isPrivateIPv4", () => {
  it("accepts the three RFC 1918 ranges", () => {
    for (const ip of ["10.0.0.1", "10.255.255.254", "172.16.0.1", "172.31.255.1", "192.168.1.47", "192.168.4.37"]) {
      expect(isPrivateIPv4(ip)).toBe(true);
    }
  });
  it("rejects link-local, the tailnet range, public, v6 and garbage", () => {
    for (const ip of [
      "169.254.7.7",     // link-local: a relative's browser can't route to it
      "100.92.248.83",   // tailnet
      "172.15.0.1",      // just outside 172.16/12
      "172.32.0.1",
      "8.8.8.8",
      "192.169.1.1",
      "192.168.01.5",    // leading zero, non-canonical
      "192.168.1.256",
      "::1",
      "fd00::1",
      "192.168.1",
      "localhost",
      "",
      undefined,
    ]) {
      expect(isPrivateIPv4(ip)).toBe(false);
    }
  });
});

describe("parseRoster", () => {
  it("parses id:token pairs and tolerates whitespace and a trailing comma", () => {
    const r = parseRoster(` birdhouse:${TOKEN_A} , kestrel:${TOKEN_B} ,`);
    expect([...r.entries()]).toEqual([
      ["birdhouse", TOKEN_A],
      ["kestrel", TOKEN_B],
    ]);
  });
  it("keeps colons inside the token", () => {
    const r = parseRoster(`birdhouse:abc:def:ghi:jkl:mno:pqr`);
    expect(r.get("birdhouse")).toBe("abc:def:ghi:jkl:mno:pqr");
  });
  it("fails loud on missing, empty, malformed, short, bad-id and duplicate entries", () => {
    expect(() => parseRoster(undefined)).toThrow(/required/);
    expect(() => parseRoster("   ")).toThrow(/required/);
    expect(() => parseRoster(",,")).toThrow(/no stations/);
    expect(() => parseRoster("birdhouse")).toThrow(/no token/);
    expect(() => parseRoster(`:${TOKEN_A}`)).toThrow(/no token/);
    expect(() => parseRoster("birdhouse:short")).toThrow(/too short/);
    expect(() => parseRoster(`Birdhouse:${TOKEN_A}`)).toThrow(/bad station id/);
    expect(() => parseRoster(`birdhouse:${TOKEN_A},birdhouse:${TOKEN_B}`)).toThrow(/duplicate/);
  });
});

describe("tokensMatch / stationForToken", () => {
  it("matches exact tokens only, regardless of length", () => {
    expect(tokensMatch(TOKEN_A, TOKEN_A)).toBe(true);
    expect(tokensMatch(TOKEN_A, TOKEN_B)).toBe(false);
    expect(tokensMatch(TOKEN_A, TOKEN_A + "x")).toBe(false);
    expect(tokensMatch("", TOKEN_A)).toBe(false);
  });
  it("resolves a token to its station and null for a stranger", () => {
    const r = parseRoster(`birdhouse:${TOKEN_A},kestrel:${TOKEN_B}`);
    expect(stationForToken(r, TOKEN_A)).toBe("birdhouse");
    expect(stationForToken(r, TOKEN_B)).toBe("kestrel");
    expect(stationForToken(r, "cccccccccccccccccccccccc")).toBeNull();
  });
});

describe("buildTarget", () => {
  it("lands on the porch with no path", () => {
    expect(buildTarget("192.168.1.47", undefined)).toBe("http://192.168.1.47/");
    expect(buildTarget("192.168.1.47", [])).toBe("http://192.168.1.47/");
  });
  it("passes subpaths through, with the query string", () => {
    expect(buildTarget("192.168.1.47", ["aviary"])).toBe("http://192.168.1.47/aviary");
    expect(buildTarget("192.168.1.47", ["weather", "history"], "?days=7")).toBe(
      "http://192.168.1.47/weather/history?days=7",
    );
  });
  it("encodes segments so nothing can escape the station origin", () => {
    expect(buildTarget("192.168.1.47", ["a b", "c?d"])).toBe("http://192.168.1.47/a%20b/c%3Fd");
    expect(buildTarget("192.168.1.47", ["evil.example:80"])).toBe("http://192.168.1.47/evil.example%3A80");
    expect(buildTarget("192.168.1.47", ["@evil.example"])).toBe("http://192.168.1.47/%40evil.example");
    // A slash inside a segment is refused, not encoded (see the dot-segment case below).
    expect(buildTarget("192.168.1.47", ["//evil.example"])).toBeNull();
  });
  it("refuses dot segments, empty segments, and non-private addresses", () => {
    expect(buildTarget("192.168.1.47", [".."])).toBeNull();
    expect(buildTarget("192.168.1.47", ["aviary", "."])).toBeNull();
    expect(buildTarget("192.168.1.47", [""])).toBeNull();
    expect(buildTarget("192.168.1.47", ["a/b"])).toBeNull();
    expect(buildTarget("8.8.8.8", ["aviary"])).toBeNull();
    expect(buildTarget("169.254.7.7", [])).toBeNull();
  });
  it("ignores a malformed query string", () => {
    expect(buildTarget("192.168.1.47", [], "?")).toBe("http://192.168.1.47/");
    expect(buildTarget("192.168.1.47", [], "days=7")).toBe("http://192.168.1.47/");
  });
});

describe("describeAge", () => {
  const now = new Date("2026-09-13T12:00:00Z");
  it("speaks in the largest sensible unit", () => {
    expect(describeAge("2026-09-13T11:59:59Z", now)).toBe("1 second ago");
    expect(describeAge("2026-09-13T11:59:20Z", now)).toBe("40 seconds ago");
    expect(describeAge("2026-09-13T11:45:00Z", now)).toBe("15 minutes ago");
    expect(describeAge("2026-09-13T09:00:00Z", now)).toBe("3 hours ago");
    expect(describeAge("2026-09-10T12:00:00Z", now)).toBe("3 days ago");
  });
  it("never goes negative and survives garbage", () => {
    expect(describeAge("2026-09-13T12:00:05Z", now)).toBe("0 seconds ago");
    expect(describeAge("not a date", now)).toBe("unknown");
  });
});
