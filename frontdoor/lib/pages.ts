// =============================================================================
// project-squirrel -- frontdoor/lib/pages.ts
//
// The three human-facing responses of the front door, rendered as HTML
// strings from route handlers (no React tree in this app). They share the
// Homestead porch's look -- "Ranger Station, Night Watch": pine-black panel,
// faint topo contours, Fraunces for the one big line, Sometype Mono for the
// rest, the LED green. Tokens are COPIED from launchpad/styles.css, not
// abstracted (the house precedent), and the two faces are self-hosted from
// /public/fonts, the same files the porch serves.
//
// Every value interpolated here is either validated upstream (slug, ip,
// iface, sha) or escaped below; both, where it costs nothing.
// =============================================================================

import { describeAge, type StationRecord } from "./frontdoor";

function esc(s: string): string {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);
}

const STYLE = `
@font-face{font-family:"Fraunces";font-style:normal;font-weight:100 900;font-display:swap;src:url("/fonts/fraunces-latin-var.woff2") format("woff2")}
@font-face{font-family:"Sometype Mono";font-style:normal;font-weight:400 700;font-display:swap;src:url("/fonts/sometype-mono-latin-var.woff2") format("woff2")}
:root{--bg:#0c1310;--panel:#121c17;--line:#27392e;--line-bright:#3b5443;--ink:#ece4cf;--ink-dim:#93a794;--ink-faint:#5c6f5f;--led:#6be585;--amber:#e5b96b;
--font-display:"Fraunces","Times New Roman",serif;--font-mono:"Sometype Mono",ui-monospace,Menlo,monospace}
*{box-sizing:border-box}
html,body{margin:0;min-height:100%}
body{background-color:var(--bg);color:var(--ink);font-family:var(--font-mono);font-size:15px;line-height:1.5;
background-image:radial-gradient(ellipse 120% 90% at 50% -10%,rgba(107,229,133,.045),transparent 60%),
repeating-radial-gradient(ellipse 140% 110% at 85% 115%,transparent 0px,transparent 34px,rgba(107,229,133,.035) 34px,rgba(107,229,133,.035) 35px);
display:grid;place-items:center;padding:24px}
main{width:min(100%,560px);background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:28px 28px 24px;
box-shadow:0 30px 60px -30px rgba(0,0,0,.8)}
.eyebrow{margin:0 0 14px;color:var(--ink-faint);font-size:12px;letter-spacing:.14em;text-transform:uppercase}
h1{margin:0 0 18px;font-family:var(--font-display);font-weight:500;font-size:clamp(28px,6vw,40px);line-height:1.05;letter-spacing:-.01em}
p{margin:0 0 12px;color:var(--ink-dim)}
p:last-child{margin-bottom:0}
dl{margin:18px 0 0;display:grid;grid-template-columns:max-content 1fr;gap:8px 18px;border-top:1px solid var(--line);padding-top:16px}
dt{color:var(--ink-faint);font-size:12px;letter-spacing:.1em;text-transform:uppercase;padding-top:3px}
dd{margin:0;overflow-wrap:anywhere}
.led{display:inline-block;width:9px;height:9px;border-radius:50%;background:var(--led);box-shadow:0 0 10px var(--led);vertical-align:middle;margin-right:8px;animation:breathe 2.4s ease-in-out infinite}
.led.dim{background:var(--amber);box-shadow:0 0 10px var(--amber)}
.led.off{background:var(--ink-faint);box-shadow:none;animation:none}
@keyframes breathe{0%,100%{opacity:1}50%{opacity:.55}}
a{color:var(--led);text-decoration:none;border-bottom:1px solid var(--line-bright)}
a:hover{border-bottom-color:var(--led)}
code{color:var(--ink);background:rgba(107,229,133,.08);padding:1px 6px;border-radius:3px}
.foot{margin-top:22px;padding-top:14px;border-top:1px solid var(--line);color:var(--ink-faint);font-size:12px}
@media (prefers-reduced-motion:reduce){.led{animation:none}}
`;

function shell(title: string, body: string): string {
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<title>${esc(title)}</title>
<style>${STYLE}</style>
</head>
<body>
<main>
${body}
</main>
</body>
</html>
`;
}

export function landingPage(): string {
  return shell(
    "The Aviary front door",
    `<p class="eyebrow">The Aviary</p>
<h1>The front door.</h1>
<p>Each Aviary station has its own link here, and that link always leads to the station wherever it lives on its home network. Open the one you were given.</p>
<p class="foot">A station is reachable only from inside its own house. This page never carries bird data.</p>`,
  );
}

export function unknownStationPage(): string {
  return shell(
    "No such station",
    `<p class="eyebrow">The Aviary</p>
<h1>That isn't a station.</h1>
<p>Check the link you were given. Station links look like <code>aviary.toddtech.llc/name</code>, all lowercase.</p>`,
  );
}

export function notReportedPage(station: string): string {
  return shell(
    `${station} hasn't reported yet`,
    `<p class="eyebrow">The Aviary</p>
<h1><span class="led off"></span>${esc(station)} hasn't checked in yet.</h1>
<p>The station tells this page where it is on its home network the first time it comes online. Until then there is nowhere to send you.</p>
<p>If the box is plugged in and its light is on, give it a few minutes and try again.</p>`,
  );
}

export function statusPage(station: string, rec: StationRecord, now: Date = new Date()): string {
  const ageS = (now.getTime() - Date.parse(rec.seen_at)) / 1000;
  const led = Number.isNaN(ageS) ? "off" : ageS <= 20 * 60 ? "" : ageS <= 24 * 3600 ? "dim" : "off";
  return shell(
    `${station} · station status`,
    `<p class="eyebrow">The Aviary · station status</p>
<h1><span class="led ${led}"></span>${esc(station)}</h1>
<p>Last heard from <strong>${esc(describeAge(rec.seen_at, now))}</strong>. The link below goes to the station itself, and only works from inside its house.</p>
<dl>
<dt>Address</dt><dd><a href="/${esc(station)}">http://${esc(rec.ip)}/</a></dd>
<dt>Interface</dt><dd>${rec.iface ? esc(rec.iface) : "<span style=\"color:var(--ink-faint)\">not reported</span>"}</dd>
<dt>Build</dt><dd>${rec.sha ? `<code>${esc(rec.sha)}</code>` : "<span style=\"color:var(--ink-faint)\">not reported</span>"}</dd>
<dt>Checked in</dt><dd>${esc(rec.seen_at)}</dd>
</dl>
<p class="foot">Green: heard within 20 minutes. Amber: within a day. Grey: longer, but the address is still the best guess.</p>`,
  );
}
