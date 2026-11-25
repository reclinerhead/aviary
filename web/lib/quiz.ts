// The question of the day (issue #719, phase 2 of epic #712): the pure half.
//
// One multiple-choice question a day per box, on the announcement strip: the
// same question and the same four birds in the same order on every device,
// an answer recorded on the box, a friendly reveal, and a count that only
// goes up. Everything here is a pure function over plain data; the store
// (quiz.db), the clock and the cookies live in app/aviary/page.tsx and
// app/aviary/quiz/answer/route.ts.
//
// Two kinds of question in this phase. A "fact" question comes from the
// reviewed pool (web/data/quiz.json, written by listener/quiz.py): its
// answer is the entry's own species, its wrong answers are drawn from the
// entry's checked "wrong" list, and its reveal is the fact itself, read
// from the fact pool. A "yard" question is built from the box's own record
// (visits, first heard, last heard) with no model anywhere, and only when
// the answer leads by a margin wide enough that a new detection during the
// day cannot overturn it.
//
// The answer never reaches the browser before it is given: quizNotice()
// copies the question and the four options and nothing else. The pool's
// "reason" field never leaves the server at all.

import { fnv1a, type AviaryNotice } from "./aviary";

// --- Tunables (epic #712 open decision 3: starting values) -----------------

/** The life list ranked by lifetime visits: the top 12 are regulars, the
 * next 30 occasionals, the rest rarities. */
export const QUIZ_REGULARS = 12;
export const QUIZ_OCCASIONALS = 30;
/** The subject's tier is drawn 6 : 3 : 1. Raw visit weighting would ask
 * about the same ten birds until their questions ran out. */
export const QUIZ_TIER_WEIGHTS = { regular: 6, occasional: 3, rarity: 1 } as const;
/** The kind is drawn 7 : 3 among kinds that still have a question. */
export const QUIZ_KIND_WEIGHTS = { fact: 7, yard: 3 } as const;

/** "Most visits": the answer has at least twice each other bird's visits. */
export const YARD_MOST_VISITS_LEAD = 2;
/** "Heard only once": each other bird has at least this many visits. */
export const YARD_ONCE_OTHERS_MIN_VISITS = 20;
/** "Joined most recently": first heard at least this long after each other. */
export const YARD_NEWEST_GAP_S = 7 * 86400;
/** "Longest without being heard": last heard at least this long before each
 * other bird's last visit. */
export const YARD_ABSENT_GAP_S = 21 * 86400;
/** No yard question on a box listening for less than this, or holding
 * fewer species than this: its record is too thin to ask about. */
export const YARD_MIN_LISTENING_S = 30 * 86400;
export const YARD_MIN_SPECIES = 12;
/** A yard question draws its three other birds from the answer's nearest
 * qualifying neighbours, so the choice is close without being unsafe. */
export const YARD_NEIGHBOURS = 10;

/** At most this many devices are recorded per day (a guard against a
 * stuffed log, not a tamper-proof score). */
export const QUIZ_DEVICE_CAP = 50;
/** The answer route's body cap. */
export const QUIZ_BODY_MAX_BYTES = 1024;

/** Holds the id of the question dismissed before answering (the fact's
 * idiom): tomorrow's question is a different id, so the cookie ends the
 * quiz for today only. */
export const QUIZ_DISMISS_COOKIE = "aviary_quiz_dismissed";
export const QUIZ_DISMISS_MAX_AGE_S = 2 * 86_400;
/** The anonymous device id, set by the answer route on a first answer. */
export const QUIZ_DEVICE_COOKIE = "aviary_quiz_device";
export const QUIZ_DEVICE_MAX_AGE_S = 2 * 365 * 86_400;

// --- The store --------------------------------------------------------------

/** quiz.db's two tables. The DDL rides the first write in the same
 * transaction (the rules route's precedent, SPECIES_RULES_DDL). quiz_days
 * freezes the day's question when it is picked; quiz_answers is a log, not
 * a tally, so the scoring rule can still change later. */
