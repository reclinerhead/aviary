import type { Metadata } from "next";
import { cookies } from "next/headers";
import { DatabaseSync } from "node:sqlite";
import { Aviary } from "@/components/Aviary";
import pool from "@/data/fun-facts.json";
import quizPool from "@/data/quiz.json";
import {
  FACT_DISMISS_COOKIE,
  funFactFor,
  localDayNumber,
  stationBrandingFromEnv,
  tallyVisits,
  type AviaryNotice,
  type FunFactPool,
} from "@/lib/aviary";
import {
  QUIZ_DDL,
  QUIZ_DEVICE_COOKIE,
  QUIZ_DISMISS_COOKIE,
  localDayKey,
  pickQuiz,
  quizDayRow,
  quizFor,
  quizFromRow,
  quizSeed,
  type QuizDayRow,
  type QuizPool,
  type QuizQuestion,
  type RosterFact,
} from "@/lib/quiz";

// The /weather page precedent (#142): a plain URL for the launchpad's tile.
// This segment holds a page AND API children (/aviary/roster, /aviary/recent)
// on purpose -- fine in the App Router, and static segments outrank the
// [species] sibling, so no bird can shadow a route (no species is named
// "roster" in any taxonomy BirdNET ships).
//
// force-dynamic + stationBrandingFromEnv: Next inlines process.env.FOO at
// build time; stations set MERLE_STATION_NAME / MERLE_AVIARY_TITLE only in
// station.env at runtime, after the bundle was built.
export const dynamic = "force-dynamic";

export function generateMetadata(): Metadata {
  const { title } = stationBrandingFromEnv();
  return {
    title,
    description:
      "Every bird the yard has announced, and the arrivals as they happen.",
  };
}

/** The box's life list, for the fun fact (#422): only a bird this yard has
 * heard may be today's fact. The roster route's posture -- no env, no file,
 * no table is an empty list, never an error page. */
function lifeListOf(path: string | undefined): Set<string> {
  if (!path) return new Set();
  let db: DatabaseSync;
  try {
    db = new DatabaseSync(path, { readOnly: true });
  } catch {
    return new Set();
  }
  try {
    const rows = db.prepare("SELECT species_sci FROM life_list").all() as {
      species_sci: string;
    }[];
    return new Set(rows.map((r) => r.species_sci));
  } catch {
    return new Set();
  } finally {
    db.close();
  }
}

/** Today's fun fact for this request (#422), or null once today's was
 * dismissed. Events don't silence it (#668): the strip queues it behind
 * them. The clock is read here, outside render. */
function todaysFunFact(station: string, dismissedId: string | undefined) {
  const now = new Date();
  return funFactFor({
    pool: pool as FunFactPool,
    lifeList: lifeListOf(process.env.MERLE_EARL_DB),
    station,
    day: localDayNumber(now),
    dismissedId,
  });
}

// --- The question of the day (#719) ------------------------------------------
//
// One question a day per box, frozen in quiz.db (MERLE_QUIZ_DB) by the first
// dashboard load of the box's local day, so every device sees the same
// question and the same four birds in the same order. Unset means no quiz:
// a box that has not been given the variable behaves exactly as before.
// Bracket access, for stationBrandingFromEnv's reason: the variable arrives
// on a box after the bundle was built.
//
// quiz.db is the web app's own store, beside earl.db and never a table in
// it: any device on the household's network can post an answer, and those
// writes must not touch the bird record. Unlike the rules route, the store
// is created when it is missing, because nothing else writes it.

const FACT_IDS: ReadonlySet<string> = new Set(
  (pool as FunFactPool).species.flatMap((s) => s.facts.map((f) => f.id)),
);

const QUIZ_DAY_SQL =
  "SELECT question_id, kind, subject_sci, question, options, answer_sci," +
  " reveal FROM quiz_days WHERE day = ?";

/** What the pick needs from the bird record: each life-list bird with its
 * visits (tallyVisits, the one definition) and its first and last heard.
 * Read-only, the roster route's quiet-empty posture. */
function rosterFactsOf(path: string | undefined): RosterFact[] {
  if (!path) return [];
  let db: DatabaseSync;
  try {
    db = new DatabaseSync(path, { readOnly: true });
  } catch {
    return [];
  }
  try {
    const life = db
      .prepare("SELECT species_sci, species_common, first_ts FROM life_list")
      .all() as { species_sci: string; species_common: string; first_ts: number }[];
    const tallies = tallyVisits(
      db.prepare("SELECT species_sci, ts FROM sightings ORDER BY ts").all() as {
        species_sci: string;
        ts: number;
      }[],
      null,
    );
    return life.map((r) => ({
      ...r,
      visits: tallies[r.species_sci]?.visits ?? 0,
      last_ts: tallies[r.species_sci]?.last_ts ?? null,
    }));
  } catch {
    return [];
  } finally {
    db.close();
  }
}

