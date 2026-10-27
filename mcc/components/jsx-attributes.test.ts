import { readdirSync, readFileSync } from "node:fs";
import { join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

// Issue #657: a JSX attribute string that spans lines compiles differently for
// the server bundle and the browser bundle -- one collapses the line breaks and
// indentation, the other keeps them -- so the server's HTML and the browser's
// first render disagree and React throws #418 on every production load, while
// dev mode never says a word. The UV note on the weather view did exactly that
// from #63 until #657. In a .tsx file a double-quoted string can only cross a
// line inside a JSX attribute, so the check below is exact; keep attribute text
// on one line (or in a {"..."} expression, which is a plain JS string).
const ROOT = fileURLToPath(new URL("..", import.meta.url));

function tsxFiles(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((e) => {
    const p = join(dir, e.name);
    if (e.isDirectory()) return tsxFiles(p);
    return e.name.endsWith(".tsx") ? [p] : [];
  });
}

/** `name` and 1-based line of every attribute string that crosses a line. A
 * line comment holding an unbalanced `="` is prose, not an attribute. */
function multilineAttributes(source: string): { line: number; name: string }[] {
  const found: { line: number; name: string }[] = [];
  for (const m of source.matchAll(/\s([A-Za-z][\w-]*)="[^"\r\n]*\r?\n/g)) {
    const start = source.lastIndexOf("\n", m.index) + 1;
    const lead = source.slice(start, m.index).trimStart();
    if (lead.startsWith("//") || lead.startsWith("*")) continue;
    found.push({ line: source.slice(0, m.index).split("\n").length, name: m[1] });
  }
  return found;
}

describe("multilineAttributes", () => {
  // The guard must bite, or the scan below could pass by finding nothing
  // (the test_import_boundary.py precedent).
  it("flags an attribute string that crosses a line, CRLF or LF", () => {
    expect(multilineAttributes('<X info="one\n  two" />')).toEqual([
      { line: 1, name: "info" },
    ]);
    expect(multilineAttributes('<X\r\n  a="b"\r\n  note="one\r\n  two"\r\n/>')).toEqual([
      { line: 3, name: "note" },
    ]);
  });

  it("leaves one-line attributes, expressions and comments alone", () => {
    expect(multilineAttributes('<X info="one two" sub={"a" +\n "b"} />')).toEqual([]);
    expect(multilineAttributes('// a label="half a quote\n<X />')).toEqual([]);
  });
});

describe("the web app's .tsx sources", () => {
  it("hold no JSX attribute string that spans lines (#657)", () => {
    const offenders = ["components", "app"]
      .flatMap((d) => tsxFiles(join(ROOT, d)))
      .flatMap((file) =>
        multilineAttributes(readFileSync(file, "utf8")).map(
          ({ line, name }) => `${relative(ROOT, file)}:${line} ${name}=`,
        ),
      );
    expect(offenders).toEqual([]);
  });
});
