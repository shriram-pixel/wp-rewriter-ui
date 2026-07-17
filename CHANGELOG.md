# Changelog

## v3 — stable snapshot

Rewrite
- One AI call per page (blocks batched with markers) instead of one call per paragraph.
- Structure preserved: paragraphs never merged; a paragraph is split only if the
  ORIGINAL was over 120 words (split at sentence boundaries, tags/markers never cut).
- Leak guard: replies that leak reasoning, invent text, or mangle markers are rejected
  and the original block is kept.
- Leading bold "Label:" on list items kept as literal HTML.
- Breadcrumbs skipped.
- Short default prompt (42 words) in both the UI and job.json.

Writes / reports
- Pages written in batches of 10 as the run goes, each batch in ONE transaction:
  all of it lands or none of it does.
- A page is marked "updated" only after the site confirms the write.
- A failed batch rolls back -> those pages stay "pending" with a retry note.
- Per-run CSV report (page_id, title, status, time, note) written as the run goes.
- Find & Replace report: time-only, find/replace pairs, count, completed/failed.

Posts panel
- Pages are NOT selected by default.
- Optional "Upload last updated page report" -> skips pages already rewritten.
- Reports are CUMULATIVE: an uploaded report's completed pages are carried into the
  new report, so each upload skips everything done so far.

Find & Replace
- Case-preserving URL/media slug handling; exact as-typed casing included.
- Serialization-safe; serialized objects skipped.

Not included
- Rewrite timer (pause / auto-resume on limit) — see REWRITE-TIMER-SPEC.md.