/** Today's frozen question and whether this device has answered it, from a
 * read-only open: the common case, and it takes no write lock. "missing"
 * when there is no store, no table or no row yet. */
function readQuizDay(
  path: string,
  day: string,
  device: string | undefined,
): { row: QuizDayRow; answered: boolean } | "missing" {
  let db: DatabaseSync;
  try {
    db = new DatabaseSync(path, { readOnly: true });
  } catch {
    return "missing";
  }
  try {
    const row = db.prepare(QUIZ_DAY_SQL).get(day) as QuizDayRow | undefined;
    if (!row) return "missing";
    const answered =
      !!device &&
      db
        .prepare("SELECT 1 FROM quiz_answers WHERE day = ? AND device = ?")
        .get(day, device) !== undefined;
    return { row, answered };
  } catch {
    return "missing";
  } finally {
    db.close();
  }
}

/** The day's first load: pick a question and freeze it. Insert-or-ignore,
 * then read back, so two first loads at once agree on one question. The
 * DDL rides the write (the rules route's precedent). A path that cannot be
 * opened or written means no quiz, never an error page. */
function freezeQuizDay(
  path: string,
  day: string,
  station: string,
  now: Date,
): QuizDayRow | null {
  // Read before the write lock is taken: the pick walks every sighting.
  const roster = rosterFactsOf(process.env.MERLE_EARL_DB);
  if (roster.length === 0) return null;
  let db: DatabaseSync;
  try {
    db = new DatabaseSync(path, { timeout: 2000 });
  } catch {
    return null;
  }
  try {
    db.exec("BEGIN IMMEDIATE");
    try {
      db.exec(QUIZ_DDL);
      let row = db.prepare(QUIZ_DAY_SQL).get(day) as QuizDayRow | undefined;
      if (!row) {
        const asked = new Set(
          (
            db.prepare("SELECT question_id FROM quiz_days").all() as {
              question_id: string;
            }[]
          ).map((r) => r.question_id),
        );
        const picked = pickQuiz({
          pool: quizPool as QuizPool,
          factIds: FACT_IDS,
          roster,
          asked,
          seed: quizSeed(station, day),
          now: Math.floor(now.getTime() / 1000),
        });
        if (picked) {
          const r = quizDayRow(picked);
          db.prepare(
            "INSERT OR IGNORE INTO quiz_days (day, question_id, kind," +
              " subject_sci, question, options, answer_sci, reveal, picked_ts)" +
              " VALUES (?,?,?,?,?,?,?,?,?)",
          ).run(
            day,
            r.question_id,
            r.kind,
            r.subject_sci,
            r.question,
            r.options,
            r.answer_sci,
            r.reveal,
            Math.floor(now.getTime() / 1000),
          );
          row = db.prepare(QUIZ_DAY_SQL).get(day) as QuizDayRow | undefined;
        }
      }
      db.exec("COMMIT");
      return row ?? null;
    } catch (e) {
      try {
        db.exec("ROLLBACK");
      } catch {}
      throw e;
    }
  } catch {
    return null;
  } finally {
    db.close();
  }
}

/** A cookie value as written by the browser's encodeURIComponent. */
function cookieText(value: string | undefined): string | undefined {
  if (value === undefined) return undefined;
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

/** Today's question for this request (#719), as a notice carrying the
 * question and its four birds and nothing else, or null: no store, nothing
 * eligible today, or this device has dismissed or answered it. */
function todaysQuiz(
  station: string,
  dismissed: string | undefined,
  device: string | undefined,
): AviaryNotice | null {
  const path = (process.env["MERLE_QUIZ_DB"] ?? "").trim();
  if (!path) return null;
  const now = new Date();
  const day = localDayKey(now);
  const read = readQuizDay(path, day, device);
  let question: QuizQuestion | null;
  let answered = false;
  if (read === "missing") {
    question = quizFromRow(freezeQuizDay(path, day, station, now));
  } else {
    question = quizFromRow(read.row);
    answered = read.answered;
  }
  return quizFor({ question, dismissedId: cookieText(dismissed), answered });
}

export default async function AviaryPage() {
  const { stationName, title } = stationBrandingFromEnv();
  // The fun fact is picked HERE, not in the browser (#422): it is in the
  // first paint, so the strip never pops in after hydration, and the pool
  // never ships to the client. Cookies, because the server makes the pick.
  const jar = await cookies();
  const funFact = todaysFunFact(
    stationName ?? "house",
    jar.get(FACT_DISMISS_COOKIE)?.value,
  );
  // The question of the day (#719) is picked here too, for the same two
  // reasons, and a third: the answer stays on the server.
  const quiz = todaysQuiz(
    stationName ?? "house",
    jar.get(QUIZ_DISMISS_COOKIE)?.value,
    jar.get(QUIZ_DEVICE_COOKIE)?.value,
  );
  return (
    <Aviary
      stationName={stationName}
      title={title}
      funFact={funFact}
      quiz={quiz}
    />
  );
}
