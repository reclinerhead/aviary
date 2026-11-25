// The question of the day's answer (issue #719, epic #712).
//
// POST /aviary/quiz/answer   body { choice }   (a scientific name)
//   -> { correct, answer, reveal, device: { right, total },
//        station: { right, total, devices } }      tallies for the local month
//
// The answer is checked HERE, against the question frozen in quiz.db for the
// box's local day: the page sends the browser the question and its four
// birds and nothing else, so this response is the first time the answer
// leaves the server. The pool's "reason" field never leaves it at all.
//
// quiz.db (MERLE_QUIZ_DB) is the web app's own store. It is not earl.db and
// this route never opens earl.db: any device on the household's network can
// post here, and those writes must not touch the bird record.
//
// Posture: no MERLE_QUIZ_DB is 404 (a box without the variable has no quiz);
// a malformed or oversized body, or a choice that is not one of today's
// four, is 400; no question frozen for today is 404; a store that cannot be
// opened or written is 500. Unlike the rules route, a missing store file is
// created: nothing else writes it, and an empty quiz.db harms nothing.
//
// One answer per device per question, enforced by the table's primary key:
// a second answer stores nothing and gets the first one's result back. The
// device is a random id in a cookie set here on a first answer, and nothing
// else about the device or the person is stored. At most QUIZ_DEVICE_CAP
// devices are recorded per day; past that the reveal is still returned.
// This is a friendly score on a household network, not a tamper-proof one.

import { randomUUID } from "node:crypto";
import { DatabaseSync } from "node:sqlite";
import { cookies } from "next/headers";
import pool from "@/data/fun-facts.json";
import type { FunFactPool } from "@/lib/aviary";
import {
  QUIZ_BODY_MAX_BYTES,
  QUIZ_DDL,
  QUIZ_DEVICE_COOKIE,
  QUIZ_DEVICE_MAX_AGE_S,
  answerPlan,
  isOption,
  localDayKey,
  monthOf,
  parseAnswerPayload,
  quizFromRow,
  quizTallies,
  type QuizAnswerResult,
  type QuizDayRow,
  type QuizQuestion,
  type QuizRevealOut,
} from "@/lib/quiz";

// The reveal of a fact question is the fact itself, read from the fact pool,
// so there is one copy. A fact that has left the pool reveals as its answer
// alone.
const FACT_TEXT = new Map(
  (pool as FunFactPool).species.flatMap((s) =>
    s.facts.map((f): [string, string] => [f.id, f.text]),
  ),
);

// A device id is what this route minted; anything else is replaced.
const DEVICE_ID = /^[0-9a-f-]{36}$/;

const status = (code: number) => new Response(null, { status: code });

export async function POST(req: Request) {
  // Bracket access: the variable arrives on a box after the bundle was built.
  const dbPath = (process.env["MERLE_QUIZ_DB"] ?? "").trim();
  if (!dbPath) return status(404);

  if (Number(req.headers.get("content-length") ?? 0) > QUIZ_BODY_MAX_BYTES)
    return status(400);
  let text: string;
  try {
    text = await req.text();
  } catch {
    return status(400);
  }
  if (Buffer.byteLength(text) > QUIZ_BODY_MAX_BYTES) return status(400);
  let body: unknown;
  try {
    body = JSON.parse(text);
  } catch {
    return status(400);
  }
  const choice = parseAnswerPayload(body);
  if (choice === null) return status(400);

  const jar = await cookies();
  const sent = jar.get(QUIZ_DEVICE_COOKIE)?.value;
  const known = sent !== undefined && DEVICE_ID.test(sent);
  const device = known ? sent : randomUUID();

  const now = new Date();
  const day = localDayKey(now);
  const month = monthOf(day);

  let db: DatabaseSync;
  try {
    db = new DatabaseSync(dbPath, { timeout: 2000 }); // created if absent
  } catch {
    return status(500);
  }
  let question: QuizQuestion | null;
  let first: string = choice;
  let rows: { day: string; device: string; correct: number }[] = [];
  try {
    db.exec("BEGIN IMMEDIATE");
    try {
      db.exec(QUIZ_DDL);
      question = quizFromRow(
        db
          .prepare(
            "SELECT question_id, kind, subject_sci, question, options," +
              " answer_sci, reveal FROM quiz_days WHERE day = ?",
          )
          .get(day) as QuizDayRow | undefined,
      );
      if (question && isOption(question, choice)) {
        const existing = db
          .prepare("SELECT choice FROM quiz_answers WHERE day = ? AND device = ?")
          .get(day, device) as { choice: string } | undefined;
        const devices = db
          .prepare("SELECT COUNT(*) AS n FROM quiz_answers WHERE day = ?")
          .get(day) as { n: number };
        const plan = answerPlan(existing?.choice, devices.n);
        if (plan === "replay" && existing) first = existing.choice;
        if (plan === "store")
          db.prepare(
            "INSERT INTO quiz_answers (day, device, choice, correct, ts)" +
              " VALUES (?,?,?,?,?)",
          ).run(
            day,
            device,
            choice,
            choice === question.answer_sci ? 1 : 0,
            Math.floor(now.getTime() / 1000),
          );
        rows = db
          .prepare(
            "SELECT day, device, correct FROM quiz_answers" +
              " WHERE substr(day, 1, 7) = ?",
          )
          .all(month) as typeof rows;
      }
      db.exec("COMMIT");
    } catch (e) {
      try {
        db.exec("ROLLBACK");
      } catch {}
      throw e;
    }
  } catch {
    return status(500);
  } finally {
    db.close();
  }

  if (!question) return status(404); // nothing frozen for today
  if (!isOption(question, choice)) return status(400);

  if (!known)
    // Path "/", Lax, not Secure: a station's LAN address serves plain http.
    jar.set(QUIZ_DEVICE_COOKIE, device, {
      path: "/",
      sameSite: "lax",
      httpOnly: true,
      maxAge: QUIZ_DEVICE_MAX_AGE_S,
    });

  const answer = question.options.find(
    (o) => o.species_sci === question.answer_sci,
  ) ?? { species_sci: question.answer_sci, species_common: question.answer_sci };
  const reveal: QuizRevealOut =
    question.reveal.kind === "fact"
      ? { kind: "fact", text: FACT_TEXT.get(question.reveal.fact_id) ?? "" }
      : question.reveal;
  const result: QuizAnswerResult = {
    correct: first === question.answer_sci,
    answer,
    reveal,
    ...quizTallies(rows, month, device),
  };
  return Response.json(result, { headers: { "cache-control": "no-store" } });
}
