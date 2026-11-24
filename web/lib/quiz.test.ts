import { describe, expect, it } from "vitest";
import {
  isAmbientKind,
  noticeChain,
  pickVisibleNotice,
  type AviaryNotice,
} from "./aviary";
import {
  QUIZ_DEVICE_CAP,
  YARD_ABSENT_GAP_S,
  YARD_MIN_LISTENING_S,
  YARD_NEWEST_GAP_S,
  answerPlan,
  factEligible,
  factQuestionId,
  isOption,
  localDayKey,
  monthOf,
  parseAnswerPayload,
  pickQuiz,
  quizDayRow,
  quizFor,
  quizFromRow,
  quizIdOf,
  quizNotice,
  quizNoticeId,
  quizSeed,
  quizTallies,
  quizTiers,
  revealHeadline,
  tallyLines,
  yardGate,
  yardOthers,
  yardQuestionId,
  yardRevealLine,
  type QuizPool,
  type QuizPoolEntry,
  type QuizQuestion,
  type RosterFact,
} from "./quiz";

// Synthetic throughout: the repo is public, so no row here came from a store.
const DAY = 86400;
const NOW = 1_800_000_000;

const sci = (i: number) => "Avis numero" + String(i).padStart(3, "0");
const common = (i: number) => "Test Bird " + i;

/** n birds, bird 0 the most visited. Old enough for the yard gate unless
 * "ageDays" says otherwise. */
function rosterOf(n: number, ageDays = 400): RosterFact[] {
  return Array.from({ length: n }, (_, i) => ({
    species_sci: sci(i),
    species_common: common(i),
    visits: (n - i) * 10,
    first_ts: NOW - ageDays * DAY + i * 60,
    last_ts: NOW - i * 3600,
  }));
}

function entryOf(i: number, k: number, wrong: number[]): QuizPoolEntry {
  return {
    fact_id: "f" + i + "_" + k,
    species_sci: sci(i),
    species_common: common(i),
    question: "Which bird is test bird " + i + ", question " + k + "?",
    wrong: wrong.map((w) => ({
      species_sci: sci(w),
      species_common: common(w),
      reason: "REASON-NEVER-SHIPS",
    })),
  };
}

/** "per" questions for each of the first n birds, wrong answers its
 * neighbours on the list. */
function poolOf(n: number, per = 1): QuizPool {
  const questions: QuizPoolEntry[] = [];
  for (let i = 0; i < n; i++)
    for (let k = 0; k < per; k++)
      questions.push(entryOf(i, k, [1, 2, 3, 4, 5].map((d) => (i + d) % n)));
  return { version: 1, questions };
}

const idsOf = (pool: QuizPool) => new Set(pool.questions.map((q) => q.fact_id));
const none = new Set<string>();
/** A roster too young for yard questions: fact questions only. */
const young = (n: number) => rosterOf(n, 5);

