import { describe, expect, it } from "vitest";
import {
  NARRATION_JOURNAL_WILDCARD,
  audioEventKey,
  busUrl,
  journalTopicId,
  mergeJournals,
  parseAudioEvent,
  parseJournal,
  parseLine,
  pickVoice,
  statusTopicId,
  toJournalEntries,
  voiceColor,
} from "./bus";

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
  it("carries the event's frame_id when present (issue #90)", () => {
    const line = parseLine(
      JSON.stringify({ text: "hi", frame_id: "20260714_x_arrival_0001" }),
    );
    expect(line?.frame_id).toBe("20260714_x_arrival_0001");
  });
  it("leaves the frame_id key ABSENT when the wire had none", () => {
    // The degradation convention: old journal files and template-tier lines
    // parse to exactly the pre-#90 shape -- no empty-string invention.
    const line = parseLine(JSON.stringify({ text: "hi" }));
    expect(line && "frame_id" in line).toBe(false);
    expect(parseLine(JSON.stringify({ text: "hi", frame_id: "" }))?.frame_id)
      .toBeUndefined();
    expect(parseLine(JSON.stringify({ text: "hi", frame_id: 7 }))?.frame_id)
      .toBeUndefined();
  });
});

describe("parseJournal", () => {
  const line = (i: number) => ({
    ts: `2026-07-06T10:00:0${i}`,
    narrator: "Marlin",
    voice: "David",
    text: `line ${i}`,
    event_kind: "arrival",
  });

  it("accepts a window and keeps its order", () => {
    const lines = parseJournal(JSON.stringify({ lines: [line(0), line(1)] }));
    expect(lines?.map((l) => l.text)).toEqual(["line 0", "line 1"]);
  });
  it("drops bad lines without discarding the window", () => {
    const lines = parseJournal(
      JSON.stringify({ lines: [line(0), { text: "" }, "junk", line(1)] }),
    );
    expect(lines?.map((l) => l.text)).toEqual(["line 0", "line 1"]);
  });
  it("accepts an empty window (a fresh narrator with nothing filed)", () => {
    expect(parseJournal(JSON.stringify({ lines: [] }))).toEqual([]);
  });
  it("keeps per-line frame_ids and tolerates lines without one (issue #90)", () => {
    const lines = parseJournal(
      JSON.stringify({
        lines: [{ ...line(0), frame_id: "fid_0" }, line(1)],
      }),
    );
    expect(lines?.[0].frame_id).toBe("fid_0");
    expect(lines && "frame_id" in lines[1]).toBe(false);
  });
  it("rejects payloads that aren't a window", () => {
    expect(parseJournal(JSON.stringify({ lines: "nope" }))).toBeNull();
    expect(parseJournal(JSON.stringify(line(0)))).toBeNull();
    expect(parseJournal("not json")).toBeNull();
  });
});

describe("toJournalEntries", () => {
  const line = (ts: string, text: string) => ({
    ts,
    narrator: "Marlin",
    voice: "",
    text,
    event_kind: "arrival",
  });

  it("flips oldest-first wire order to newest-first display order", () => {
    const entries = toJournalEntries([
      line("2026-07-06T10:00:00", "first"),
      line("2026-07-06T10:00:05", "second"),
    ]);
    expect(entries.map((e) => e.text)).toEqual(["second", "first"]);
  });
  it("derives stable keys from content, so a republished window keeps them", () => {
    const window = [
      line("2026-07-06T10:00:00", "first"),
      line("2026-07-06T10:00:05", "second"),
    ];
    const before = toJournalEntries(window);
    const after = toJournalEntries([
      ...window,
      line("2026-07-06T10:00:09", "third"),
    ]);
    expect(after.slice(1).map((e) => e.key)).toEqual(before.map((e) => e.key));
  });
  it("keeps keys unique when the same line files twice in one second", () => {
    const twin = line("2026-07-06T10:00:00", "again");
    const entries = toJournalEntries([twin, twin]);
    expect(new Set(entries.map((e) => e.key)).size).toBe(2);
  });
  it("carries frame_id through to the entry (issue #90)", () => {
    const entries = toJournalEntries([
      { ...line("2026-07-06T10:00:00", "look at him"), frame_id: "fid_1" },
    ]);
    expect(entries[0].frame_id).toBe("fid_1");
  });
});

describe("journalTopicId", () => {
  it("extracts the narrator id from a per-narrator journal topic", () => {
    expect(journalTopicId("narration/journal/marlin")).toBe("marlin");
    expect(journalTopicId("narration/journal/jim")).toBe("jim");
  });
  it("rejects the retired bare topic (a stale pre-#80 retained blob)", () => {
    expect(journalTopicId("narration/journal")).toBeNull();
  });
  it("ignores unrelated topics and deeper paths", () => {
    expect(journalTopicId("narration/lines")).toBeNull();
    expect(journalTopicId("narration/journal/a/b")).toBeNull();
  });
  it("matches the subscribed wildcard shape", () => {
    expect(NARRATION_JOURNAL_WILDCARD).toBe("narration/journal/+");
  });
});