export const QUIZ_DDL =
  "CREATE TABLE IF NOT EXISTS quiz_days (" +
  " day TEXT PRIMARY KEY," +
  " question_id TEXT NOT NULL," +
  " kind TEXT NOT NULL," +
  " subject_sci TEXT NOT NULL," +
  " question TEXT NOT NULL," +
  " options TEXT NOT NULL," +
  " answer_sci TEXT NOT NULL," +
  " reveal TEXT NOT NULL," +
  " picked_ts INTEGER NOT NULL" +
  ");" +
  "CREATE TABLE IF NOT EXISTS quiz_answers (" +
  " day TEXT NOT NULL," +
  " device TEXT NOT NULL," +
  " choice TEXT NOT NULL," +
  " correct INTEGER NOT NULL," +
  " ts INTEGER NOT NULL," +
  " PRIMARY KEY (day, device)" +
  ");";

// --- Shapes -----------------------------------------------------------------

export type QuizKind = "fact" | "yard";
export type YardTemplate =
  | "most_visits"
  | "heard_once"
  | "newest"
  | "longest_absent";

export type QuizBird = { species_sci: string; species_common: string };

/** What the pick needs to know about one bird on the box's life list.
 * "visits" and "last_ts" come from tallyVisits, the one definition of a
 * visit; the pick never re-derives one. */
export type RosterFact = QuizBird & {
  visits: number;
  first_ts: number;
  last_ts: number | null;
};

/** One entry of web/data/quiz.json. The entry's own species is the answer. */
export type QuizPoolEntry = {
  fact_id: string;
  species_sci: string;
  species_common: string;
  question: string | null;
  wrong?: { species_sci: string; species_common: string; reason?: string }[];
};
export type QuizPool = { version?: number; questions: QuizPoolEntry[] };

/** What the reveal needs, as ids, counts and epoch seconds, never
 * sentences: the fact's text is read from the fact pool (one copy), and the
 * component words a yard reveal's date in the viewer's locale. */
export type QuizReveal =
  | { kind: "fact"; fact_id: string }
  | { kind: "yard"; template: YardTemplate; visits: number; ts: number | null };

/** A picked question, as frozen in quiz_days. */
export type QuizQuestion = {
  id: string;
  kind: QuizKind;
  subject_sci: string;
  question: string;
  /** Four birds, in the order every device shows them. */
  options: QuizBird[];
  answer_sci: string;
  reveal: QuizReveal;
};

/** The reveal as the answer route returns it. */
export type QuizRevealOut =
  | { kind: "fact"; text: string }
  | { kind: "yard"; template: YardTemplate; visits: number; ts: number | null };

export type QuizTally = { right: number; total: number };
export type QuizTallies = {
  device: QuizTally;
  station: QuizTally & { devices: number };
};

/** The answer route's whole response. */
export type QuizAnswerResult = QuizTallies & {
  correct: boolean;
  answer: QuizBird;
  reveal: QuizRevealOut;
};

// --- Days -------------------------------------------------------------------

/** The box's local calendar day, "YYYY-MM-DD", from the server clock (the
 * box lives in its yard's timezone; #677 is why a day is named there). */
export function localDayKey(d: Date): string {
  const two = (n: number) => String(n).padStart(2, "0");
  return d.getFullYear() + "-" + two(d.getMonth() + 1) + "-" + two(d.getDate());
}

/** "YYYY-MM" of a day key: the score's month. */
export function monthOf(day: string): string {
  return day.slice(0, 7);
}

/** The pick's seed: station and day, so every device on a box agrees and a
 * re-run of the same day picks the same question. */
export function quizSeed(station: string, day: string): string {
  return "quiz|" + station + "|" + day;
}

// --- A small seeded generator -----------------------------------------------

/** mulberry32 over an FNV-1a seed: tiny, stable across runtimes, and all a
 * daily draw needs. Not a security generator. */