describe("pickQuiz", () => {
  it("never repeats a question across a simulated year", () => {
    const roster = rosterOf(60);
    const pool = poolOf(60, 2);
    const asked = new Set<string>();
    let picked = 0;
    for (let d = 0; d < 365; d++) {
      const q = pickQuiz({
        pool,
        factIds: idsOf(pool),
        roster,
        asked,
        seed: quizSeed("house", "day" + d),
        now: NOW,
      });
      if (!q) continue;
      expect(asked.has(q.id)).toBe(false);
      asked.add(q.id);
      picked++;
    }
    expect(picked).toBe(365);
    expect(asked.size).toBe(365);
  });

  it("gives the same pick for the same seed, and four distinct options", () => {
    const args = {
      pool: poolOf(40),
      factIds: idsOf(poolOf(40)),
      roster: rosterOf(40),
      asked: none,
      now: NOW,
    };
    for (let d = 0; d < 50; d++) {
      const seed = quizSeed("birdhouse", "2031-01-" + d);
      const a = pickQuiz({ ...args, seed });
      const b = pickQuiz({ ...args, seed });
      expect(a).not.toBeNull();
      expect(b).toEqual(a);
      const scis = a!.options.map((o) => o.species_sci);
      expect(new Set(scis).size).toBe(4);
      expect(scis).toContain(a!.answer_sci);
      expect(a!.subject_sci).toBe(a!.answer_sci);
    }
  });

  it("differs by station on the same day", () => {
    const pool = poolOf(40);
    const pick = (station: string) =>
      Array.from({ length: 20 }, (_, d) =>
        pickQuiz({
          pool,
          factIds: idsOf(pool),
          roster: young(40),
          asked: none,
          seed: quizSeed(station, "d" + d),
          now: NOW,
        })!.id,
      ).join(",");
    expect(pick("house")).not.toBe(pick("kestrel"));
  });

  function tierShares(roster: RosterFact[], pool: QuizPool, n = 4000) {
    const tiers = quizTiers(roster);
    const seen = { regular: 0, occasional: 0, rarity: 0 };
    for (let s = 0; s < n; s++) {
      const q = pickQuiz({
        pool,
        factIds: idsOf(pool),
        roster,
        asked: none,
        seed: "seed" + s,
        now: NOW,
      })!;
      seen[tiers.get(q.subject_sci)!]++;
    }
    return {
      regular: seen.regular / n,
      occasional: seen.occasional / n,
      rarity: seen.rarity / n,
    };
  }

  it("draws the subject's tier near 6 : 3 : 1", () => {
    const shares = tierShares(young(80), poolOf(80));
    expect(shares.regular).toBeGreaterThan(0.56);
    expect(shares.regular).toBeLessThan(0.64);
    expect(shares.occasional).toBeGreaterThan(0.26);
    expect(shares.occasional).toBeLessThan(0.34);
    expect(shares.rarity).toBeGreaterThan(0.07);
    expect(shares.rarity).toBeLessThan(0.13);
  });

  it("folds an empty tier's weight into the others", () => {
    // Questions only for regulars (birds 0-11) and rarities (42 and up):
    // 6 : 1, so regulars take about six sevenths.
    const all = poolOf(80);
    const pool = {
      questions: all.questions.filter((q) => {
        const i = Number(q.fact_id.slice(1).split("_")[0]);
        return i < 12 || i >= 42;
      }),
    };
    const shares = tierShares(young(80), pool);
    expect(shares.occasional).toBe(0);
    expect(shares.regular).toBeGreaterThan(0.82);
    expect(shares.regular).toBeLessThan(0.9);
    // A box with a dozen birds has regulars only.
    expect(tierShares(young(12), poolOf(12), 300).regular).toBe(1);
  });

  function kindShare(roster: RosterFact[], pool: QuizPool, n = 2000) {
    let fact = 0;
    let got = 0;
    for (let s = 0; s < n; s++) {
      const q = pickQuiz({
        pool,
        factIds: idsOf(pool),
        roster,
        asked: none,
        seed: "kind" + s,
        now: NOW,
      });
      if (!q) continue;
      got++;
      if (q.kind === "fact") fact++;
    }
    return { fact: got ? fact / got : 0, got };
  }

  it("mixes fact and yard near 7 : 3, and skips an exhausted kind", () => {
    const both = kindShare(rosterOf(60), poolOf(60));
    expect(both.fact).toBeGreaterThan(0.65);
    expect(both.fact).toBeLessThan(0.75);
    // No fact questions left: every pick is a yard question.
    const yardOnly = kindShare(rosterOf(60), { questions: [] }, 200);
    expect(yardOnly.got).toBe(200);
    expect(yardOnly.fact).toBe(0);
    // A young station has no yard questions: every pick is a fact.
    expect(kindShare(young(60), poolOf(60), 200).fact).toBe(1);
  });

  it("returns null when nothing is eligible", () => {
    expect(
      pickQuiz({
        pool: { questions: [] },
        factIds: none,
        roster: young(60),
        asked: none,
        seed: "s",
        now: NOW,
      }),
    ).toBeNull();
    expect(
      pickQuiz({
        pool: poolOf(10),
        factIds: idsOf(poolOf(10)),
        roster: [],
        asked: none,
        seed: "s",
        now: NOW,
      }),
    ).toBeNull();
    // Every question asked already: none, never a repeat.
    const pool = poolOf(10);
    const asked = new Set(pool.questions.map((q) => factQuestionId(q.fact_id)));
    expect(
      pickQuiz({ pool, factIds: idsOf(pool), roster: young(10), asked, seed: "s", now: NOW }),
    ).toBeNull();
  });
});

