import { describe, expect, it } from "vitest";
import { abbrev, ago, duration, int, ms, num, pct } from "./format";

describe("int", () => {
  it("adds thousands separators and rounds", () => {
    expect(int(12840)).toBe("12,840");
    expect(int(3.6)).toBe("4");
  });
  it("renders em dash for nullish/NaN", () => {
    expect(int(null)).toBe("—");
    expect(int(NaN)).toBe("—");
  });
});

describe("num / pct", () => {
  it("formats with fixed decimals", () => {
    expect(num(3.14159, 2)).toBe("3.14");
    expect(num(10, 0)).toBe("10");
  });
  it("appends percent sign", () => {
    expect(pct(98.42)).toBe("98.4%");
  });
});

describe("abbrev", () => {
  it("abbreviates large numbers", () => {
    expect(abbrev(1500)).toBe("1.5K");
    expect(abbrev(2_300_000)).toBe("2.3M");
    expect(abbrev(950)).toBe("950");
  });
});

describe("duration", () => {
  it("formats h/m/s tiers", () => {
    expect(duration(9)).toBe("9s");
    expect(duration(129)).toBe("02m 09s");
    expect(duration(3729)).toBe("1h 02m 09s");
  });
  it("guards negatives and nullish", () => {
    expect(duration(-5)).toBe("—");
    expect(duration(undefined)).toBe("—");
  });
});

describe("ms", () => {
  it("appends unit", () => {
    expect(ms(42.5)).toBe("42.5 ms");
  });
});

describe("ago", () => {
  it("describes relative time", () => {
    const now = Date.UTC(2026, 5, 8, 12, 0, 0);
    expect(ago(new Date(now - 5000).toISOString(), now)).toBe("just now");
    expect(ago(new Date(now - 90_000).toISOString(), now)).toBe("1m ago");
    expect(ago(new Date(now - 7_200_000).toISOString(), now)).toBe("2h ago");
  });
  it("handles null", () => {
    expect(ago(null)).toBe("—");
  });
});