describe("mergeJournals", () => {
  const line = (narrator: string, ts: string, text: string) => ({
    ts,
    narrator,
    voice: "",
    text,
    event_kind: "arrival",
  });

  it("interleaves windows chronologically, oldest first", () => {
    const merged = mergeJournals(
      {
        marlin: [
          line("Marlin", "2026-07-13T10:00:00", "m1"),
          line("Marlin", "2026-07-13T10:02:00", "m2"),
        ],
        jim: [line("Jim", "2026-07-13T10:01:00", "j1")],
      },
      50,
    );
    expect(merged.map((l) => l.text)).toEqual(["m1", "j1", "m2"]);
  });
  it("caps at the limit keeping the newest lines", () => {
    const merged = mergeJournals(
      {
        marlin: [
          line("Marlin", "2026-07-13T10:00:00", "old"),
          line("Marlin", "2026-07-13T10:02:00", "kept"),
        ],
        jim: [line("Jim", "2026-07-13T10:01:00", "also kept")],
      },
      2,
    );
    expect(merged.map((l) => l.text)).toEqual(["also kept", "kept"]);
  });
  it("keeps within-window order for same-second lines (stable sort)", () => {
    const ts = "2026-07-13T10:00:00";
    const merged = mergeJournals(
      { marlin: [line("Marlin", ts, "first"), line("Marlin", ts, "second")] },
      50,
    );
    expect(merged.map((l) => l.text)).toEqual(["first", "second"]);
  });
  it("passes a single window through unchanged and survives none at all", () => {
    const window = [line("Marlin", "2026-07-13T10:00:00", "solo")];
    expect(mergeJournals({ marlin: window }, 50)).toEqual(window);
    expect(mergeJournals({}, 50)).toEqual([]);
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

describe("voiceColor", () => {
  it("art-directs the named cast: warm host, khaki field man", () => {
    expect(voiceColor("Marlin")).toBe("var(--squirrel)");
    expect(voiceColor("Jim")).toBe("var(--turkey)");
  });
  it("keeps the cast visually distinct", () => {
    expect(voiceColor("Marlin")).not.toBe(voiceColor("Jim"));
  });
  it("gives a guest voice a stable color from the palette", () => {
    const guest = voiceColor("Rover");
    expect(guest).toBe(voiceColor("Rover")); // deterministic
    expect(guest).toMatch(/^var\(--(squirrel|turkey|chipmunk|led)\)$/);
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

describe("parseAudioEvent", () => {
  const detection = {
    ts: 1752861234,
    source: "amcrest",
    kind: "detection",
    species_sci: "Cardinalis cardinalis",
    species_common: "Northern Cardinal",
    confidence: 0.87,
    window_s: 3,
    clip: "amcrest/1752861234-Northern_Cardinal.wav",
    wind_suspect: false,
    rms: 0.013,
  };
  it("accepts a full detection payload", () => {
    expect(parseAudioEvent(JSON.stringify(detection))).toEqual({
      ts: 1752861234,
      source: "amcrest",
      kind: "detection",
      species_sci: "Cardinalis cardinalis",
      species_common: "Northern Cardinal",
      confidence: 0.87,
      clip: "amcrest/1752861234-Northern_Cardinal.wav",
      wind_suspect: false,
      rms: 0.013,
    });
  });
  it("accepts a sound event (#174) with its coarse class", () => {
    const e = parseAudioEvent(
      JSON.stringify({
        ts: 1752861234,
        source: "rover",
        kind: "sound",
        class: "Dog",
        confidence: 0.42,
        window_s: 3,
        clip: "rover/1752861234-Dog.wav",
        wind_suspect: true,
        rms: 0.2,
      }),
    );
    expect(e).toMatchObject({ kind: "sound", class: "Dog", wind_suspect: true });
  });
  it("keeps a failed clip write and a pre-#175 payload honest (null clip/rms)", () => {
    const e = parseAudioEvent(
      JSON.stringify({ ...detection, clip: null, rms: undefined }),
    );
    expect(e?.clip).toBeNull();
    expect(e?.rms).toBeNull();
  });
  it("parses an unknown kind to null -- the ignore-unknown-kinds guard, client-side", () => {
    expect(
      parseAudioEvent(JSON.stringify({ ...detection, kind: "seismic" })),
    ).toBeNull();
  });
  it("rejects a detection missing its species fields", () => {
    expect(
      parseAudioEvent(JSON.stringify({ ...detection, species_sci: "" })),
    ).toBeNull();
  });
  it("rejects malformed JSON and non-object payloads", () => {
    expect(parseAudioEvent("{nope")).toBeNull();
    expect(parseAudioEvent("42")).toBeNull();
    expect(parseAudioEvent("null")).toBeNull();
  });
});

describe("audioEventKey", () => {
  const base = parseAudioEvent(
    JSON.stringify({
      ts: 1000,
      source: "amcrest",
      kind: "detection",
      species_sci: "A sci",
      species_common: "A",
      confidence: 0.9,
    }),
  )!;
  it("differs for two species sharing one window", () => {
    const other = { ...base, kind: "detection" as const, species_sci: "B sci" };
    expect(audioEventKey(base)).not.toBe(audioEventKey(other));
  });
  it("differs for a detection and a sound sharing one window", () => {
    const sound = parseAudioEvent(
      JSON.stringify({
        ts: 1000,
        source: "amcrest",
        kind: "sound",
        class: "Dog",
        confidence: 0.4,
      }),
    )!;
    expect(audioEventKey(base)).not.toBe(audioEventKey(sound));
  });
  it("is stable across hydration and the live topic (same content, same key)", () => {
    expect(audioEventKey(base)).toBe(audioEventKey({ ...base }));
  });
});