describe("fact questions", () => {
  const life = new Set([0, 1, 2, 3, 4, 5].map(sci));
  const good = entryOf(0, 0, [1, 2, 3]);
  const facts = new Set([good.fact_id]);

  it("needs a question, an answer on the life list and a fact still in the pool", () => {
    expect(factEligible(good, life, facts, none)).toBe(true);
    expect(factEligible({ ...good, question: null }, life, facts, none)).toBe(false);
    expect(factEligible({ ...good, question: "  " }, life, facts, none)).toBe(false);
    expect(factEligible(good, new Set([sci(1), sci(2)]), facts, none)).toBe(false);
    expect(factEligible(good, life, new Set(["other"]), none)).toBe(false);
    expect(
      factEligible(good, life, facts, new Set([factQuestionId(good.fact_id)])),
    ).toBe(false);
  });

  it("needs three different wrong answers that are not the answer", () => {
    expect(factEligible(entryOf(0, 0, [1, 2]), life, facts, none)).toBe(false);
    expect(factEligible(entryOf(0, 0, [1, 1, 2]), life, facts, none)).toBe(false);
    expect(factEligible(entryOf(0, 0, [0, 1, 2]), life, facts, none)).toBe(false);
    expect(factEligible({ ...good, wrong: undefined }, life, facts, none)).toBe(false);
  });

  function optionsFor(entry: QuizPoolEntry, roster: RosterFact[], seed: string) {
    const q = pickQuiz({
      pool: { questions: [entry] },
      factIds: new Set([entry.fact_id]),
      roster,
      asked: none,
      seed,
      now: NOW,
    })!;
    return q.options.map((o) => o.species_sci).filter((s) => s !== q.answer_sci);
  }

  it("prefers wrong answers on the life list and falls back to the rest", () => {
    // Birds 0-5 are on this box's list; 50-52 are not.
    const roster = young(6);
    const mixed = entryOf(0, 0, [50, 1, 51, 2, 52, 3]);
    const thin = entryOf(0, 0, [50, 1, 51, 52]);
    for (let s = 0; s < 40; s++) {
      expect(optionsFor(mixed, roster, "w" + s).sort()).toEqual([sci(1), sci(2), sci(3)]);
      const got = optionsFor(thin, roster, "w" + s);
      expect(got).toHaveLength(3);
      expect(got).toContain(sci(1));
    }
  });

  it("keeps the order for the day and varies it across days", () => {
    const roster = young(6);
    const entry = entryOf(0, 0, [1, 2, 3]);
    const order = (seed: string) =>
      pickQuiz({
        pool: { questions: [entry] },
        factIds: new Set([entry.fact_id]),
        roster,
        asked: none,
        seed,
        now: NOW,
      })!.options.map((o) => o.species_sci).join(",");
    expect(order("a")).toBe(order("a"));
    expect(new Set(Array.from({ length: 30 }, (_, i) => order("o" + i))).size).toBeGreaterThan(3);
  });

  it("sends the browser no answer and no reason", () => {
    const entry = entryOf(0, 0, [1, 2, 3]);
    const q = pickQuiz({
      pool: { questions: [entry] },
      factIds: new Set([entry.fact_id]),
      roster: young(6),
      asked: none,
      seed: "n",
      now: NOW,
    })!;
    expect(JSON.stringify(q)).not.toContain("REASON-NEVER-SHIPS");
    const notice = quizNotice(q);
    expect(Object.keys(notice).sort()).toEqual(["id", "kind", "options", "title"]);
    expect(notice.kind).toBe("quiz");
    expect(notice.title).toBe(entry.question);
    expect(notice.options).toEqual(q.options);
    for (const o of notice.options ?? [])
      expect(Object.keys(o).sort()).toEqual(["species_common", "species_sci"]);
    // Even a caller that hands over the whole question leaks nothing.
    const wide = quizNotice(q as QuizQuestion);
    expect(JSON.stringify(wide)).not.toContain("answer");
    expect(JSON.stringify(wide)).not.toContain("reveal");
    expect(JSON.stringify(wide)).not.toContain("subject");
  });
});

