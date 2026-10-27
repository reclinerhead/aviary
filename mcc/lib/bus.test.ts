import { describe, expect, it } from "vitest";
import {
  SOURCE_CAPTURING_S,
  audioEventKey,
  busUrl,
  parseAudioEvent,
  parseAudioSources,
  sourceListenLabel,
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

describe("voiceColor", () => {
  it("art-directs the named cast: warm host, khaki field man, sky weather man", () => {
    expect(voiceColor("Marlin")).toBe("var(--squirrel)");
    expect(voiceColor("Jim")).toBe("var(--turkey)");
    // Willard (issue #92): muted sky, not rain/precip and not a species accent.
    expect(voiceColor("Willard")).toBe("var(--sky)");
  });
  it("keeps the cast visually distinct", () => {
    expect(voiceColor("Marlin")).not.toBe(voiceColor("Jim"));
    expect(voiceColor("Willard")).not.toBe(voiceColor("Marlin"));
    expect(voiceColor("Willard")).not.toBe(voiceColor("Jim"));
  });
  it("gives a guest voice a stable color from the palette", () => {
    const guest = voiceColor("Rover");
    expect(guest).toBe(voiceColor("Rover")); // deterministic
    expect(guest).toMatch(/^var\(--(squirrel|turkey|chipmunk|led)\)$/);
  });
});

describe("parseAudioSources", () => {
  const payload = {
    ts: 1752861234,
    sources: {
      "house-rear": { state: "online", last_window_ts: 1752861200 },
      "house-front": { state: "silent", last_window_ts: 1752861000 },
      rover: { state: "offline", last_window_ts: null },
    },
  };
  it("parses every source and sorts by name for a stable masthead line", () => {
    expect(parseAudioSources(JSON.stringify(payload))).toEqual([
      { name: "house-front", state: "silent", last_window_ts: 1752861000 },
      { name: "house-rear", state: "online", last_window_ts: 1752861200 },
      { name: "rover", state: "offline", last_window_ts: null },
    ]);
  });
  it("tolerates a missing last_window_ts as null", () => {
    const one = parseAudioSources(
      JSON.stringify({
        ts: 1,
        sources: { "house-rear": { state: "starting" } },
      }),
    );
    expect(one).toEqual([
      { name: "house-rear", state: "starting", last_window_ts: null },
    ]);
  });
  it("rejects malformed payloads rather than inventing a roster", () => {
    expect(parseAudioSources("{nope")).toBeNull();
    expect(parseAudioSources("42")).toBeNull();
    expect(parseAudioSources(JSON.stringify({ ts: 1 }))).toBeNull();
    expect(
      parseAudioSources(JSON.stringify({ sources: ["house-rear"] })),
    ).toBeNull();
  });
});

describe("sourceListenLabel", () => {
  const online = {
    name: "house-rear",
    state: "online",
    last_window_ts: 1000,
  };
  it("promotes a fresh online source to capturing", () => {
    expect(sourceListenLabel(online, 1000 + SOURCE_CAPTURING_S)).toBe(
      "capturing",
    );
  });
  it("leaves an online source alone once the capturing window passes", () => {
    expect(sourceListenLabel(online, 1000 + SOURCE_CAPTURING_S + 1)).toBe(
      "online",
    );
  });
  it("never calls a silent or offline source capturing", () => {
    expect(
      sourceListenLabel(
        { name: "a", state: "silent", last_window_ts: 1000 },
        1000,
      ),
    ).toBe("silent");
    expect(
      sourceListenLabel(
        { name: "b", state: "offline", last_window_ts: 1000 },
        1000,
      ),
    ).toBe("offline");
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
