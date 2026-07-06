# WP Rewriter — web UI

A small local web app for editing a live WordPress site over SSH (via WP-CLI):
connection check, find & replace, and XPath + OpenAI rewriting — all in the
browser, with live previews and streaming progress. The XPath parsing and OpenAI
calls run on your machine; WP-CLI on the server only reads and writes content.

```
wp-rewriter-ui/
├── app.py             ← Flask server (the web UI)
├── core.py            ← shared logic (SSH/WP-CLI, OpenAI, lxml)
├── rewriter.py        ← optional command-line interface (same core)
├── templates/
│   └── index.html
├── static/
│   ├── style.css
│   └── app.js
├── config.ini         ← saved settings (also editable from the UI)
├── job.json           ← rewrite job for the CLI
└── requirements.txt   ← Flask, lxml, requests
```

## Run it

```bash
pip install -r requirements.txt
python3 app.py
```

Then open <http://127.0.0.1:5000>. It binds to localhost only.

## The three panels

1. **Connection** — enter the SSH **host/IP**, **port**, **username**, and
   **password**, plus the **working folder path** (the WordPress directory that
   contains wp-config.php). Click **Test connection**; the status pill in the
   header turns green and shows the WordPress version when WP-CLI answers.
   Settings are saved to `config.ini`.

2. **Find & replace** — enter find/replace values (one per line, matched
   line-by-line), choose a scope, and run. **Preview** does a dry run; switch to
   **Apply** (the control and a banner turn amber) to commit. It uses
   `wp search-replace`, so URLs and image `src` inside content, and serialized
   page-builder/ACF data, are rewritten too. Three options:
   - **Case-insensitive** — also replaces other capitalizations while keeping
     each one's case. `ram metal → sham metal` then also turns `Ram Metal` into
     `Sham Metal`, `RAM METAL` into `SHAM METAL`, `Ram metal` into `Sham metal`,
     and the hyphenated slug forms (`ram-metal`, `Ram-Metal`, …) used in URLs.
   - **Replace URLs everywhere, including permalinks** — also rewrites the `guid`
     column. Use it to change URLs completely; note `guid` is a permanent feed
     identifier, so feed readers may re-show posts as new.
   - **Also rename media files on the server** — after the database is updated,
     renames upload files whose names contain the search term (including the
     resized variants like `-150x150`), so the rewritten image URLs still
     resolve. Preview lists what would be renamed; Apply performs the moves.

   Every pair and case variant runs inside a single WP-CLI process, so a job
   pays WordPress's startup cost once — not once per variant. For content-only
   edits, the **Posts table only** scope is the fastest.

3. **AI rewrite** — load the posts you want, map any placeholders to XPath, and
   add XPath + prompt rows. **Fetch sample** shows what an XPath matches; **Test
   prompt** runs one post through OpenAI so you can see the result first. Leave
   **Preview only** checked to call OpenAI without saving, or uncheck it to write
   back. Progress streams in live with per-post results.

## Security

This app connects with the SSH **password** and OpenAI key you enter, and both
are stored in plain text in `config.ini`. Keep the app on `127.0.0.1` (the
default), keep the folder private, and delete `config.ini` (or clear the
password/key fields) when you're done. If your server supports SSH keys, those
are safer than a stored password — say the word and I can switch the connection
back to key auth.

## Requirements on the server

WP-CLI (`wp`) must be installed and SSH (password login) must reach the
WordPress directory. The server does **not** need outbound internet — the OpenAI
calls happen on your machine.

## Optional CLI

The same operations are available without the browser:

```bash
python3 rewriter.py check
python3 rewriter.py replace "old" "new"            # dry run
python3 rewriter.py replace "old" "new" --apply
python3 rewriter.py rewrite                         # preview from job.json
python3 rewriter.py rewrite --apply                 # write changes
```

## A note before big runs

Back up the database (or use staging). Both Apply and the rewrite write to the
live database, and the rewrite writes content directly — there's no revision to
roll back to. Purge any persistent or page cache afterward.