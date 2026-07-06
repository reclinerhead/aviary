import { describe, expect, it } from "vitest";
import { busUrl, parseLine, pickVoice, statusTopicId } from "./bus";

describe("busUrl", () => {
  it("targets port 9001 on the page's host", () => {
    expect(busUrl("192.168.1.50")).toBe("ws://192.168.1.50:9001");
  });
  it("pins localhost to IPv4 (Windows resolves it to ::1, which the WS can't use)", () => {
    expect(busUrl("localhost")).toBe("ws://127.0.0.1:9001");
  });
  it("pins an empty hostname to IPv4 loopback too", () => {
    expect(busUrl("")).toBe("ws://127.0.0.1:9001");
  });
  it("lets the env override win entirely", () => {
    expect(busUrl("myhost", "wss://elsewhere:9002")).toBe("wss://elsewhere:9002");
  });
});

describe("parseLine", () => {
  it("accepts a full narration payload", () => {
    const line = parseLine(
      JSON.stringify({
        ts: "2026-07-06T10:00:00",
        narrator: "Marlin",
        voice: "David",
        text: "A chipmunk just came in.",
        event_kind: "arrival",
      }),
    );
    expect(line?.narrator).toBe("Marlin");
    expect(line?.text).toBe("A chipmunk just came in.");
  });
  it("fills defaults for missing optional fields", () => {
    const line = parseLine(JSON.stringify({ text: "hi" }));
    expect(line).toEqual({
      ts: "",
      narrator: "unknown",
      voice: "",
      text: "hi",
      event_kind: "",
    });
  });
  it("rejects payloads without text", () => {
    expect(parseLine(JSON.stringify({ narrator: "Marlin" }))).toBeNull();
    expect(parseLine(JSON.stringify({ text: "" }))).toBeNull();
  });
  it("rejects non-JSON garbage", () => {
    expect(parseLine("not json")).toBeNull();
  });
});

describe("statusTopicId", () => {
  it("extracts the narrator id", () => {
    expect(statusTopicId("narrators/marlin/status")).toBe("marlin");
  });
  it("ignores unrelated topics", () => {
    expect(statusTopicId("narration/lines")).toBeNull();
    expect(statusTopicId("narrators/marlin/mood")).toBeNull();
    expect(statusTopicId("narrators/a/b/status")).toBeNull();
  });
});

describe("pickVoice", () => {
  const voices = [{ name: "Microsoft David - English (United States)" }, { name: "Microsoft Zira" }];
  it("matches the hint as a case-insensitive substring", () => {
    expect(pickVoice(voices, "david")?.name).toContain("David");
  });
  it("returns null when nothing matches or the hint is empty", () => {
    expect(pickVoice(voices, "Attenborough")).toBeNull();
    expect(pickVoice(voices, "")).toBeNull();
  });
});