describe("yard questions", () => {
  const b = (i: number, visits: number, first: number, last: number | null): RosterFact => ({
    species_sci: sci(i),
    species_common: common(i),
    visits,
    first_ts: first,
    last_ts: last,
  });

  it("most visits needs a 2x lead over each other bird", () => {
    const subject = b(0, 100, 0, NOW);
    const roster = [subject, b(1, 50, 0, NOW), b(2, 51, 0, NOW), b(3, 10, 0, NOW), b(4, 0, 0, null)];
    expect(yardOthers("most_visits", subject, roster).map((r) => r.species_sci)).toEqual([
      sci(1),
      sci(3),
    ]);
    // A tie is no lead at all.
    expect(yardOthers("most_visits", b(0, 50, 0, NOW), [b(1, 50, 0, NOW)])).toEqual([]);
  });

  it("heard once needs exactly one visit and the others at twenty or more", () => {
    const roster = [b(1, 20, 0, NOW), b(2, 19, 0, NOW), b(3, 300, 0, NOW)];
    expect(
      yardOthers("heard_once", b(0, 1, 0, NOW), roster).map((r) => r.species_sci),
    ).toEqual([sci(1), sci(3)]);
    expect(yardOthers("heard_once", b(0, 2, 0, NOW), roster)).toEqual([]);
  });

  it("newest needs a seven-day gap in first heard", () => {
    const first = NOW - 10 * DAY;
    const roster = [
      b(1, 5, first - YARD_NEWEST_GAP_S, NOW),
      b(2, 5, first - YARD_NEWEST_GAP_S + 1, NOW),
      b(3, 5, first, NOW),
      b(4, 5, first - 100 * DAY, NOW),
    ];
    expect(
      yardOthers("newest", b(0, 5, first, NOW), roster).map((r) => r.species_sci),
    ).toEqual([sci(1), sci(4)]);
  });

  it("longest absent needs a 21-day gap in last heard", () => {
    const last = NOW - 60 * DAY;
    const roster = [
      b(1, 5, 0, last + YARD_ABSENT_GAP_S),
      b(2, 5, 0, last + YARD_ABSENT_GAP_S - 1),
      b(3, 5, 0, last),
      b(4, 5, 0, null),
      b(5, 5, 0, NOW),
    ];
    expect(
      yardOthers("longest_absent", b(0, 5, 0, last), roster).map((r) => r.species_sci),
    ).toEqual([sci(1), sci(5)]);
    expect(yardOthers("longest_absent", b(0, 5, 0, null), roster)).toEqual([]);
  });

  it("asks nothing of a young or small station", () => {
    expect(yardGate(rosterOf(12), NOW)).toBe(true);
    expect(yardGate(rosterOf(11), NOW)).toBe(false);
    expect(yardGate(rosterOf(40, 29), NOW)).toBe(false);
    const exact = rosterOf(12).map((r) => ({ ...r, first_ts: NOW - YARD_MIN_LISTENING_S }));
    expect(yardGate(exact, NOW)).toBe(true);
    expect(yardGate([], NOW)).toBe(false);
  });

  it("builds a question whose answer clears its margin, and never the same four twice", () => {
    const roster = rosterOf(40);
    const byId = new Map(roster.map((r) => [r.species_sci, r]));
    const asked = new Set<string>();
    for (let d = 0; d < 200; d++) {
      const q = pickQuiz({
        pool: { questions: [] },
        factIds: none,
        roster,
        asked,
        seed: "yard" + d,
        now: NOW,
      })!;
      expect(q.kind).toBe("yard");
      expect(q.question).toContain("heard here");
      expect(asked.has(q.id)).toBe(false);
      asked.add(q.id);
      if (q.reveal.kind !== "yard") throw new Error("a yard question has a yard reveal");
      expect(q.id).toBe(yardQuestionId(q.reveal.template, q.options.map((o) => o.species_sci)));
      const subject = byId.get(q.answer_sci)!;
      const others = yardOthers(q.reveal.template, subject, roster).map((r) => r.species_sci);
      for (const o of q.options)
        if (o.species_sci !== q.answer_sci) expect(others).toContain(o.species_sci);
      expect(q.reveal.visits).toBe(subject.visits);
    }
  });

  it("has no question when no bird leads by the margin", () => {
    // Twelve old birds with the same counts and moments: every template ties.
    const flat = Array.from({ length: 12 }, (_, i) => b(i, 30, NOW - 400 * DAY, NOW - DAY));
    expect(
      pickQuiz({ pool: { questions: [] }, factIds: none, roster: flat, asked: none, seed: "t", now: NOW }),
    ).toBeNull();
  });

  it("an id does not depend on the order of its birds", () => {
    expect(yardQuestionId("newest", ["b", "a", "d", "c"])).toBe(
      yardQuestionId("newest", ["d", "c", "b", "a"]),
    );
    expect(yardQuestionId("newest", ["a", "b", "c", "d"])).not.toBe(
      yardQuestionId("most_visits", ["a", "b", "c", "d"]),
    );
  });
});