function rngOf(seed: string): () => number {
  let a = fnv1a(seed);
  return () => {
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function pickOne<T>(rng: () => number, items: readonly T[]): T {
  return items[Math.floor(rng() * items.length)];
}

function shuffled<T>(rng: () => number, items: readonly T[]): T[] {
  const out = [...items];
  for (let i = out.length - 1; i > 0; i--) {
    const j = Math.floor(rng() * (i + 1));
    [out[i], out[j]] = [out[j], out[i]];
  }
  return out;
}

/** A weighted draw. Callers pass only the choices that are still possible,
 * which is how an empty tier's or an exhausted kind's weight folds into
 * the others. */
function weighted<T>(rng: () => number, choices: readonly [T, number][]): T {
  const total = choices.reduce((sum, [, w]) => sum + w, 0);
  let at = rng() * total;
  for (const [item, w] of choices) {
    at -= w;
    if (at < 0) return item;
  }
  return choices[choices.length - 1][0];
}

// --- Tiers ------------------------------------------------------------------

export type QuizTier = "regular" | "occasional" | "rarity";
const TIERS: readonly QuizTier[] = ["regular", "occasional", "rarity"];

/** The life list by lifetime visits, most first (the scientific name breaks
 * a tie so the order is the same on every load). */
function byVisits(roster: readonly RosterFact[]): RosterFact[] {
  return [...roster].sort(
    (a, b) => b.visits - a.visits || a.species_sci.localeCompare(b.species_sci),
  );
}

/** Each bird's tier: regulars, occasionals, rarities. */
export function quizTiers(roster: readonly RosterFact[]): Map<string, QuizTier> {
  const out = new Map<string, QuizTier>();
  byVisits(roster).forEach((bird, i) =>
    out.set(
      bird.species_sci,
      i < QUIZ_REGULARS
        ? "regular"
        : i < QUIZ_REGULARS + QUIZ_OCCASIONALS
          ? "occasional"
          : "rarity",
    ),
  );
  return out;
}

// --- "fact" questions -------------------------------------------------------

/** A fact question's id is its fact id, so rewording never asks it again. */
export function factQuestionId(factId: string): string {
  return "fact:" + factId;
}

/** The entry's usable wrong answers: each a different bird, none the answer. */
function wrongOf(entry: QuizPoolEntry): QuizBird[] {
  const seen = new Set<string>([entry.species_sci]);
  const out: QuizBird[] = [];
  for (const w of entry.wrong ?? []) {
    if (!w?.species_sci || !w.species_common || seen.has(w.species_sci)) continue;
    seen.add(w.species_sci);
    out.push({ species_sci: w.species_sci, species_common: w.species_common });
  }
  return out;
}

/** May this box be asked this entry today? It has a question, its answer is
 * on this box's life list, its fact is still in the fact pool, it keeps at
 * least three wrong answers, and it has never been asked here. */
export function factEligible(
  entry: QuizPoolEntry,
  lifeList: ReadonlySet<string>,
  factIds: ReadonlySet<string>,
  asked: ReadonlySet<string>,
): boolean {
  return (
    !!entry?.fact_id &&
    typeof entry.question === "string" &&
    entry.question.trim() !== "" &&
    lifeList.has(entry.species_sci) &&
    factIds.has(entry.fact_id) &&
    !asked.has(factQuestionId(entry.fact_id)) &&
    wrongOf(entry).length >= 3
  );
}

/** Three wrong answers for this box: birds on its own life list first (the
 * household knows them), the rest of the entry's list as the fallback. */
function drawWrong(
  entry: QuizPoolEntry,
  lifeList: ReadonlySet<string>,
  rng: () => number,
): QuizBird[] {
  const all = wrongOf(entry);
  const known = shuffled(rng, all.filter((w) => lifeList.has(w.species_sci)));
  const rest = shuffled(rng, all.filter((w) => !lifeList.has(w.species_sci)));
  return [...known, ...rest].slice(0, 3);
}

// --- "yard" questions -------------------------------------------------------

export const YARD_TEMPLATES: readonly YardTemplate[] = [
  "most_visits",
  "heard_once",
  "newest",
  "longest_absent",
];

/** Always "heard here": the record is what the microphones caught, not a
 * fact about the bird. */
export const YARD_QUESTIONS: Record<YardTemplate, string> = {
  most_visits: "Which of these birds has been heard here most often?",
  heard_once: "Which of these birds has been heard here only once?",
  newest: "Which of these birds was first heard here most recently?",
  longest_absent: "Which of these birds has gone longest without being heard here?",
};

/** A yard question's id is its template plus its four birds, sorted: the
 * same template can return with different birds, never the same set. */
export function yardQuestionId(template: YardTemplate, scis: readonly string[]): string {
  return "yard:" + template + ":" + [...scis].sort().join("|");
}

/** Is the box's record deep enough to ask about? */
export function yardGate(roster: readonly RosterFact[], now: number): boolean {
  if (roster.length < YARD_MIN_SPECIES) return false;
  const since = Math.min(...roster.map((r) => r.first_ts));
  return now - since >= YARD_MIN_LISTENING_S;
}

/** The birds that may sit beside "subject" as wrong answers under one
 * template: every one clears the template's margin against the subject, so
 * a tie or a thin lead never makes a question. Nearest to the margin first
 * (the closest calls are the better question), at most YARD_NEIGHBOURS. */
export function yardOthers(
  template: YardTemplate,
  subject: RosterFact,
  roster: readonly RosterFact[],
): RosterFact[] {
  const others = roster.filter((r) => r.species_sci !== subject.species_sci);
  const bySci = (a: RosterFact, b: RosterFact) =>
    a.species_sci.localeCompare(b.species_sci);
  let fit: RosterFact[] = [];
  if (template === "most_visits") {
    if (subject.visits < 1) return [];
    fit = others
      .filter((r) => r.visits >= 1 && r.visits * YARD_MOST_VISITS_LEAD <= subject.visits)
      .sort((a, b) => b.visits - a.visits || bySci(a, b));
  } else if (template === "heard_once") {
    if (subject.visits !== 1) return [];
    fit = others
      .filter((r) => r.visits >= YARD_ONCE_OTHERS_MIN_VISITS)
      .sort((a, b) => a.visits - b.visits || bySci(a, b));
  } else if (template === "newest") {
    fit = others
      .filter((r) => r.first_ts + YARD_NEWEST_GAP_S <= subject.first_ts)
      .sort((a, b) => b.first_ts - a.first_ts || bySci(a, b));
  } else {
    const last = subject.last_ts;
    if (last == null) return [];
    fit = others
      .filter((r) => r.last_ts != null && r.last_ts >= last + YARD_ABSENT_GAP_S)
      .sort((a, b) => (a.last_ts ?? 0) - (b.last_ts ?? 0) || bySci(a, b));
  }
  return fit.slice(0, YARD_NEIGHBOURS);
}

/** Every set of three other birds this subject and template have not been
 * asked with (or just the first, when the caller only needs to know there
 * is one). At most C(YARD_NEIGHBOURS, 3) = 120 sets, so it is counted
 * exactly rather than sampled. */
function yardSets(
  template: YardTemplate,
  subject: RosterFact,
  roster: readonly RosterFact[],
  asked: ReadonlySet<string>,
  firstOnly = false,
): RosterFact[][] {
  const others = yardOthers(template, subject, roster);
  const out: RosterFact[][] = [];
  for (let i = 0; i < others.length; i++)
    for (let j = i + 1; j < others.length; j++)
      for (let k = j + 1; k < others.length; k++) {
        const set = [others[i], others[j], others[k]];
        const id = yardQuestionId(template, [
          subject.species_sci,
          ...set.map((r) => r.species_sci),
        ]);
        if (asked.has(id)) continue;
        out.push(set);
        if (firstOnly) return out;
      }
  return out;
}

function yardReveal(template: YardTemplate, subject: RosterFact): QuizReveal {
  return {
    kind: "yard",
    template,
    visits: subject.visits,
    ts: template === "newest" ? subject.first_ts : subject.last_ts,
  };
}

// --- The pick ---------------------------------------------------------------

const bird = (r: QuizBird): QuizBird => ({
  species_sci: r.species_sci,
  species_common: r.species_common,
});

/**
 * Today's question for one box, or null when nothing is eligible (no quiz
 * that day, never a repeat). A pure function of the pool, the ids of the
 * facts still in the fact pool, the roster, the ids already asked on this
 * box, the clock and the seed.
 *
 * The draw goes kind, then tier, then bird, then question, each among only
 * the choices that still hold a question: so an exhausted kind or an empty
 * tier folds its weight into the others, and a bird with five facts is no
 * likelier a subject than a bird with one.
 */
export function pickQuiz({
  pool,
  factIds,
  roster,
  asked,
  seed,
  now,
}: {
  pool: QuizPool;
  factIds: ReadonlySet<string>;
  roster: readonly RosterFact[];
  asked: ReadonlySet<string>;
  seed: string;
  now: number;
}): QuizQuestion | null {
  const rng = rngOf(seed);
  const ranked = byVisits(roster);
  const tiers = quizTiers(roster);
  const lifeList = new Set(roster.map((r) => r.species_sci));

  const facts = new Map<string, QuizPoolEntry[]>();
  for (const entry of pool.questions ?? []) {
    if (!factEligible(entry, lifeList, factIds, asked)) continue;
    const list = facts.get(entry.species_sci);
    if (list) list.push(entry);
    else facts.set(entry.species_sci, [entry]);
  }

  // Which templates still hold a question for each bird. Only the chosen
  // one's sets are listed in full, below.
  const yards = new Map<string, YardTemplate[]>();
  if (yardGate(roster, now))
    for (const subject of ranked) {
      const open = YARD_TEMPLATES.filter(
        (template) => yardSets(template, subject, roster, asked, true).length > 0,
      );
      if (open.length > 0) yards.set(subject.species_sci, open);
    }

  const has = (kind: QuizKind, sci: string) =>
    kind === "fact" ? facts.has(sci) : yards.has(sci);
  const subjectsOf = (kind: QuizKind, tier: QuizTier) =>
    ranked.filter((r) => tiers.get(r.species_sci) === tier && has(kind, r.species_sci));

  const kinds = (["fact", "yard"] as const)
    .filter((kind) => ranked.some((r) => has(kind, r.species_sci)))
    .map((kind): [QuizKind, number] => [kind, QUIZ_KIND_WEIGHTS[kind]]);
  if (kinds.length === 0) return null;
  const kind = weighted(rng, kinds);

  const tier = weighted(
    rng,
    TIERS.filter((t) => subjectsOf(kind, t).length > 0).map(
      (t): [QuizTier, number] => [t, QUIZ_TIER_WEIGHTS[t]],
    ),
  );
  const subject = pickOne(rng, subjectsOf(kind, tier));

  if (kind === "fact") {
    const entry = pickOne(rng, facts.get(subject.species_sci) ?? []);
    const known = new Map(roster.map((r) => [r.species_sci, r.species_common]));
    // The box's own name for a bird it has heard; the pool's otherwise.
    const wrong = drawWrong(entry, lifeList, rng).map((w) => ({
      species_sci: w.species_sci,
      species_common: known.get(w.species_sci) ?? w.species_common,
    }));
    return {
      id: factQuestionId(entry.fact_id),
      kind: "fact",
      subject_sci: subject.species_sci,
      question: (entry.question ?? "").trim(),
      options: shuffled(rng, [bird(subject), ...wrong]),
      answer_sci: subject.species_sci,
      reveal: { kind: "fact", fact_id: entry.fact_id },
    };
  }

  const template = pickOne(rng, yards.get(subject.species_sci) ?? []);
  const set = pickOne(rng, yardSets(template, subject, roster, asked));
  const four = [subject, ...set];
  return {
    id: yardQuestionId(template, four.map((r) => r.species_sci)),
    kind: "yard",
    subject_sci: subject.species_sci,
    question: YARD_QUESTIONS[template],
    options: shuffled(rng, four.map(bird)),
    answer_sci: subject.species_sci,
    reveal: yardReveal(template, subject),
  };
}

// --- The frozen day ---------------------------------------------------------

/** One quiz_days row, as stored. */
export type QuizDayRow = {
  question_id: string;
  kind: string;
  subject_sci: string;
  question: string;
  options: string;
  answer_sci: string;
  reveal: string;
};

/** The columns to store for a picked question. */
export function quizDayRow(q: QuizQuestion): QuizDayRow {
  return {
    question_id: q.id,
    kind: q.kind,
    subject_sci: q.subject_sci,
    question: q.question,
    options: JSON.stringify(q.options.map(bird)),
    answer_sci: q.answer_sci,
    reveal: JSON.stringify(q.reveal),
  };
}

/** A stored row back as a question, or null when it is not one this code
 * can ask: the stations run yesterday's code against today's store for up
 * to a day, so an unreadable row means no quiz, never an error page. */
export function quizFromRow(row: QuizDayRow | null | undefined): QuizQuestion | null {
  if (!row || (row.kind !== "fact" && row.kind !== "yard")) return null;
  let options: unknown;
  let reveal: unknown;
  try {
    options = JSON.parse(row.options);
    reveal = JSON.parse(row.reveal);
  } catch {
    return null;
  }
  if (!Array.isArray(options) || options.length !== 4) return null;
  const birds: QuizBird[] = [];
  for (const o of options as Partial<QuizBird>[]) {
    if (typeof o?.species_sci !== "string" || typeof o.species_common !== "string")
      return null;
    birds.push({ species_sci: o.species_sci, species_common: o.species_common });
  }
  if (new Set(birds.map((b) => b.species_sci)).size !== 4) return null;
  if (!birds.some((b) => b.species_sci === row.answer_sci)) return null;
  const r = reveal as Partial<{
    kind: string;
    fact_id: string;
    template: YardTemplate;
    visits: number;
    ts: number | null;
  }> | null;
  let shaped: QuizReveal;
  if (row.kind === "fact" && r?.kind === "fact" && typeof r.fact_id === "string") {
    shaped = { kind: "fact", fact_id: r.fact_id };
  } else if (
    row.kind === "yard" &&
    r?.kind === "yard" &&
    YARD_TEMPLATES.includes(r.template as YardTemplate) &&
    typeof r.visits === "number"
  ) {
    shaped = {
      kind: "yard",
      template: r.template as YardTemplate,
      visits: r.visits,
      ts: typeof r.ts === "number" ? r.ts : null,
    };
  } else return null;
  if (!row.question_id || !row.question) return null;
  return {
    id: row.question_id,
    kind: row.kind,
    subject_sci: row.subject_sci,
    question: row.question,
    options: birds,
    answer_sci: row.answer_sci,
    reveal: shaped,
  };
}

// --- The notice -------------------------------------------------------------

export function quizNoticeId(questionId: string): string {
  return "quiz:" + questionId;
}

/** The question id inside a quiz notice id (the dismiss cookie's value). */
export function quizIdOf(noticeId: string): string {
  return noticeId.startsWith("quiz:") ? noticeId.slice("quiz:".length) : noticeId;
}

/** The strip's question: the question is the headline and the four birds
 * are its buttons. Fields are copied one by one, so the answer, the subject
 * and the reveal cannot ride along to the browser. No species_sci (a
 * portrait would give the answer away), no href (so would the species
 * page), no ts (a server-seeded notice makes no locale call). */
export function quizNotice(q: Pick<QuizQuestion, "id" | "question" | "options">): AviaryNotice {
  return {
    id: quizNoticeId(q.id),
    kind: "quiz",
    title: q.question,
    options: q.options.map(bird),
  };
}

/** Whether the answer tiles carry portraits (#721): all four or none. A
 * wrong answer can be a bird this box has never heard, with no portrait
 * here, and three photos beside one blank would mark the blank as wrong. */
export function quizOptionPortraits(
  options: readonly QuizBird[],
  withPortrait: ReadonlySet<string>,
): boolean {
  return options.length > 0 && options.every((o) => withPortrait.has(o.species_sci));
}

/** The server's whole decision for one page load: no quiz when there is no
 * question today, when this device dismissed it, or when this device has
 * answered it (the fun fact takes the strip). */
export function quizFor({
  question,
  dismissedId,
  answered,
}: {
  question: QuizQuestion | null;
  dismissedId?: string | null;
  answered: boolean;
}): AviaryNotice | null {
  if (!question || answered || question.id === dismissedId) return null;
  return quizNotice(question);
}

// --- The answer route's pure parts ------------------------------------------

/** The choice in a POST body (a scientific name), or null when the body is
 * not the shape asked for. Whether it is one of today's four is isOption's
 * question, not this one's. */
export function parseAnswerPayload(body: unknown): string | null {
  if (body === null || typeof body !== "object" || Array.isArray(body)) return null;
  const choice = (body as { choice?: unknown }).choice;
  if (typeof choice !== "string") return null;
  const trimmed = choice.trim();
  return trimmed === "" || trimmed.length > 120 ? null : trimmed;
}

export function isOption(q: Pick<QuizQuestion, "options">, choice: string): boolean {
  return q.options.some((o) => o.species_sci === choice);
}

/** What to do with an answer. A device answers a question once: a second
 * answer replays the first one's result and stores nothing. A new device
 * past the day's cap still gets its reveal, and stores nothing either. */
export function answerPlan(
  existingChoice: string | null | undefined,
  devicesToday: number,
): "replay" | "store" | "skip" {
  if (existingChoice != null) return "replay";
  return devicesToday >= QUIZ_DEVICE_CAP ? "skip" : "store";
}

// --- The score --------------------------------------------------------------

/** Right answers over all answers for one local month, for one device and
 * pooled for the station. A dismissed or unopened day has no row and so
 * counts for nothing. Derived from the log on every read, so the rule can
 * change later without losing a day. */
export function quizTallies(
  rows: readonly { day: string; device: string; correct: number | boolean }[],
  month: string,
  device: string | null,
): QuizTallies {
  const out: QuizTallies = {
    device: { right: 0, total: 0 },
    station: { right: 0, total: 0, devices: 0 },
  };
  const devices = new Set<string>();
  for (const r of rows) {
    if (monthOf(r.day) !== month) continue;
    const right = r.correct ? 1 : 0;
    devices.add(r.device);
    out.station.total++;
    out.station.right += right;
    if (device !== null && r.device === device) {
      out.device.total++;
      out.device.right += right;
    }
  }
  out.station.devices = devices.size;
  return out;
}

// --- The reveal's words -----------------------------------------------------

/** Friendly either way: never "wrong" or "incorrect". */
export function revealHeadline(correct: boolean, answerCommon: string): string {
  return correct ? "That's right!" : "Good guess. It's the " + answerCommon + ".";
}

/** A yard reveal, worded from the frozen counts and moments. The component
 * hands in its own day formatter (the DayFormatter rule), so this file
 * stays locale-free. */
export function yardRevealLine(
  reveal: Extract<QuizRevealOut, { kind: "yard" }>,
  answerCommon: string,
  dayOf: (ts: number) => string,
): string {
  const on = reveal.ts == null ? "" : " on " + dayOf(reveal.ts);
  if (reveal.template === "most_visits")
    return (
      "The " + answerCommon + " has been heard here on " + reveal.visits +
      " visits, at least twice as many as each of the others."
    );
  if (reveal.template === "heard_once")
    return "The " + answerCommon + " has been heard here just once" + on + ".";
  if (reveal.template === "newest")
    return "The " + answerCommon + " was first heard here" + on + ".";
  return "The " + answerCommon + " was last heard here" + on + ".";
}

/** Counts, never a percentage: "You're 9 of 11 this month." and, once more
 * than one device has played this month, "This yard: 23 of 27." */
export function tallyLines(t: QuizTallies): string[] {
  const out: string[] = [];
  if (t.device.total > 0)
    out.push("You're " + t.device.right + " of " + t.device.total + " this month.");
  if (t.station.devices > 1)
    out.push("This yard: " + t.station.right + " of " + t.station.total + ".");
  return out;
}