describe("the frozen day", () => {
  const pool = poolOf(20);
  const q = pickQuiz({
    pool,
    factIds: idsOf(pool),
    roster: young(20),
    asked: none,
    seed: "row",
    now: NOW,
  })!;

  it("round-trips a question through its row", () => {
    expect(quizFromRow(quizDayRow(q))).toEqual(q);
    const yard = pickQuiz({
      pool: { questions: [] },
      factIds: none,
      roster: rosterOf(30),
      asked: none,
      seed: "row",
      now: NOW,
    })!;
    expect(quizFromRow(quizDayRow(yard))).toEqual(yard);
  });

  it("reads an unusable row as no question", () => {
    const row = quizDayRow(q);
    expect(quizFromRow(null)).toBeNull();
    expect(quizFromRow({ ...row, kind: "call" })).toBeNull();
    expect(quizFromRow({ ...row, options: "not json" })).toBeNull();
    expect(quizFromRow({ ...row, options: "[]" })).toBeNull();
    expect(quizFromRow({ ...row, answer_sci: "Not listed" })).toBeNull();
    expect(quizFromRow({ ...row, reveal: "{}" })).toBeNull();
    const twice = JSON.stringify([q.options[0], q.options[0], q.options[1], q.options[2]]);
    expect(quizFromRow({ ...row, options: twice })).toBeNull();
  });

  it("shows no quiz once dismissed or answered", () => {
    expect(quizFor({ question: q, answered: false })?.id).toBe(quizNoticeId(q.id));
    expect(quizFor({ question: q, answered: true })).toBeNull();
    expect(quizFor({ question: q, answered: false, dismissedId: q.id })).toBeNull();
    expect(quizFor({ question: q, answered: false, dismissedId: "fact:other" })).not.toBeNull();
    expect(quizFor({ question: null, answered: false })).toBeNull();
    expect(quizIdOf(quizNoticeId(q.id))).toBe(q.id);
  });

  it("names a day and its month in the box's own zone", () => {
    expect(localDayKey(new Date(2031, 0, 5, 23, 59))).toBe("2031-01-05");
    expect(localDayKey(new Date(2031, 10, 30, 0, 0))).toBe("2031-11-30");
    expect(monthOf("2031-11-30")).toBe("2031-11");
  });
});

describe("the answer route's pure parts", () => {
  it("parses a choice and refuses anything else", () => {
    expect(parseAnswerPayload({ choice: "Strix varia" })).toBe("Strix varia");
    expect(parseAnswerPayload({ choice: "  Strix varia " })).toBe("Strix varia");
    for (const bad of [
      null,
      undefined,
      "Strix varia",
      7,
      [],
      {},
      { choice: 7 },
      { choice: "" },
      { choice: "   " },
      { choice: "x".repeat(121) },
      { choices: "Strix varia" },
    ])
      expect(parseAnswerPayload(bad)).toBeNull();
  });

  it("accepts only one of today's four", () => {
    const q = { options: [0, 1, 2, 3].map((i) => ({ species_sci: sci(i), species_common: common(i) })) };
    expect(isOption(q, sci(2))).toBe(true);
    expect(isOption(q, sci(4))).toBe(false);
    expect(isOption(q, common(2))).toBe(false);
  });

  it("stores one answer per device, up to the day's cap", () => {
    expect(answerPlan(null, 0)).toBe("store");
    expect(answerPlan(undefined, QUIZ_DEVICE_CAP - 1)).toBe("store");
    expect(answerPlan(null, QUIZ_DEVICE_CAP)).toBe("skip");
    // A device that has answered gets its first answer back, cap or no cap.
    expect(answerPlan(sci(1), 0)).toBe("replay");
    expect(answerPlan(sci(1), QUIZ_DEVICE_CAP + 5)).toBe("replay");
  });
});

describe("the score", () => {
  const rows = [
    { day: "2031-10-31", device: "a", correct: 1 },
    { day: "2031-11-01", device: "a", correct: 1 },
    { day: "2031-11-01", device: "b", correct: 0 },
    { day: "2031-11-02", device: "a", correct: 0 },
    { day: "2031-11-30", device: "b", correct: 1 },
    { day: "2031-12-01", device: "c", correct: 1 },
  ];

  it("counts one local month, for the device and for the station", () => {
    expect(quizTallies(rows, "2031-11", "a")).toEqual({
      device: { right: 1, total: 2 },
      station: { right: 2, total: 4, devices: 2 },
    });
    // The days either side of the boundary belong to their own months.
    expect(quizTallies(rows, "2031-10", "a").station).toEqual({ right: 1, total: 1, devices: 1 });
    expect(quizTallies(rows, "2031-12", "c").device).toEqual({ right: 1, total: 1 });
  });

  it("is zero for a device with no answers, and for no device", () => {
    expect(quizTallies(rows, "2031-11", "z").device).toEqual({ right: 0, total: 0 });
    expect(quizTallies(rows, "2031-11", null).device).toEqual({ right: 0, total: 0 });
    expect(quizTallies([], "2031-11", "a")).toEqual({
      device: { right: 0, total: 0 },
      station: { right: 0, total: 0, devices: 0 },
    });
  });

  it("is worded as counts, with the yard's line once a second device plays", () => {
    expect(tallyLines(quizTallies(rows, "2031-10", "a"))).toEqual(["You're 1 of 1 this month."]);
    expect(tallyLines(quizTallies(rows, "2031-11", "a"))).toEqual([
      "You're 1 of 2 this month.",
      "This yard: 2 of 4.",
    ]);
    expect(tallyLines(quizTallies(rows, "2031-11", null))).toEqual(["This yard: 2 of 4."]);
    expect(tallyLines(quizTallies(rows, "2031-11", "a")).join(" ")).not.toContain("%");
  });
});

describe("the reveal's words", () => {
  it("is friendly either way", () => {
    expect(revealHeadline(true, "Barred Owl")).toBe("That's right!");
    expect(revealHeadline(false, "Barred Owl")).toBe("Good guess. It's the Barred Owl.");
    expect(revealHeadline(false, "Barred Owl").toLowerCase()).not.toMatch(/wrong|incorrect/);
  });

  it("words a yard reveal from counts and the caller's day", () => {
    const day = () => "DAY";
    const of = (template: "most_visits" | "heard_once" | "newest" | "longest_absent") =>
      yardRevealLine({ kind: "yard", template, visits: 42, ts: 1 }, "Barred Owl", day);
    expect(of("most_visits")).toContain("42 visits");
    expect(of("heard_once")).toBe("The Barred Owl has been heard here just once on DAY.");
    expect(of("newest")).toBe("The Barred Owl was first heard here on DAY.");
    expect(of("longest_absent")).toBe("The Barred Owl was last heard here on DAY.");
    expect(
      yardRevealLine({ kind: "yard", template: "newest", visits: 1, ts: null }, "Barred Owl", day),
    ).toBe("The Barred Owl was first heard here.");
  });
});

describe("the quiz on the strip", () => {
  const n = (id: string, kind: AviaryNotice["kind"]): AviaryNotice => ({ id, kind, title: id });
  const quiz = n(quizNoticeId("fact:abc"), "quiz");
  const fact = n("fun_fact:abc", "fun_fact");
  const never = () => false;

  it("is ambient, and its id carries the question id", () => {
    expect(isAmbientKind("quiz")).toBe(true);
    expect(quiz.id).toBe("quiz:fact:abc");
  });

  it("is outranked by every event and outranks the fun fact", () => {
    for (const event of [n("r", "rare_return"), n("b", "new_best")])
      expect(pickVisibleNotice([fact, quiz, event], never)?.id).toBe(event.id);
    expect(pickVisibleNotice([fact, quiz], never)?.id).toBe(quiz.id);
    expect(pickVisibleNotice([fact, quiz, n("d", "demo")], never)?.id).toBe(quiz.id);
    // Answered or dismissed, the fun fact follows it.
    expect(pickVisibleNotice([fact, quiz], (id) => id === quiz.id)?.id).toBe(fact.id);
  });

  it("holds its place in the chain, and the fact is still the first cut", () => {
    const events = [n("r1", "rare_return"), n("r2", "rare_return"), n("b", "new_best")];
    const full = noticeChain([fact, quiz, ...events], never, 3);
    expect(full.chain.map((x) => x.id)).toEqual(["r1", "r2", "b"]);
    expect(full.cut.map((x) => x.id)).toEqual([quiz.id, fact.id]);
    const two = noticeChain([fact, quiz, events[0], events[2]], never, 3);
    expect(two.chain.map((x) => x.id)).toEqual(["r1", "b", quiz.id]);
    expect(two.cut.map((x) => x.id)).toEqual([fact.id]);
  });
});
