#!/usr/bin/env python3
"""
Shared core for WP Rewriter — drives a live WordPress site over SSH via WP-CLI.

Used by app.py (web UI) and rewriter.py (CLI). All functions return data; none
print or exit, so they're safe to import.
"""

import configparser
import html
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.ini")

BUILD = "build 36 (2026-06-29) — AI provider option: API key OR Claude CLI (claude -p, no key)"

# Optional deps — imported lazily so importing core never crashes.
try:
    import lxml.html as _lxml_html
except ImportError:
    _lxml_html = None
try:
    import requests as _requests
except ImportError:
    _requests = None
try:
    import paramiko as _paramiko
except ImportError:
    _paramiko = None


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DEFAULTS = {
    "ssh": {"host": "", "username": "", "port": "22", "password": ""},
    "wordpress": {"path": "/var/www/html", "wp_bin": "wp"},
    "openai": {"api_key": "", "base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini", "max_tokens": "256", "temperature": "0.7", "provider": "api"},
    "claude": {"command": "claude", "model": ""},
}


def get_config(path=CONFIG_PATH):
    cp = configparser.ConfigParser(interpolation=None)
    cp.read_dict(DEFAULTS)
    if os.path.isfile(path):
        cp.read(path)
    return cp


def save_config(data, path=CONFIG_PATH):
    cp = get_config(path)
    for section, fields in data.items():
        if section not in cp:
            cp.add_section(section)
        for key, value in fields.items():
            cp.set(section, key, str(value))
    with open(path, "w", encoding="utf-8") as fh:
        cp.write(fh)


# ---------------------------------------------------------------------------
# SSH / WP-CLI
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# SSH connection — password auth via paramiko
# ---------------------------------------------------------------------------
_client = None
_client_key = None
_ssh_lock = threading.Lock()


def _conn_key(cfg):
    s = cfg["ssh"]
    return (s.get("host", ""), s.get("port", "22"), s.get("username", ""), s.get("password", ""))


def _get_client(cfg):
    global _client, _client_key
    key = _conn_key(cfg)
    if (_client is not None and _client_key == key
            and _client.get_transport() is not None
            and _client.get_transport().is_active()):
        return _client
    if _client is not None:
        try:
            _client.close()
        except Exception:
            pass
        _client = None
    host = cfg["ssh"].get("host", "").strip()
    if not host:
        raise RuntimeError("SSH host/IP is not set.")
    client = _paramiko.SSHClient()
    client.set_missing_host_key_policy(_paramiko.AutoAddPolicy())
    client.connect(
        hostname=host,
        port=int(cfg["ssh"].get("port", "22") or 22),
        username=cfg["ssh"].get("username", ""),
        password=cfg["ssh"].get("password", ""),
        look_for_keys=False,
        allow_agent=False,
        timeout=20,
    )
    _client = client
    _client_key = key
    return client


def reset_client():
    """Drop the cached connection (call after credentials change)."""
    global _client, _client_key
    with _ssh_lock:
        if _client is not None:
            try:
                _client.close()
            except Exception:
                pass
        _client = None
        _client_key = None


def ssh_run(cfg, remote_cmd, stdin_data=None, timeout=120):
    if _paramiko is None:
        return (127, "", "The 'paramiko' package is not installed (pip install -r requirements.txt).")
    try:
        with _ssh_lock:
            client = _get_client(cfg)
            stdin, stdout, stderr = client.exec_command(remote_cmd, timeout=timeout)
            if stdin_data is not None:
                stdin.write(stdin_data)
                stdin.channel.shutdown_write()
            out = stdout.read().decode("utf-8", "replace")
            err = stderr.read().decode("utf-8", "replace")
            rc = stdout.channel.recv_exit_status()
        return (rc, out, err)
    except _paramiko.AuthenticationException:
        return (255, "", "SSH authentication failed — check the username and password.")
    except Exception as exc:  # noqa: BLE001
        return (255, "", "SSH connection error: " + str(exc))


def wp_cmd(cfg, args):
    base = [cfg["wordpress"].get("wp_bin", "wp"), "--path=" + cfg["wordpress"]["path"]]
    return " ".join(shlex.quote(a) for a in (base + args))


def wp_run(cfg, args, stdin_data=None, timeout=120):
    return ssh_run(cfg, wp_cmd(cfg, args), stdin_data=stdin_data, timeout=timeout)


# ---------------------------------------------------------------------------
# OpenAI (runs locally)
# ---------------------------------------------------------------------------
def _provider_base_from_key(key):
    """Infer the API endpoint from the key prefix so users don't have to set it."""
    k = (key or "").strip()
    if k.startswith("sk-or-"):
        return "https://openrouter.ai/api/v1"
    if k.startswith("sk-ant-"):
        return None  # Anthropic's native API isn't OpenAI-compatible
    if k.startswith("AIza"):
        return "https://generativelanguage.googleapis.com/v1beta/openai"
    if k.startswith("gsk_"):
        return "https://api.groq.com/openai/v1"
    if k.startswith("sk-"):
        return "https://api.openai.com/v1"
    return None


def openai_complete(cfg, prompt, _retries=5):
    if _requests is None:
        raise RuntimeError("The 'requests' package is not installed (pip install -r requirements.txt).")
    key = cfg["openai"].get("api_key", "").strip()
    if not key:
        raise RuntimeError("API key is not set. Add it on the Connection tab.")
    base = cfg["openai"].get("base_url", "").strip().rstrip("/")
    detected = _provider_base_from_key(key)
    if not base:
        base = detected or "https://api.openai.com/v1"
    elif base == "https://api.openai.com/v1" and detected and detected != base:
        base = detected  # key clearly belongs to another provider — use its endpoint
    model = cfg["openai"].get("model", "gpt-4o-mini")
    if "openrouter.ai" in base and "/" not in model:
        model = "openai/" + model  # OpenRouter model ids are namespaced
    resp = _requests.post(
        base + "/chat/completions",
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [{"role": "system", "content": _REWRITE_SYSTEM},
                         {"role": "user", "content": prompt}],
            "max_tokens": cfg["openai"].getint("max_tokens", fallback=256),
            "temperature": cfg["openai"].getfloat("temperature", fallback=0.7),
        },
        timeout=60,
    )
    if resp.status_code == 429 and _retries > 0:
        ra = (resp.headers.get("Retry-After") or "").strip()
        try:
            wait = float(ra)
        except ValueError:
            wait = min(5 * (2 ** (5 - _retries)), 60)  # 5, 10, 20, 40, 60s
        time.sleep(min(wait, 60))
        return openai_complete(cfg, prompt, _retries - 1)
    if resp.status_code // 100 != 2:
        try:
            msg = resp.json()["error"]["message"]
        except Exception:
            msg = "HTTP %d" % resp.status_code
        if resp.status_code == 429:
            msg += " (rate limit — the free tier allows very few requests; use a paid key or gemini-2.5-flash-lite, or rewrite fewer items)"
        raise RuntimeError("AI API: " + msg)
    return resp.json()["choices"][0]["message"]["content"]


# ---------------------------------------------------------------------------
# Claude CLI backend (uses the local Claude Code login — no API key)
# ---------------------------------------------------------------------------
_CLAUDE_CLI_TIMEOUT = 180


def ai_provider(cfg):
    if cfg.has_section("openai"):
        return (cfg["openai"].get("provider", "api") or "api").strip().lower()
    return "api"


# Models often wrap the answer in chatter ("Here's the rewritten text:"), code
# fences, or quotes. These patterns match ONLY recognisable lead-ins, so real
# content is never touched.
_AI_OPENER = r"(?:(?:sure|certainly|of\s+course|absolutely|okay|ok|alright|got\s+it)[\s,!.:\-–—]*)?"
_AI_LEAD = (r"(?:here(?:'s|\s+is|\s+are)|below\s+is|this\s+is|the\s+following\s+is|"
            r"(?:the\s+)?(?:rewritten|revised|updated|improved|edited|new|polished|cleaned[\s-]*up))")
_AI_PREAMBLE_COLON_RE = re.compile(r"^\s*" + _AI_OPENER + _AI_LEAD + r"[^:\n]{0,48}:\s*", re.IGNORECASE)
_AI_PREAMBLE_LINE_RE = re.compile(
    r"^\s*" + _AI_OPENER +
    r"(?:here(?:'s|\s+is|\s+are)|below\s+is)\s+(?:the\s+)?"
    r"(?:rewritten|revised|updated|improved|edited|new|polished|following)\s+"
    r"(?:text|paragraph|version|content|heading|copy)\s*[.!]?\s*\n",
    re.IGNORECASE,
)
_AI_SIGNOFF_RE = re.compile(
    r"\n+\s*(?:let me know|i hope this|hope this helps|feel free to|would you like|"
    r"is there anything|happy to)[^\n]*$", re.IGNORECASE)
# A trailing commentary block set off from the content by a blank line — the
# mirror of the preamble case. Scoped to clear meta openers so a legitimate
# final paragraph (e.g. "This is our best product.") is never swallowed.
_AI_POSTAMBLE_RE = re.compile(
    r"\n\s*\n\s*[\(\[\"'\u201c\u2018]?\s*"
    r"(?:note[:\s]|n\.?b\.?[:\s]|"
    r"i['\u2019](?:ve|m)\b|i (?:have|swapped|rewrote|changed|updated|kept|made|used)\b|"
    r"this (?:version|rewrite|keeps|maintains|preserves)\b|"
    r"here(?:['\u2019]s| is| are)\b|as (?:requested|instructed|per)\b|"
    r"changes?(?: made)?:|the (?:above|rewritten|revised)\b|"
    r"let me know|hope this|i hope|feel free|would you like|is there anything|happy to|"
    r"swapp?(?:ing|ed) each\b)"
    r".*$",
    re.IGNORECASE | re.DOTALL)
# A first line that reads like commentary (mentions a meta word) and ends with a
# colon, separated from the actual content by a blank line.
_AI_LABEL_LINE_RE = re.compile(
    r"^\s*(?=[^\n]*\b(?:swap|swapp|rewrit|rewrote|rewrote|updat|revis|chang|replac|"
    r"generat|creat|produc|result|output|version|paragraph|heading|keywords?|"
    r"here|below|following|sure|certainly|okay)\b)"
    r"[^\n]{1,120}:[ \t]*\n\s*\n(?=\S)", re.IGNORECASE)
_QUOTES = "\"'\u201c\u201d\u2018\u2019"

# Sent to the model as a system instruction so it returns ONLY the replacement
# text — no preamble, commentary, or labels leaking into the page.
_REWRITE_SYSTEM = (
    "You are a text-rewriting engine embedded in a tool that writes your output "
    "directly into a web page. Return ONLY the rewritten replacement text and "
    "nothing else. Do not add any preamble, explanation, commentary, labels, "
    "headings, or notes about what you did, and do not wrap the result in quotes. "
    "Preserve any placeholder markers exactly. Output the replacement text verbatim."
)


def _clean_ai_reply(text):
    """Strip conversational wrappers a model may add, leaving only the rewrite."""
    if not isinstance(text, str):
        return text
    original = text.strip()
    s = original
    if not s:
        return s
    # 1) ```code fences```
    if s.startswith("```"):
        s = re.sub(r"^```[A-Za-z0-9_+-]*\s*\n?", "", s)
        s = re.sub(r"\n?```\s*$", "", s).strip()
    # 2) leading preamble ("Here's the rewritten text:" / "Rewritten paragraph:")
    m = _AI_PREAMBLE_COLON_RE.match(s) or _AI_PREAMBLE_LINE_RE.match(s)
    if m:
        s = s[m.end():].lstrip("\n").strip()
    # 2b) a meta-commentary label line ("Swapping each adjacent pair … keywords:")
    #     set off from the content by a blank line. Scoped to meta words so real
    #     content like "Our services include:" is left alone.
    m = _AI_LABEL_LINE_RE.match(s)
    if m:
        s = s[m.end():].strip()
    # 3) trailing sign-off / postamble ("Let me know…", "Note: I swapped…")
    s = _AI_SIGNOFF_RE.sub("", s).strip()
    s = _AI_POSTAMBLE_RE.sub("", s).strip()
    # 4) the whole reply wrapped in quotes
    if len(s) >= 2 and s[0] in _QUOTES and s[-1] in _QUOTES:
        inner = s[1:-1].strip()
        if inner:
            s = inner
    # never return empty (don't let cleanup wipe a field)
    return s if s else original


def ai_complete(cfg, prompt):
    """Route to the configured AI backend: the HTTP API or the local Claude CLI."""
    if ai_provider(cfg) == "cli":
        reply = claude_cli_complete(cfg, prompt)
    else:
        reply = openai_complete(cfg, prompt)
    return _clean_ai_reply(reply)


def claude_cli_complete(cfg, prompt):
    """Run the Claude Code CLI in headless mode (`claude -p`) and return its text.
    The prompt is sent on stdin so content with shell-special characters is safe.
    Authentication uses the user's `claude login` session — no API key required."""
    sec = cfg["claude"] if cfg.has_section("claude") else None
    command = (sec.get("command", "claude") if sec else "claude").strip() or "claude"
    model = (sec.get("model", "") if sec else "").strip()
    args = [command, "-p", "--output-format", "text", "--append-system-prompt", _REWRITE_SYSTEM]
    if model:
        args += ["--model", model]
    resolved = shutil.which(command)
    if resolved:
        args[0] = resolved
    workdir = tempfile.gettempdir()  # neutral dir: no project CLAUDE.md / source picked up

    def _run(call_args, use_shell=False):
        return subprocess.run(
            call_args, input=prompt, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=_CLAUDE_CLI_TIMEOUT,
            cwd=workdir, shell=use_shell,
        )

    try:
        try:
            proc = _run(args)
        except (FileNotFoundError, OSError, NotADirectoryError):
            # Windows: `claude` is usually a .cmd shim the shell must resolve/run.
            proc = _run(subprocess.list2cmdline(args), use_shell=True)
    except subprocess.TimeoutExpired:
        raise RuntimeError("Claude CLI timed out after %ds (try a shorter prompt or fewer items)." % _CLAUDE_CLI_TIMEOUT)
    except (FileNotFoundError, OSError):
        raise RuntimeError(
            "Claude CLI '%s' not found. Install Claude Code (npm i -g @anthropic-ai/claude-code), "
            "run 'claude login', or set the command path on the Connection tab." % command)

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        low = err.lower()
        if any(w in low for w in ("login", "logged in", "unauthor", "authentication", "api key")):
            err += " — run 'claude login' in a terminal to sign in to Claude Code."
        raise RuntimeError("Claude CLI: " + (err[:600] or ("exit code %d" % proc.returncode)))
    out = (proc.stdout or "").strip()
    if not out:
        raise RuntimeError("Claude CLI returned empty output. Check that you're signed in (`claude login`).")
    return out


def ai_test(cfg):
    """Send a tiny prompt through the active provider to confirm it works."""
    reply = ai_complete(cfg, "Reply with exactly: OK")
    return {"ok": True, "provider": ai_provider(cfg), "reply": (reply or "").strip()[:200]}


# ---------------------------------------------------------------------------
# HTML / XPath (lxml)
# ---------------------------------------------------------------------------
def _require_lxml():
    if _lxml_html is None:
        raise RuntimeError("The 'lxml' package is not installed (pip install -r requirements.txt).")


def load_root(content):
    _require_lxml()
    return _lxml_html.fragment_fromstring(content or "", create_parent="div")


def inner_html(root):
    parts = [root.text or ""]
    for child in root:
        parts.append(_lxml_html.tostring(child, encoding="unicode"))
    return "".join(parts)


def node_text(node):
    return node.text_content() if hasattr(node, "text_content") else str(node)


def strip_tags(s):
    return re.sub(r"<[^>]+>", "", s)


def set_node_content(node, reply):
    for child in list(node):
        node.remove(child)
    node.text = None
    if strip_tags(reply) == reply:
        node.text = reply
    else:
        frag = _lxml_html.fragment_fromstring(reply, create_parent="span")
        node.text = frag.text
        for child in frag:
            node.append(child)


def render_prompt(tpl, root, title, placeholders, current_text=""):
    out = tpl
    if root is not None:
        for key, expr in (placeholders or {}).items():
            nodes = root.xpath(expr) if expr else []
            out = out.replace("{{" + key.strip("{}") + "}}", node_text(nodes[0]) if nodes else "")
        h1 = root.xpath("//h1")
        out = out.replace("{{h1}}", node_text(h1[0]) if h1 else "")
    out = out.replace("{{title}}", title or "")
    out = out.replace("{{text}}", current_text or "")
    return out


# ---------------------------------------------------------------------------
# Post read / write
# ---------------------------------------------------------------------------
def read_post(cfg, pid):
    rc, out, err = wp_run(cfg, ["post", "get", str(pid),
                                "--fields=ID,post_title,post_content", "--format=json"])
    if rc != 0:
        raise RuntimeError(err.strip() or "wp post get failed")
    return json.loads(out)


def write_post(cfg, pid, content):
    tmp = "/tmp/wprw-%d-%d.html" % (os.getpid(), int(pid))
    rc, _, err = ssh_run(cfg, "cat > " + shlex.quote(tmp), stdin_data=content)
    if rc != 0:
        raise RuntimeError(err.strip() or "could not stage content on server")
    php = ('$w=$GLOBALS["wpdb"];'
           '$w->update($w->posts,array("post_content"=>file_get_contents("%s")),array("ID"=>%d));'
           "clean_post_cache(%d);" % (tmp, int(pid), int(pid)))
    rc, _, err = wp_run(cfg, ["eval", php])
    ssh_run(cfg, "rm -f " + shlex.quote(tmp))
    if rc != 0:
        raise RuntimeError(err.strip() or "wp eval (write) failed")


# ---------------------------------------------------------------------------
# High-level operations
# ---------------------------------------------------------------------------
def check_connection(cfg):
    rc, out, err = wp_run(cfg, ["--version"], timeout=30)
    if rc != 0:
        return {"ok": False, "error": (err.strip() or "WP-CLI not reachable over SSH.")}
    result = {"ok": True, "wp_cli": (out.strip().splitlines() or [""])[0]}
    rc, out, _ = wp_run(cfg, ["core", "version"], timeout=30)
    result["wordpress"] = out.strip() if rc == 0 else "(path not found)"
    rc, out, _ = wp_run(cfg, ["config", "get", "table_prefix"], timeout=30)
    result["prefix"] = out.strip() if rc == 0 else ""
    return result


# Page builders the agent recognizes, in the priority order detection uses.
# Meta-flag builders are the most reliable, so they win over content-shortcode
# guesses (a page can contain a stray shortcode without being built with it).
BUILDERS = ("elementor", "bricks", "oxygen", "beaver", "divi", "wpbakery",
            "gutenberg", "classic")


def detect_builder(sig):
    """Map cheap per-post signals to a single builder key.

    `sig` (gathered by the list runner) may contain: elementor
    (_elementor_edit_mode value), elementor_data (1/0 presence),
    beaver (_fl_builder_enabled), oxygen / bricks (1/0 presence),
    divi_meta (_et_pb_use_builder), vc_meta (_wpb_vc_js_status), and the
    content flags has_vc / has_etpb / has_block. Returns one of BUILDERS.
    This is the single source of truth for detection, kept in Python so it is
    testable without a database.
    """
    sig = sig or {}

    def truthy(v):
        return str(v).strip().lower() in ("1", "true", "on", "yes", "builder")

    if str(sig.get("elementor", "")).strip().lower() == "builder" or sig.get("elementor_data"):
        return "elementor"
    if sig.get("bricks"):
        return "bricks"
    if sig.get("oxygen"):
        return "oxygen"
    if truthy(sig.get("beaver", "")):
        return "beaver"
    if truthy(sig.get("divi_meta", "")) or sig.get("has_etpb"):
        return "divi"
    if truthy(sig.get("vc_meta", "")) or sig.get("has_vc"):
        return "wpbakery"
    if sig.get("has_block"):
        return "gutenberg"
    return "classic"


# One boot: list posts of a type with the cheap builder signals attached.
# Substring checks use LOCATE (literal, no LIKE wildcards) and the big
# _elementor_data JSON is probed for presence only, never shipped.
_LIST_RUNNER_PHP = r"""<?php
if ( ! defined( 'WP_CLI' ) || ! WP_CLI ) { return; }
global $wpdb;
$in = json_decode( file_get_contents( $args[0] ), true );
$post_type = isset($in['post_type']) ? (string) $in['post_type'] : 'page';
$statuses  = ( isset($in['statuses']) && is_array($in['statuses']) ) ? $in['statuses'] : array('publish');

$st = array();
foreach ( $statuses as $s ) { $st[] = "'" . esc_sql( (string) $s ) . "'"; }
if ( empty($st) ) { $st[] = "'publish'"; }
$st_in = implode( ',', $st );
$pt = esc_sql( $post_type );

$posts = $wpdb->get_results(
    "SELECT ID, post_title, post_type,
        (LOCATE('[vc_', post_content) > 0)     AS has_vc,
        (LOCATE('[et_pb_', post_content) > 0)  AS has_etpb,
        (LOCATE('<!-- wp:', post_content) > 0) AS has_block
     FROM {$wpdb->posts}
     WHERE post_type = '{$pt}' AND post_status IN ({$st_in})
     ORDER BY post_title ASC",
    ARRAY_A
);
$posts = (array) $posts;

$ids = array();
foreach ( $posts as $p ) { $ids[] = (int) $p['ID']; }

$meta_map = array();
$el_data  = array();
if ( ! empty( $ids ) ) {
    $id_in = implode( ',', array_map( 'intval', $ids ) );
    $keys = array(
        '_elementor_edit_mode', '_fl_builder_enabled', 'ct_builder_shortcodes',
        '_bricks_page_content_2', '_et_pb_use_builder', '_wpb_vc_js_status',
    );
    $key_in = "'" . implode( "','", array_map( 'esc_sql', $keys ) ) . "'";
    $rows = $wpdb->get_results(
        "SELECT post_id, meta_key, meta_value FROM {$wpdb->postmeta}
         WHERE post_id IN ({$id_in}) AND meta_key IN ({$key_in})",
        ARRAY_A
    );
    foreach ( (array) $rows as $r ) {
        $pid = (int) $r['post_id'];
        $k = $r['meta_key'];
        if ( ! isset( $meta_map[ $pid ] ) ) { $meta_map[ $pid ] = array(); }
        if ( ! isset( $meta_map[ $pid ][ $k ] ) ) { $meta_map[ $pid ][ $k ] = $r['meta_value']; }
    }
    $er = $wpdb->get_col(
        "SELECT DISTINCT post_id FROM {$wpdb->postmeta}
         WHERE meta_key = '_elementor_data' AND post_id IN ({$id_in})
           AND meta_value IS NOT NULL AND meta_value <> '' AND meta_value <> '[]'
           AND CHAR_LENGTH(meta_value) > 2"
    );
    foreach ( (array) $er as $eid ) { $el_data[ (int) $eid ] = true; }
}

$out = array();
foreach ( $posts as $p ) {
    $pid = (int) $p['ID'];
    $m = isset( $meta_map[ $pid ] ) ? $meta_map[ $pid ] : array();
    $out[] = array(
        'ID'    => $pid,
        'title' => ( $p['post_title'] !== '' ? $p['post_title'] : '(no title)' ),
        'type'  => $p['post_type'],
        'sig'   => array(
            'elementor'      => isset($m['_elementor_edit_mode']) ? $m['_elementor_edit_mode'] : '',
            'elementor_data' => isset($el_data[$pid]) ? 1 : 0,
            'beaver'         => isset($m['_fl_builder_enabled']) ? $m['_fl_builder_enabled'] : '',
            'oxygen'         => ( isset($m['ct_builder_shortcodes']) && $m['ct_builder_shortcodes'] !== '' ) ? 1 : 0,
            'bricks'         => ( isset($m['_bricks_page_content_2']) && $m['_bricks_page_content_2'] !== '' ) ? 1 : 0,
            'divi_meta'      => isset($m['_et_pb_use_builder']) ? $m['_et_pb_use_builder'] : '',
            'vc_meta'        => isset($m['_wpb_vc_js_status']) ? $m['_wpb_vc_js_status'] : '',
            'has_vc'         => ( ! empty($p['has_vc']) ) ? 1 : 0,
            'has_etpb'       => ( ! empty($p['has_etpb']) ) ? 1 : 0,
            'has_block'      => ( ! empty($p['has_block']) ) ? 1 : 0,
        ),
    );
}
echo json_encode( $out );
"""


def list_posts(cfg, post_type, statuses):
    """List posts of a type, each tagged with the page builder it uses.

    Primary path uses one WordPress boot to gather cheap builder signals and
    maps them via detect_builder. If that runner can't run for any reason it
    falls back to a plain `wp post list` (builder unknown), so listing always
    works even on unusual setups.
    """
    try:
        rc, out, err = _run_php_with_json(
            cfg, _LIST_RUNNER_PHP,
            {"post_type": post_type, "statuses": list(statuses)}, timeout=300)
        if rc == 0:
            rows = json.loads(out.strip() or "[]")
            return [{"id": int(r["ID"]), "title": r.get("title") or "(no title)",
                     "type": r.get("type"), "builder": detect_builder(r.get("sig"))}
                    for r in rows]
    except Exception:
        pass  # fall through to the simple listing below

    rc, out, err = wp_run(cfg, [
        "post", "list",
        "--post_type=" + post_type,
        "--post_status=" + ",".join(statuses),
        "--posts_per_page=-1",
        "--orderby=title", "--order=ASC",
        "--fields=ID,post_title,post_type", "--format=json",
    ], timeout=300)
    if rc != 0:
        raise RuntimeError(err.strip() or "wp post list failed")
    rows = json.loads(out or "[]")
    return [{"id": int(r["ID"]), "title": r.get("post_title") or "(no title)",
             "type": r.get("post_type"), "builder": ""} for r in rows]


# Internal / non-content post types we never want to offer for rewriting.
# Anything public and not in this set (page, post, product, custom CPTs) is kept.
_SKIP_POST_TYPES = {
    "attachment", "revision", "nav_menu_item", "custom_css",
    "customize_changeset", "oembed_cache", "user_request",
    "wp_block", "wp_template", "wp_template_part", "wp_navigation",
    "wp_global_styles", "wp_font_family", "wp_font_face",
    "acf-field", "acf-field-group", "acf-post-type", "acf-taxonomy",
    "elementor_library", "e-landing-page", "elementor_font",
    "elementor_icons", "e-floating-buttons",
}


# Substring tokens (matched against both the slug and the label, lower-cased)
# that mark a page-builder template / widget / menu artifact rather than real
# editable content. Sites running ElementsKit, Royal Addons, Elementor, etc.
# register these as public, so they pollute the list. page / post / product are
# always exempt from this filter.
_SKIP_TYPE_TOKENS = (
    "template", "widget", "mega menu", "megamenu", "mega-menu",
    "library", "elementskit", "popup",
)
_ALWAYS_KEEP = ("page", "post", "product")


def list_post_types(cfg):
    """Registered, public, content-bearing post types on the site.

    Returns e.g. [{"name": "page", "label": "Pages"}, {"name": "post", ...}].
    'product' only appears if WooCommerce is active; genuine custom CPTs are
    included automatically. Internal types (menus, revisions, blocks, …) and
    page-builder template/widget types are filtered out. page + post are always
    guaranteed so the dropdown is usable even on unusual setups.
    """
    rc, out, err = wp_run(cfg, [
        "post-type", "list",
        "--fields=name,label,public",
        "--format=json",
    ], timeout=120)
    if rc != 0:
        raise RuntimeError(err.strip() or "wp post-type list failed")
    rows = json.loads(out or "[]")
    types, seen = [], set()
    for r in rows:
        name = (r.get("name") or "").strip()
        label = r.get("label") or name
        pub = r.get("public")
        is_public = pub is True or str(pub).strip().lower() in ("1", "true", "yes")
        if not name or not is_public or name in _SKIP_POST_TYPES or name in seen:
            continue
        if name not in _ALWAYS_KEEP:
            hay = (name + " " + str(label)).lower()
            if any(tok in hay for tok in _SKIP_TYPE_TOKENS):
                continue
        seen.add(name)
        types.append({"name": name, "label": label})
    for core_name, core_label in (("page", "Pages"), ("post", "Posts")):
        if core_name not in seen:
            seen.add(core_name)
            types.append({"name": core_name, "label": core_label})
    order = {"page": 0, "post": 1}
    types.sort(key=lambda t: (order.get(t["name"], 2), str(t["label"]).lower()))
    return types


# Batched read/write runners — one WordPress boot for the whole job instead of
# two SSH round-trips per post.
_READ_RUNNER_PHP = r"""<?php
if ( ! defined( 'WP_CLI' ) || ! WP_CLI ) { return; }
global $wpdb;
$ids = json_decode( file_get_contents( $args[0] ), true );
$out = array();
foreach ( (array) $ids as $id ) {
    $id = (int) $id;
    clean_post_cache( $id );
    $p = get_post( $id );
    if ( $p ) {
        // Read _elementor_data straight from the DB (mirrors get_post_meta's
        // first-row semantics) so a persistent object cache can't serve stale data.
        $elementor = $wpdb->get_var( $wpdb->prepare(
            "SELECT meta_value FROM {$wpdb->postmeta} WHERE post_id = %d AND meta_key = '_elementor_data' ORDER BY meta_id ASC LIMIT 1",
            $id
        ) );
        // Builder sources: Bricks / Beaver are stored as (PHP-serialized) arrays,
        // so read them via get_post_meta (unserialized) and ship as JSON. Oxygen
        // is a plain shortcode string.
        $bricks = get_post_meta( $id, '_bricks_page_content_2', true );
        $beaver = get_post_meta( $id, '_fl_builder_data', true );
        $out[] = array(
            'ID'        => $p->ID,
            'title'     => $p->post_title,
            'content'   => $p->post_content,
            'elementor' => ( null === $elementor ? '' : $elementor ),
            'url'       => get_permalink( $id ),
            'bricks'    => ( $bricks === '' || $bricks === null ) ? '' : wp_json_encode( $bricks ),
            'beaver'    => ( $beaver === '' || $beaver === null ) ? '' : wp_json_encode( $beaver ),
            'oxygen'    => (string) get_post_meta( $id, 'ct_builder_shortcodes', true ),
            'meta'      => array(
                '_elementor_edit_mode' => (string) get_post_meta( $id, '_elementor_edit_mode', true ),
                '_fl_builder_enabled'  => (string) get_post_meta( $id, '_fl_builder_enabled', true ),
                '_wpb_vc_js_status'    => (string) get_post_meta( $id, '_wpb_vc_js_status', true ),
                '_et_pb_use_builder'   => (string) get_post_meta( $id, '_et_pb_use_builder', true ),
            ),
        );
    }
}
echo json_encode( $out );
"""

_WRITE_RUNNER_PHP = r"""<?php
if ( ! defined( 'WP_CLI' ) || ! WP_CLI ) { return; }
$updates = json_decode( file_get_contents( $args[0] ), true );
$w = $GLOBALS['wpdb'];
$n = 0;
$elementor_touched = false;
$beaver_touched = false;
foreach ( (array) $updates as $id => $u ) {
    $id = (int) $id;
    $type = isset( $u['type'] ) ? $u['type'] : 'content';
    if ( $type === 'elementor' ) {
        update_post_meta( $id, '_elementor_data', wp_slash( $u['value'] ) );
        $elementor_touched = true;
    } elseif ( $type === 'meta' ) {
        update_post_meta( $id, $u['key'], wp_slash( $u['value'] ) );
    } elseif ( $type === 'bricks' ) {
        // Bricks stores its page content as an array; save it as one.
        update_post_meta( $id, '_bricks_page_content_2', json_decode( $u['value'], true ) );
    } elseif ( $type === 'beaver' ) {
        $data = get_post_meta( $id, '_fl_builder_data', true );
        if ( is_array( $data ) ) {
            foreach ( (array) $u['edits'] as $ed ) {
                $nid = (string) $ed['node']; $field = (string) $ed['field'];
                if ( isset( $data[ $nid ] ) && is_object( $data[ $nid ] ) && isset( $data[ $nid ]->settings ) ) {
                    $data[ $nid ]->settings->$field = $ed['value'];
                }
            }
            update_post_meta( $id, '_fl_builder_data', $data );
            $beaver_touched = true;
        }
    } else {
        $w->update( $w->posts, array( 'post_content' => $u['value'] ), array( 'ID' => $id ) );
    }
    clean_post_cache( $id );
    $n++;
}
if ( function_exists( 'wp_cache_flush' ) ) { wp_cache_flush(); }
if ( $elementor_touched && class_exists( '\\Elementor\\Plugin' ) ) {
    try { \Elementor\Plugin::$instance->files_manager->clear_cache(); } catch ( \Throwable $e ) {}
}
if ( $beaver_touched && class_exists( 'FLBuilderModel' ) && method_exists( 'FLBuilderModel', 'delete_asset_cache_for_post' ) ) {
    foreach ( (array) $updates as $id => $u ) {
        if ( ( isset( $u['type'] ) ? $u['type'] : '' ) === 'beaver' ) {
            try { FLBuilderModel::delete_asset_cache_for_post( (int) $id ); } catch ( \Throwable $e ) {}
        }
    }
}
echo "UPDATED $n";
"""


def _run_php_with_json(cfg, runner_php, payload, timeout=1800):
    runner = "/tmp/wprw-run-%d.php" % os.getpid()
    pf = "/tmp/wprw-pl-%d.json" % os.getpid()
    rc, _, err = ssh_run(cfg, "cat > " + shlex.quote(runner), stdin_data=runner_php)
    if rc != 0:
        return (rc, "", _clean_stderr(err) or "could not stage runner")
    rc, _, err = ssh_run(cfg, "cat > " + shlex.quote(pf), stdin_data=json.dumps(payload))
    if rc != 0:
        ssh_run(cfg, "rm -f " + shlex.quote(runner))
        return (rc, "", _clean_stderr(err) or "could not stage payload")
    rc, out, err = wp_run(cfg, ["eval-file", runner, pf], timeout=timeout)
    ssh_run(cfg, "rm -f " + shlex.quote(runner) + " " + shlex.quote(pf))
    return (rc, out, _clean_stderr(err))


def read_posts(cfg, ids):
    """Read title, content, Elementor data and other builder sources in one boot."""
    if not ids:
        return {}
    rc, out, err = _run_php_with_json(cfg, _READ_RUNNER_PHP, [int(i) for i in ids], timeout=600)
    if rc != 0:
        raise RuntimeError(err or "could not read posts")
    rows = json.loads(out.strip() or "[]")
    return {int(r["ID"]): {"id": int(r["ID"]), "title": r.get("title", ""),
                           "content": r.get("content", ""),
                           "elementor": r.get("elementor", ""), "url": r.get("url", ""),
                           "bricks": r.get("bricks", ""), "beaver": r.get("beaver", ""),
                           "oxygen": r.get("oxygen", ""), "meta": r.get("meta", {})}
            for r in rows}


def builder_of_post(post):
    """Detect the page builder for an already-read post (uses the same rules as
    the list badge). Falls back to 'classic' for plain HTML / unknown."""
    post = post or {}
    content = post.get("content") or ""
    meta = post.get("meta") or {}
    el = (post.get("elementor") or "").strip()
    sig = {
        "elementor": meta.get("_elementor_edit_mode", ""),
        "elementor_data": 0 if el in ("", "[]", "null") else 1,
        "beaver": meta.get("_fl_builder_enabled", ""),
        "oxygen": 1 if (post.get("oxygen") or "") != "" else 0,
        "bricks": 1 if (post.get("bricks") or "") not in ("", '""', "null", "false") else 0,
        "divi_meta": meta.get("_et_pb_use_builder", ""),
        "vc_meta": meta.get("_wpb_vc_js_status", ""),
        "has_vc": 1 if "[vc_" in content else 0,
        "has_etpb": 1 if "[et_pb_" in content else 0,
        "has_block": 1 if "<!-- wp:" in content else 0,
    }
    return detect_builder(sig)


def _apply_specs_to_doc(cfg, doc, rows, title, placeholders):
    """Run each row's selector against a builder doc's targets, rewrite the
    matched text with the AI, and set it back. Returns fields changed. A target
    already rewritten by an earlier row is not rewritten again."""
    import builders
    changed = 0
    done = set()
    for row in rows or []:
        sel = builders.parse_selector(row.get("xpath", ""))
        matches = [t for t in doc.targets if builders.spec_matches(sel, t)]
        for t in builders.select_targets(matches, sel["index"]):
            tid = id(t)
            if tid in done:
                continue
            cur = t.text
            if not (isinstance(cur, str) and cur.strip()):
                continue
            prompt = render_prompt(row.get("prompt", ""), None, title, placeholders, current_text=cur)
            if builders.has_link_tokens(cur):
                prompt += "\n\n" + builders.LINK_HINT
            reply = ai_complete(cfg, prompt)
            if reply is not None and t.set(reply):
                done.add(tid)
                changed += 1
    return changed


def _beaver_match(sel, nd):
    k = sel.get("kind")
    if k == "any":
        return True
    if k == "heading":
        return nd.get("kind") == "heading" and (
            sel.get("level") is None or (nd.get("level") or "").lower() == sel["level"])
    if k == "para":
        return nd.get("kind") == "para"
    if k == "button":
        return nd.get("kind") == "button"
    return False


def _process_beaver(cfg, post, rows, placeholders):
    """Beaver Builder: text lives in the (PHP-serialized) _fl_builder_data meta,
    shipped here as JSON by read_posts. We pick targets + rewrite in Python and
    return per-node edits; the write runner re-applies them to the real array."""
    import builders
    title = post.get("title", "")
    raw = post.get("beaver") or ""
    if raw in ("", '""', "null", "false"):
        return {"status": "skipped", "message": "no Beaver data on this page"}
    try:
        data = json.loads(raw)
    except Exception:
        return {"status": "error", "message": "could not parse Beaver data"}
    if not isinstance(data, dict):
        return {"status": "skipped", "message": "no matching text found"}
    fmap = json.loads(builders.BEAVER_FIELDS_JSON)
    nodes = []
    for nid, node in data.items():
        if not isinstance(node, dict):
            continue
        s = node.get("settings")
        if not isinstance(s, dict):
            continue
        spec = fmap.get(s.get("type", ""))
        if not spec:
            continue
        val = s.get(spec["field"])
        if not isinstance(val, str) or not val.strip():
            continue
        level = str(s.get("tag", "")) if spec["kind"] == "heading" else ""
        nodes.append({"node": str(nid), "field": spec["field"],
                      "kind": spec["kind"], "level": level, "text": val})
    done = set()
    edits = []
    for row in rows or []:
        sel = builders.parse_selector(row.get("xpath", ""))
        cand = [nd for nd in nodes if _beaver_match(sel, nd)]
        for nd in builders.select_targets(cand, sel["index"]):
            key = (nd["node"], nd["field"])
            if key in done:
                continue
            masked, links = builders.mask_links_inplace(nd["text"])
            prompt = render_prompt(row.get("prompt", ""), None, title, placeholders,
                                   current_text=masked)
            if builders.has_link_tokens(masked):
                prompt += "\n\n" + builders.LINK_HINT
            reply = ai_complete(cfg, prompt)
            if reply is not None:
                restored, ok = builders.restore_links(reply, links)
                if links and not ok:
                    continue  # keep original rather than drop a link
                edits.append({"node": nd["node"], "field": nd["field"], "value": restored})
                done.add(key)
    if not edits:
        return {"status": "skipped", "message": "no matching text found for those selectors"}
    return {"status": "updated", "message": "ready", "_kind": "beaver", "edits": edits}


def process_builder(cfg, post, builder, rows, placeholders):
    """Rewrite visible text for a non-Elementor builder page. Returns
    {status, message, _kind, ...writeback}. _kind tells app.py how to save:
    'content' (post_content), 'meta' (a meta key), 'bricks' (JSON->array meta),
    or 'beaver' (per-node edits)."""
    if post is None:
        return {"status": "error", "message": "post not found"}
    import builders
    title = post.get("title", "")
    try:
        if builder in ("wpbakery", "divi"):
            doc = builders.ShortcodeDoc(post.get("content", ""), builder)
            if not _apply_specs_to_doc(cfg, doc, rows, title, placeholders):
                return {"status": "skipped", "message": "no matching text found for those selectors"}
            return {"status": "updated", "message": "ready", "_kind": "content",
                    "content": doc.serialize()}
        if builder == "oxygen":
            doc = builders.ShortcodeDoc(post.get("oxygen", ""), "oxygen")
            if not _apply_specs_to_doc(cfg, doc, rows, title, placeholders):
                return {"status": "skipped",
                        "message": "no Oxygen text matched (this version may store text "
                                   "differently — Find & Replace still works on it)"}
            return {"status": "updated", "message": "ready", "_kind": "meta",
                    "meta_key": "ct_builder_shortcodes", "value": doc.serialize()}
        if builder == "bricks":
            doc = builders.BricksDoc(post.get("bricks", ""))
            if not doc.ok:
                return {"status": "error", "message": "could not parse Bricks data"}
            if not _apply_specs_to_doc(cfg, doc, rows, title, placeholders):
                return {"status": "skipped", "message": "no matching text found for those selectors"}
            return {"status": "updated", "message": "ready", "_kind": "bricks",
                    "value": doc.serialize()}
        if builder == "beaver":
            return _process_beaver(cfg, post, rows, placeholders)
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "message": str(exc).replace("\n", " ")}
    return {"status": "skipped", "message": "builder '%s' isn't supported for rewrite yet" % builder}


def write_posts(cfg, updates):
    """Write many posts in one boot. updates = {id: {'type': ...}} where type is
    'content'|'elementor'|'meta'|'bricks'|'beaver' with the matching fields."""
    if not updates:
        return {"rc": 0, "count": 0, "report": "Nothing to write."}
    payload = {}
    for k, v in updates.items():
        t = v.get("type", "content")
        u = {"type": t}
        if t == "meta":
            u["key"] = v["key"]
            u["value"] = v["value"]
        elif t == "beaver":
            u["edits"] = v["edits"]
        else:  # content, elementor, bricks
            u["value"] = v["value"]
        payload[str(int(k))] = u
    rc, out, err = _run_php_with_json(cfg, _WRITE_RUNNER_PHP, payload, timeout=1800)
    count = 0
    for ln in out.splitlines():
        if ln.startswith("UPDATED "):
            try:
                count = int(ln.split()[1])
            except (IndexError, ValueError):
                pass
    return {"rc": rc, "count": count, "report": (err or "")}


def process_one(cfg, post, rows, placeholders):
    """XPath + OpenAI for a single already-read post. No SSH. Returns
    {'status': updated|skipped|error, 'message', and 'content' when changed}."""
    if post is None:
        return {"status": "error", "message": "post not found"}
    import builders
    try:
        root = load_root(post.get("content", ""))
        title = post.get("title", "")
        changed = False
        for row in rows:
            expr = row.get("xpath", "")
            nodes = [n for n in (root.xpath(expr) if expr else []) if hasattr(n, "tag")]
            if not nodes:
                continue
            node = nodes[0]
            masked, links = builders.mask_links(inner_html(node))
            prompt = render_prompt(row.get("prompt", ""), root, title, placeholders,
                                   current_text=masked)
            if builders.has_link_tokens(masked):
                prompt += "\n\n" + builders.LINK_HINT
            reply = ai_complete(cfg, prompt)
            if reply is None:
                continue
            restored, ok = builders.restore_links(reply, links)
            if links and not ok:
                continue  # a link token was dropped — leave this node as-is
            set_node_content(node, restored)
            changed = True
        if not changed:
            return {"status": "skipped", "message": "no XPath matched"}
        return {"status": "updated", "message": "ready", "content": inner_html(root)}
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "message": str(exc).replace("\n", " ")}


# ---------------------------------------------------------------------------
# Elementor: detect pages whose visible text lives in _elementor_data and
# rewrite the text inside their widgets.
# ---------------------------------------------------------------------------
ELEMENTOR_TEXT_FIELDS = {
    "heading": ["title"],
    "text-editor": ["editor"],
    "button": ["text"],
    "icon-box": ["title_text", "description_text"],
    "image-box": ["title_text", "description_text"],
    "call-to-action": ["title", "description"],
    "testimonial": ["testimonial_content", "testimonial_name", "testimonial_job"],
    "blockquote": ["blockquote_content"],
    "alert": ["alert_title", "alert_description"],
    "counter": ["title"],
    "toggle": [],
}


def is_elementor(post):
    raw = ((post or {}).get("elementor") or "").strip()
    if not raw or raw in ("[]", "null"):
        return False
    try:
        data = json.loads(raw)
        return isinstance(data, list) and len(data) > 0
    except Exception:
        return False


def _allow(allowed, wt, settings):
    """allowed may be None (all), a set of widget types, or a predicate
    (wt, settings) -> bool. Used to filter which Elementor widgets are touched."""
    if allowed is None:
        return True
    if callable(allowed):
        try:
            return bool(allowed(wt, settings if isinstance(settings, dict) else {}))
        except Exception:
            return False
    return wt in allowed


def _is_table_field(field, val):
    """A text-editor whose HTML contains a <table> is a table, not a paragraph.
    We never treat it as rewritable text — flattening it would destroy the table."""
    return field == "editor" and isinstance(val, str) and "<table" in val.lower()


# --- accordion / toggle / FAQ support -------------------------------------
# FAQ-style widgets keep their items in a repeater inside the widget's settings
# (Elementor's accordion/toggle use "tabs" with tab_title/tab_content; add-ons
# such as Royal use their own names). We rewrite only the *answer* (content)
# field of each item — identified by key name AND value shape — and never the
# titles, icons, html-tag settings, ids, or tables.
_REPEATER_WIDGET_HINTS = ("accordion", "toggle", "faq")
_REPEATER_CONTENT_KEYS = ("content", "description", "desc", "editor", "text")
# Question/title field of an accordion item (Elementor native uses tab_title;
# FAQ plugins use title/question). Styling sub-fields like title_size,
# title_tag, heading_color are excluded by the style-hint list below.
_REPEATER_TITLE_KEYS = ("title", "question", "heading")
_TITLE_STYLE_HINTS = ("size", "tag", "color", "typography", "align", "html",
                      "weight", "spacing", "transform", "decoration", "style",
                      "width", "height", "position", "animation", "icon")


def _is_repeater_widget(wt):
    w = (wt or "").lower()
    return any(h in w for h in _REPEATER_WIDGET_HINTS)


def _is_content_field(key):
    k = (key or "").lower()
    if "title" in k or "tag" in k or "name" in k or k == "id" or k.endswith("_id"):
        return False
    return any(t in k for t in _REPEATER_CONTENT_KEYS)


def _is_title_field(key):
    k = (key or "").lower()
    if k == "id" or k.endswith("_id") or "name" in k:
        return False
    if any(h in k for h in _TITLE_STYLE_HINTS):
        return False
    return any(t in k for t in _REPEATER_TITLE_KEYS)


def _looks_like_prose(val):
    s = (val or "").strip()
    return s != "" and (" " in s or "<" in s or len(s) > 30)


def _is_paragraph_text(val):
    """Distinguish a paragraph from a heading by the value itself: headings are
    short even when punchy ('Need help? Call now!'); paragraphs are genuinely
    long or several full sentences. Used so prose lands on //p even when its
    field name leans 'title', without dragging short headings along."""
    text = re.sub(r"<[^>]+>", "", val or "").strip()
    if len(text) > 80:
        return True
    return len(re.findall(r"[.!?]\s+[A-Z]", text)) >= 2 and len(text) > 60


def _repeater_content_targets(settings):
    """(item_dict, field) for the answer/content field of each item in any
    repeater list inside this widget's settings. item dicts are live refs, so
    callers can rewrite in place. Tables are skipped."""
    out = []
    for val in settings.values():
        if isinstance(val, list) and val and all(isinstance(it, dict) for it in val):
            for item in val:
                for fk, fv in item.items():
                    if (_is_content_field(fk) and isinstance(fv, str)
                            and _looks_like_prose(fv) and "<table" not in fv.lower()):
                        out.append((item, fk))
    return out


def _repeater_title_targets(settings, level=None):
    """(item_dict, field) for the question/title field of each item in any
    repeater list inside this widget's settings. Live refs so callers rewrite
    in place. Styling sub-fields and tables are skipped. When a heading level is
    given, the questions are only collected if the accordion renders its titles
    at that level (its title-tag setting, or the h2 default when untagged)."""
    out = []
    if level is not None and _widget_heading_level(settings) != level:
        return out
    for val in settings.values():
        if isinstance(val, list) and val and all(isinstance(it, dict) for it in val):
            for item in val:
                for fk, fv in item.items():
                    if (_is_title_field(fk) and isinstance(fv, str)
                            and _looks_like_prose(fv) and "<table" not in fv.lower()):
                        out.append((item, fk))
    return out


# ---- Generic text detection for third-party widgets ----------------------
# Page builders add their own widgets (ElementsKit, Royal/wpr, etc.) whose
# types aren't in ELEMENTOR_TEXT_FIELDS. For those, pick out the real copy by
# key name + value shape: a title-like key feeds heading selectors (//h2), a
# content-like key feeds paragraph selectors (//p). Styling, colours, urls,
# css, numbers, ids, icons, etc. are skipped, and only prose-shaped values
# count — so a miss is far more likely than rewriting the wrong field.
_GENERIC_TITLE_KEYS = ("title", "heading", "subtitle", "subheading", "headline")
# Strong content words decide "body text" even when the key ALSO contains a
# title word (e.g. heading_description is the description, not a title).
_GENERIC_CONTENT_KEYS = ("description", "desc", "content", "editor", "paragraph",
                         "summary", "excerpt", "blurb", "intro", "body", "copy")
_GENERIC_SKIP_HINTS = ("size", "tag", "color", "colour", "typography", "align",
                       "html", "width", "height", "css", "class", "url", "link",
                       "media", "animation", "hover",
                       "background", "bg_", "border", "margin", "padding",
                       "position", "shadow", "spacing", "transform", "decoration",
                       "style", "format", "duration", "speed", "delay", "count",
                       "number", "num_", "value", "ratio", "level", "_id",
                       "anchor", "target", "rel", "nofollow", "placeholder",
                       "slug", "name", "date", "order", "weight", "gap",
                       "tooltip", "button", "btn", "cta", "label", "badge",
                       "caption")

# An icon class VALUE (fa/eicon/dashicons/etc.) — excluded by value, so that
# text fields named after a widget (image_box_title, icon_box_description) are
# still picked up. Anchored so real headings like "Iconic" aren't matched.
_ICON_CLASS_RE = re.compile(
    r"^\s*(fa[srlbd]?\s+fa-|fa-|e?icon-|dashicons-|flaticon-|ion-|bi-|ti-)[a-z0-9 _-]*$",
    re.I)

# //span targets inline sub-text — the subtitle / tagline / eyebrow line that
# builders render inside a <span> (e.g. ElementsKit's ekit-heading--subtitle),
# not the main heading or a paragraph. Keyed by name; styling sub-fields of a
# subtitle (subtitle_color, subtitle_typography, ...) are excluded.
_SUBTITLE_KEYS = ("subtitle", "sub_title", "subheading", "sub_heading",
                  "subtext", "sub_text", "tagline", "eyebrow", "overline",
                  "pretitle", "pre_title", "supertitle", "super_title",
                  "before_title", "after_title")
_SUBTITLE_STYLE = ("color", "colour", "typograph", "font", "css", "class",
                   "html", "align", "background", "bg_", "border", "shadow",
                   "spacing", "margin", "padding", "width", "height",
                   "animation", "icon", "url", "link", "position", "transform",
                   "decoration", "_id", "style")

# Common widget DEFAULT/demo strings that ship un-edited and shouldn't be
# rewritten (usually belong to disabled features like tooltips).
_DEFAULT_TEXT_MARKERS = ("tooltip content", "lorem ipsum", "your content goes here",
                         "add your heading text here", "enter your text here",
                         "click edit button to change this text", "i am text block",
                         "this is the heading", "type your text here",
                         "no headings were found")


def _is_default_placeholder(val):
    s = (val or "").strip().lower()
    return any(m in s for m in _DEFAULT_TEXT_MARKERS) or _is_lorem(s)


# Distinctive Latin words from Lorem-ipsum filler — none occur in real English
# copy, so 2+ of them means the value is placeholder text left in the data.
_LOREM_WORDS = frozenset((
    "lorem", "ipsum", "dolor", "amet", "consectetur", "consectetuer",
    "adipiscing", "adipiscings", "elit", "eiusmod", "tempor", "incididunt",
    "labore", "dolore", "aliqua", "aliquam", "accumsan", "posuere", "vulputate",
    "pellentesque", "vestibulum", "malesuada", "fringilla", "euismod",
    "tincidunt", "vivamus", "suspendisse", "condimentum", "sodales",
    "ullamcorper", "sagittis", "venenatis", "faucibus", "porttitor",
    "sollicitudin", "lacinia", "mauris", "egestas", "commodo", "gravida",
    "pharetra", "dignissim", "convallis", "laoreet", "vehicula", "scelerisque",
    "facilisis", "fermentum", "feugiat", "hendrerit", "imperdiet", "interdum",
    "lobortis", "luctus", "maecenas", "rhoncus", "rutrum", "tristique",
    "ultrices", "ultricies", "volutpat", "phasellus", "placerat", "tortor",
    "varius", "elementum", "nibh", "nisl", "pretium", "fusce", "morbi",
    "praesent", "proin", "curabitur", "aenean", "nullam", "eros", "felis",
    "etiam", "ligula", "metus", "augue", "turpis", "vitae",
))


def _is_lorem(val):
    text = re.sub(r"<[^>]+>", "", val or "").lower()
    words = re.findall(r"[a-z]+", text)
    if len(words) < 3:
        return False
    hits = sum(1 for w in words if w in _LOREM_WORDS)
    return hits >= 2 or (len(words) >= 5 and hits / len(words) > 0.25)


def _is_link_list(val):
    """True when a value is mostly a list of links (a product-category menu,
    button row, etc.) rather than real prose — so it isn't treated as a
    paragraph. A sentence with one or two inline links stays prose."""
    s = val or ""
    links = re.findall(r"<a\b[^>]*>(.*?)</a>", s, re.S | re.I)
    if len(links) < 2:
        return False
    link_text = "".join(re.sub(r"<[^>]+>", "", l) for l in links)
    full_text = re.sub(r"<[^>]+>", "", s)
    lt = len(re.sub(r"\s+", "", link_text))
    ft = len(re.sub(r"\s+", "", full_text))
    return ft > 0 and (lt / ft) > 0.6


def _generic_kind(key, val=""):
    k = (key or "").lower()
    if any(h in k for h in _GENERIC_SKIP_HINTS):
        return None
    if _is_subtitle_field(key):                       # subtitles belong to //span
        return None
    para = _is_paragraph_text(val)
    if any(t in k for t in _GENERIC_CONTENT_KEYS):   # strong content key wins
        return "content"
    if any(t in k for t in _GENERIC_TITLE_KEYS):
        return "content" if para else "title"        # long prose under a title-ish key is a paragraph
    if "text" in k:                                  # weak fallback: *_text = body/label text
        return "content"
    if para:                                         # unknown key, but clearly a paragraph
        return "content"
    return None


def _widget_title_tag(settings):
    """The HTML tag a widget renders its title in — h1..h6, span, div or p —
    read from the widget's tag/size control by value. Returns None when the
    page data carries no such control (Elementor omits a control left at its
    default). Lets //hN match real heading tags and //span match titles that
    render inline, e.g. an ElementsKit accordion question that comes out as
    <span class="ekit-accordion-title">."""
    if not isinstance(settings, dict):
        return None
    chosen = anyt = None
    for k, v in settings.items():
        if not isinstance(v, str):
            continue
        vs = v.strip().lower()
        if not re.fullmatch(r"h[1-6]|span|div|p", vs):
            continue
        kl = k.lower()
        is_tag = ("tag" in kl or "size" in kl)
        if is_tag and ("title" in kl or "head" in kl):
            return vs                      # most specific: the title's own tag
        if is_tag and chosen is None:
            chosen = vs
        elif anyt is None:
            anyt = vs
    return chosen or anyt


def _widget_heading_level(settings):
    """The widget's title tag only when it's a real heading (h1..h6); otherwise
    None — so a span/div title is not placed on a specific //hN."""
    t = _widget_title_tag(settings)
    return t if t in ("h1", "h2", "h3", "h4", "h5", "h6") else None


def _generic_text_targets(settings, want_title, want_content, level=None, wt=None):
    """(settings, field) for prose-shaped text in an unknown widget's top-level
    settings. Live refs so callers rewrite in place. Tables are skipped. When a
    heading level is given (//h2 etc.), a title is only collected if the widget
    renders at that level. Heading-type widgets (ekit-heading, advanced-heading,
    …) have their main text treated as a heading even when the field key isn't
    named like a title; their level is read from the tag control, defaulting to
    h2 when the widget carries no readable tag (Elementor's own heading default)."""
    out = []
    if not (want_title or want_content):
        return out
    widget_is_heading = _is_heading_widget(wt)
    raw_tag = _widget_title_tag(settings)                     # h1..h6/span/div/p or None
    wlevel = raw_tag if raw_tag in ("h1", "h2", "h3", "h4", "h5", "h6") else None
    if widget_is_heading and raw_tag is None:
        wlevel = "h2"                                         # heading widget, no tag control -> default
    for fk, fv in settings.items():
        if not isinstance(fv, str) or not fv.strip():
            continue
        if "<table" in fv.lower() or not _looks_like_prose(fv):
            continue
        if (_is_default_placeholder(fv) or _ICON_CLASS_RE.match(fv.strip())
                or _is_link_list(fv) or _is_breadcrumb_value(fv)):
            continue
        klow = fk.lower()
        # A field is heading text when its key is a title/heading key, OR the
        # whole widget is a heading widget (so its main text is a heading even
        # under an oddly-named field). It belongs on //hN only, never //p —
        # regardless of length or tag readability. Strong content keys
        # (…_description) and subtitles are excluded so paragraphs and //span
        # sub-text survive.
        is_content_key = any(t in klow for t in _GENERIC_CONTENT_KEYS)
        is_skip_key = any(h in klow for h in _GENERIC_SKIP_HINTS)
        is_title_key = any(t in klow for t in _GENERIC_TITLE_KEYS)
        is_heading_field = (
            not _is_subtitle_field(fk) and not is_content_key and not is_skip_key
            and (is_title_key or widget_is_heading)
        )
        if is_heading_field:
            if want_title and (level is None or wlevel == level):
                out.append((settings, fk))
            continue
        kind = _generic_kind(fk, fv)
        if kind == "title" and want_title:
            if level is None or wlevel == level:
                out.append((settings, fk))
        elif kind == "content" and want_content:
            out.append((settings, fk))
    return out


def _is_subtitle_field(key):
    """A subtitle/tagline text field (not its styling sub-fields)."""
    kl = (key or "").lower()
    return (any(s in kl for s in _SUBTITLE_KEYS)
            and not any(s in kl for s in _SUBTITLE_STYLE))


def _sub_ok(val):
    return (isinstance(val, str) and val.strip() and "<table" not in val.lower()
            and _looks_like_prose(val) and not _is_default_placeholder(val)
            and not _ICON_CLASS_RE.match(val.strip()) and not _is_link_list(val))


def _generic_sub_targets(settings):
    """(settings, field) for subtitle/tagline text in a widget's top-level
    settings — the inline sub-text that renders inside a <span> (//span)."""
    return [(settings, fk) for fk, fv in settings.items()
            if _is_subtitle_field(fk) and _sub_ok(fv)]


def _repeater_sub_targets(settings):
    """(item_dict, field) for subtitle/tagline text inside repeater items
    (slides, cards, etc.) — also reached by //span."""
    out = []
    for val in settings.values():
        if isinstance(val, list) and val and all(isinstance(it, dict) for it in val):
            for item in val:
                for fk, fv in item.items():
                    if _is_subtitle_field(fk) and _sub_ok(fv):
                        out.append((item, fk))
    return out


def _is_rewritable_text(key, val):
    """A settings value worth rewriting: prose-shaped, not a url/css/table, and
    not under an obviously non-text (style/url/id/number) key."""
    if not isinstance(val, str):
        return False
    s = val.strip()
    if not s or not _looks_like_prose(s) or "<table" in s.lower():
        return False
    if (_is_default_placeholder(s) or _ICON_CLASS_RE.match(s)
            or _is_link_list(s)):
        return False
    low = s.lower()
    if low.startswith(("http://", "https://", "www.", "#", "{", "[", "<svg",
                       "<iframe", "<style", "<script", "data:")):
        return False
    if "://" in low:
        return False
    if "{" in s and "}" in s and ":" in s:        # CSS / JSON-ish blob
        return False
    if (not _is_subtitle_field(key)
            and any(h in (key or "").lower() for h in _GENERIC_SKIP_HINTS)):
        return False
    return True


def _walk_settings_text(container, out):
    """Recurse a settings dict (and any nested dicts / repeater item dicts),
    collecting (container_dict, key) for every prose-shaped string."""
    if not isinstance(container, dict):
        return
    for k, v in container.items():
        if isinstance(v, str):
            if _is_rewritable_text(k, v) and not _is_breadcrumb_value(v):
                out.append((container, k))
        elif isinstance(v, dict):
            _walk_settings_text(v, out)
        elif isinstance(v, list):
            for it in v:
                if isinstance(it, dict):
                    _walk_settings_text(it, out)


# Navigation widgets whose text is chrome, not editable content — breadcrumbs
# render a trail like "Home > Disclaimer" that must never be AI-rewritten or
# matched by //hN / //*. Matched by substring so every builder's variant is
# covered: Elementor `breadcrumbs`, ElementsKit/Royal/EAEL `*-breadcrumbs`, etc.
_SKIP_WIDGET_TOKENS = ("breadcrumb",)


def _is_nav_widget(widget_type):
    wt = (widget_type or "")
    if not isinstance(wt, str):
        return False
    wt = wt.lower()
    return any(tok in wt for tok in _SKIP_WIDGET_TOKENS)


# A value that reads as a breadcrumb trail ("Home > Products > Flanges") even
# when it isn't inside a dedicated breadcrumb widget — e.g. hand-typed into a
# heading or text widget. Kept deliberately tight so real sentences are never
# caught: classic breadcrumb separators with a space on both sides, 2–6 short
# crumbs, none containing sentence punctuation, and a home-like first crumb.
_BREADCRUMB_SEP_RE = re.compile(r"\s+(?:>|»|›|→|❯|/)\s+")
_HOME_WORDS = {
    "home", "homepage", "inicio", "início", "accueil", "startseite",
    "start", "hem", "hjem", "主页", "首页", "ホーム", "होम",
}


def _is_breadcrumb_value(val):
    text = re.sub(r"<[^>]+>", " ", val or "")
    text = re.sub(r"\s+", " ", text).strip()
    if not text or len(text) > 120:
        return False
    parts = _BREADCRUMB_SEP_RE.split(text)
    if not (2 <= len(parts) <= 6):
        return False
    for p in parts:
        p = p.strip()
        if not p or len(p) > 40 or re.search(r"[.!?]", p):
            return False
    return parts[0].strip().lower() in _HOME_WORDS


# Add-on heading widgets (ElementsKit `ekit-heading`, Essential Addons
# `eael-advanced-heading`, Premium/Royal/etc.) carry "heading"/"headline" in
# their type. Their main text is a heading even when its field key isn't named
# like a title, so //hN should treat it as one.
_HEADING_WIDGET_TOKENS = ("heading", "headline")


def _is_heading_widget(widget_type):
    wt = (widget_type or "")
    if not isinstance(wt, str):
        return False
    return any(tok in wt.lower() for tok in _HEADING_WIDGET_TOKENS)


def _collect_all_elementor_text(node, out=None):
    """Deep, builder-agnostic scan (used by //*): collect every prose-shaped
    text value anywhere in the Elementor tree — any widget, any addon, any
    nesting depth. Live refs so the rewriter can write in place. Styling, urls,
    css, numbers and ids are skipped."""
    if out is None:
        out = []
    if isinstance(node, list):
        for it in node:
            _collect_all_elementor_text(it, out)
        return out
    if not isinstance(node, dict):
        return out
    if _is_nav_widget(node.get("widgetType")):
        kids = node.get("elements")
        if isinstance(kids, list):
            _collect_all_elementor_text(kids, out)
        return out
    settings = node.get("settings")
    if isinstance(settings, dict):
        _walk_settings_text(settings, out)
    kids = node.get("elements")
    if isinstance(kids, list):
        _collect_all_elementor_text(kids, out)
    return out


def _norm_text(s):
    """Normalize text so a widget's stored value can be matched against the
    page's rendered text: strip tags, unescape entities, collapse whitespace,
    lowercase."""
    if not isinstance(s, str):
        return ""
    t = re.sub(r"<[^>]+>", " ", s)
    t = html.unescape(t)
    return re.sub(r"\s+", " ", t).strip().lower()


def fetch_rendered_html(permalink, timeout=25):
    """GET the live page HTML. Returns the HTML string, or None when it can't be
    fetched (offline, blocked, non-200) so callers fall back to JSON detection."""
    if _requests is None or not permalink:
        return None
    try:
        resp = _requests.get(
            permalink, timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0 (compatible; wp-rewriter)"},
        )
    except Exception:  # noqa: BLE001
        return None
    if getattr(resp, "status_code", 0) == 200 and getattr(resp, "text", ""):
        return resp.text
    return None


def rendered_heading_sets(html_str):
    """{'h1'..'h6' -> set(normalized text)} of every heading actually rendered on
    the page — the ground truth for which text is an h2, h3, … no matter how the
    widget stored (or didn't store) its tag."""
    sets = {h: set() for h in ("h1", "h2", "h3", "h4", "h5", "h6")}
    if not html_str or _lxml_html is None:
        return sets
    try:
        doc = _lxml_html.fromstring(html_str)
    except Exception:  # noqa: BLE001
        return sets
    for h in sets:
        for el in doc.iter(h):
            txt = _norm_text(el.text_content())
            if txt:
                sets[h].add(txt)
    return sets


def _is_candidate_text_field(fk, fv):
    """A settings field that could hold display text (not styling/url/tag/number).
    Deliberately liberal — the rendered-tag match is what decides the level."""
    if not isinstance(fv, str):
        return False
    s = fv.strip()
    if not s or not re.search(r"[A-Za-z0-9]", s):
        return False
    if re.fullmatch(r"h[1-6]|span|div|p", s.lower()):
        return False
    if _ICON_CLASS_RE.match(s) or _is_link_list(s) or _is_breadcrumb_value(s):
        return False
    klow = (fk or "").lower()
    if any(h in klow for h in _GENERIC_SKIP_HINTS):
        return False
    return True


def _collect_heading_targets_rendered(elements, level_texts, out=None):
    """(container, field) for every widget text field whose rendered text is one
    of `level_texts` (the normalized texts that appear as <hN> on the live page).
    A field is treated as that heading only when the page really renders it so."""
    if out is None:
        out = []

    def walk(els):
        for el in els or []:
            if not isinstance(el, dict):
                continue
            if _is_nav_widget(el.get("widgetType")):
                walk(el.get("elements"))
                continue
            settings = el.get("settings")
            if isinstance(settings, dict):
                for fk, fv in settings.items():
                    if isinstance(fv, str):
                        if _is_candidate_text_field(fk, fv) and _norm_text(fv) in level_texts:
                            out.append((settings, fk))
                    elif isinstance(fv, list) and fv and all(isinstance(it, dict) for it in fv):
                        for item in fv:
                            for ik, iv in item.items():
                                if (isinstance(iv, str) and _is_candidate_text_field(ik, iv)
                                        and _norm_text(iv) in level_texts):
                                    out.append((item, ik))
            walk(el.get("elements"))

    walk(elements)
    return out


def _all_heading_texts(heading_sets):
    """Union of every rendered heading text across h1..h6 — used to keep anything
    that renders as a heading out of //p."""
    u = set()
    for s in (heading_sets or {}).values():
        u |= s
    return u


def _collect_elementor_targets(elements, allowed=None, out=None, rendered=None, exclude=None):
    """Ordered list of (container_dict, field_name) for every widget text field
    that matches the filter, in document order. Table widgets are skipped.
    Container dicts are live references so callers can read/rewrite in place.
    //p adds accordion/FAQ answers; //h2 adds FAQ questions; //* (match-all)
    deep-scans the whole tree for every text value on any widget/addon.
    When `rendered` (the set of normalized texts that appear as this heading tag
    on the live page) is given for an //hN selector, headings are matched against
    the page's real rendered tags instead of guessed from the JSON."""
    if out is None:
        out = []
    if allowed is None:
        return _collect_all_elementor_text(elements, out)
    kind = getattr(allowed, "kind", None)
    level = getattr(allowed, "level", None)
    if rendered is not None and level in ("h1", "h2", "h3", "h4", "h5", "h6"):
        # Ground truth: match widget text against the tags the page actually renders.
        return _collect_heading_targets_rendered(elements, rendered, out)
    want_repeater = kind == "para"
    want_question = kind == "heading"
    want_sub = kind == "span"
    for el in elements:
        if not isinstance(el, dict):
            continue
        wt = el.get("widgetType")
        if _is_nav_widget(wt):
            kids = el.get("elements")
            if isinstance(kids, list):
                _collect_elementor_targets(kids, allowed, out)
            continue
        settings = el.get("settings")
        if isinstance(settings, dict):
            handled = False
            if wt in ELEMENTOR_TEXT_FIELDS:
                handled = True
                if _allow(allowed, wt, settings):
                    for field in ELEMENTOR_TEXT_FIELDS[wt]:
                        val = settings.get(field)
                        if (isinstance(val, str) and val.strip()
                                and not _is_table_field(field, val)
                                and not _is_breadcrumb_value(val)):
                            out.append((settings, field))
            if _is_repeater_widget(wt):
                handled = True
                if want_repeater:
                    out.extend(_repeater_content_targets(settings))
                if want_question:
                    out.extend(_repeater_title_targets(settings, level))
            if want_sub:
                out.extend(_generic_sub_targets(settings))
                out.extend(_repeater_sub_targets(settings))
                if _is_repeater_widget(wt):
                    # Accordion/FAQ questions render inline (e.g. ElementsKit's
                    # <span class="ekit-accordion-title">); collect them on //span
                    # unless this accordion gives its titles a real heading tag.
                    if _widget_heading_level(settings) is None:
                        out.extend(_repeater_title_targets(settings, None))
                elif _widget_title_tag(settings) == "span":
                    out.extend(_generic_text_targets(settings, True, False, None))
            elif not handled:
                out.extend(_generic_text_targets(settings, want_question, want_repeater, level, wt))
        kids = el.get("elements")
        if isinstance(kids, list):
            _collect_elementor_targets(kids, allowed, out)
    if exclude:
        out[:] = [(s, f) for (s, f) in out if _norm_text(s.get(f)) not in exclude]
    return out


def _elementor_table_count(elements, allowed=None):
    """How many matching widgets were skipped because they contain a table."""
    n = 0
    for el in elements:
        if not isinstance(el, dict):
            continue
        wt = el.get("widgetType")
        settings = el.get("settings")
        if wt in ELEMENTOR_TEXT_FIELDS and isinstance(settings, dict) and _allow(allowed, wt, settings):
            for field in ELEMENTOR_TEXT_FIELDS[wt]:
                if _is_table_field(field, settings.get(field)):
                    n += 1
        kids = el.get("elements")
        if isinstance(kids, list):
            n += _elementor_table_count(kids, allowed)
    return n


def xpath_index(xpath):
    """Parse a trailing positional index from an XPath: //h2[3] -> 3,
    //h2[last()] -> 'last'. Text predicates like [contains(...)] -> None.
    On Elementor pages this selects the Nth matching widget in document order."""
    x = (xpath or "").strip()
    m = re.search(r"\[(\d+)\]\s*$", x)
    if m:
        return int(m.group(1))
    if re.search(r"\[\s*last\(\s*\)\s*\]\s*$", x):
        return "last"
    return None


def _select_targets(targets, index):
    """Pick target(s) for a 1-based positional index. None -> all."""
    if index is None:
        return list(targets)
    if index == "last":
        return list(targets[-1:])
    if 1 <= index <= len(targets):
        return [targets[index - 1]]
    return []


def _clean_widget_text(val):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", val)).strip()


def elementor_types_from_xpath(xpath):
    """Map an XPath's target tag to an Elementor widget filter, so //h2 only
    rewrites heading widgets whose HTML tag is h2 (Elementor stores the tag in
    'header_size', default h2). Returns None (all), a set, or a predicate."""
    x = (xpath or "").lower()
    m = re.search(r"h([1-6])", x)
    if m:
        level = "h" + m.group(1)

        def by_level(wt, s):
            return wt == "heading" and (s.get("header_size") or "h2").lower() == level

        by_level.kind = "heading"
        by_level.level = level
        return by_level
    if re.search(r"(/|//)p(\[|/|$)", x):
        def is_para(wt, s):
            if wt == "text-editor":
                return True
            return wt == "heading" and (s.get("header_size") or "h2").lower() == "p"
        is_para.kind = "para"
        return is_para
    if "button" in x or re.search(r"(/|//)a(\[|/|$)", x):
        return {"button"}
    if re.search(r"(/|//)span(\[|/|$)", x):
        def is_span(wt, s):
            return False                              # no native-widget match
        is_span.kind = "span"
        is_span.level = None
        return is_span
    return None


def process_one_elementor(cfg, post, specs, placeholders):
    """Rewrite text inside an Elementor page's widgets. `specs` is a list of
    {prompt, allowed, index} — one per XPath/prompt row — applied in order to the
    same page, so //p[1] and //p[4] (two rows) both run. With a positional index
    only the Nth matching widget in document order is rewritten. A field already
    rewritten by an earlier spec on this page is not rewritten again."""
    import builders
    try:
        data = json.loads(post.get("elementor") or "[]")
    except Exception:
        return {"status": "error", "message": "could not parse Elementor data"}
    title = post.get("title", "")
    done = set()            # (id(settings), field) already rewritten on this page
    count = 0
    matched_any = False
    indexed = False
    # If any row targets a heading level, read the live page's real heading tags
    # once and match widgets against them (ground truth), falling back to the
    # stored-data heuristic if the page can't be fetched.
    heading_sets = None
    _hlevels = ("h1", "h2", "h3", "h4", "h5", "h6")
    if any((getattr(s.get("allowed"), "level", None) in _hlevels
            or getattr(s.get("allowed"), "kind", None) == "para") for s in (specs or [])):
        html_str = fetch_rendered_html(post.get("url") or "")
        if html_str is not None:
            heading_sets = rendered_heading_sets(html_str)
    try:
        for spec in specs or []:
            idx = spec.get("index")
            if idx is not None:
                indexed = True
            allowed = spec.get("allowed")
            level = getattr(allowed, "level", None)
            kind = getattr(allowed, "kind", None)
            rendered = (heading_sets.get(level)
                        if (heading_sets is not None and level in _hlevels) else None)
            exclude = (_all_heading_texts(heading_sets)
                       if (heading_sets is not None and kind == "para") else None)
            targets = _select_targets(
                _collect_elementor_targets(data, allowed, rendered=rendered, exclude=exclude), idx)
            if targets:
                matched_any = True
            prompt_tpl = spec.get("prompt", "")
            for settings, field in targets:
                key = (id(settings), field)
                if key in done:
                    continue
                text = settings.get(field)
                if not isinstance(text, str) or not text.strip():
                    continue
                masked, links = builders.mask_links_inplace(text)
                prompt = render_prompt(prompt_tpl, None, title, placeholders,
                                       current_text=masked)
                if builders.has_link_tokens(masked):
                    prompt += "\n\n" + builders.LINK_HINT
                reply = ai_complete(cfg, prompt)
                if reply is not None:
                    restored, ok = builders.restore_links(reply, links)
                    if links and not ok:
                        continue  # a link token was dropped — keep original
                    settings[field] = restored
                    done.add(key)
                    count += 1
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "message": str(exc).replace("\n", " ")}
    if not count:
        if not matched_any and indexed:
            return {"status": "skipped",
                    "message": "no widget at the requested index on this page"}
        return {"status": "skipped", "message": "no matching text widgets"}
    return {"status": "updated", "message": "%d widget(s)" % count,
            "elementor": json.dumps(data, ensure_ascii=False)}


# ---------------------------------------------------------------------------
# Logo: upload a new file to the media library, and replace the logo everywhere
# (URL references via search-replace + the theme's Site Identity attachment).
# ---------------------------------------------------------------------------
def sftp_put_bytes(cfg, data_bytes, remote_path):
    if _paramiko is None:
        raise RuntimeError("The 'paramiko' package is not installed.")
    with _ssh_lock:
        client = _get_client(cfg)
        sftp = client.open_sftp()
        try:
            with sftp.open(remote_path, "wb") as fh:
                fh.write(data_bytes)
        finally:
            sftp.close()


def upload_logo(cfg, data_bytes, filename):
    """Push a file to the server and import it into the media library, keeping its original name."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "-", os.path.basename(filename or "")).lstrip("-") or "logo.png"
    tmpdir = "/tmp/wprw-up-%d" % os.getpid()
    remote = tmpdir + "/" + safe
    ssh_run(cfg, "mkdir -p " + shlex.quote(tmpdir))
    sftp_put_bytes(cfg, data_bytes, remote)
    rc, out, err = wp_run(cfg, ["media", "import", remote, "--porcelain"], timeout=300)
    ssh_run(cfg, "rm -rf " + shlex.quote(tmpdir))
    if rc != 0:
        raise RuntimeError(_clean_stderr(err) or "wp media import failed")
    att = (_clean_stderr(out).strip().splitlines() or ["0"])[-1].strip()
    try:
        aid = int(att)
    except ValueError:
        raise RuntimeError("media import did not return an attachment id: " + att)
    rc2, out2, _ = wp_run(cfg, ["eval", "echo wp_get_attachment_url(%d);" % aid], timeout=60)
    url = (_clean_stderr(out2).strip().splitlines() or [""])[-1].strip()
    return {"id": aid, "url": url}


_LOGO_FINALIZE_RUNNER_PHP = r"""<?php
if ( ! defined( 'WP_CLI' ) || ! WP_CLI ) { return; }
$cfg = json_decode( file_get_contents( $args[0] ), true );
$new_url = isset( $cfg['new_url'] ) ? $cfg['new_url'] : '';
$set_identity = ! empty( $cfg['set_identity'] );
if ( $set_identity && $new_url ) {
    $id = attachment_url_to_postid( $new_url );
    if ( $id ) {
        set_theme_mod( 'custom_logo', $id );
        echo "Site Identity logo set to attachment $id\n";
    } else {
        echo "Site Identity not changed (the new URL is not a media-library attachment)\n";
    }
}
if ( class_exists( '\\Elementor\\Plugin' ) ) {
    try { \Elementor\Plugin::$instance->files_manager->clear_cache(); echo "Elementor cache cleared\n"; } catch ( \Throwable $e ) {}
}
echo 'done';
"""


_LOGO_INSPECT_RUNNER_PHP = r"""<?php
if ( ! defined( 'WP_CLI' ) || ! WP_CLI ) { return; }
$cfg = json_decode( file_get_contents( $args[0] ), true );
$old_url = isset( $cfg['old_url'] ) ? trim( $cfg['old_url'] ) : '';
global $wpdb;
$out = array( 'theme' => wp_get_theme()->get( 'Name' ), 'custom_logo' => null, 'old_id' => null, 'url_hits' => array(), 'widgets' => array() );
$cl = get_theme_mod( 'custom_logo' );
if ( $cl ) { $out['custom_logo'] = array( 'id' => (int) $cl, 'url' => wp_get_attachment_url( $cl ) ); }

$variants = array();
if ( $old_url ) {
    $variants[ $old_url ] = true;
    $old_id = attachment_url_to_postid( $old_url );
    if ( $old_id ) {
        $out['old_id'] = (int) $old_id;
        $full = wp_get_attachment_url( $old_id );
        if ( $full ) { $variants[ $full ] = true; }
        $meta = wp_get_attachment_metadata( $old_id );
        $base = $full ? substr( $full, 0, strrpos( $full, '/' ) ) : '';
        if ( ! empty( $meta['sizes'] ) && $base ) {
            foreach ( $meta['sizes'] as $sz ) { if ( ! empty( $sz['file'] ) ) { $variants[ $base . '/' . $sz['file'] ] = true; } }
        }
    }
    foreach ( array_keys( $variants ) as $v ) {
        $like = '%' . $wpdb->esc_like( $v ) . '%';
        $p  = (int) $wpdb->get_var( $wpdb->prepare( "SELECT COUNT(*) FROM {$wpdb->posts} WHERE post_content LIKE %s", $like ) );
        $pm = (int) $wpdb->get_var( $wpdb->prepare( "SELECT COUNT(*) FROM {$wpdb->postmeta} WHERE meta_value LIKE %s", $like ) );
        $o  = (int) $wpdb->get_var( $wpdb->prepare( "SELECT COUNT(*) FROM {$wpdb->options} WHERE option_value LIKE %s", $like ) );
        if ( $p || $pm || $o ) { $out['url_hits'][] = array( 'url' => $v, 'post_content' => $p, 'postmeta' => $pm, 'options' => $o ); }
    }
}

$rows = $wpdb->get_results( "SELECT post_id, meta_value FROM {$wpdb->postmeta} WHERE meta_key='_elementor_data' AND meta_value LIKE '%logo%'" );
$widgets = array();
$walk = function( $node, $post_id ) use ( &$walk, &$widgets ) {
    if ( ! is_array( $node ) ) { return; }
    if ( isset( $node['widgetType'] ) && stripos( $node['widgetType'], 'logo' ) !== false ) {
        $widgets[] = array( 'post_id' => (int) $post_id, 'title' => get_the_title( $post_id ), 'widget' => $node['widgetType'], 'settings' => isset( $node['settings'] ) && is_array( $node['settings'] ) ? $node['settings'] : array() );
    }
    foreach ( $node as $v ) { if ( is_array( $v ) ) { $walk( $v, $post_id ); } }
};
foreach ( $rows as $r ) { $d = json_decode( $r->meta_value, true ); if ( is_array( $d ) ) { $walk( $d, (int) $r->post_id ); } }
$out['widgets'] = $widgets;
echo json_encode( $out );
"""


def inspect_logo(cfg, old_url=""):
    rc, out, err = _run_php_with_json(cfg, _LOGO_INSPECT_RUNNER_PHP, {"old_url": old_url or ""}, timeout=300)
    raw = _clean_stderr(out).strip()
    try:
        info = json.loads(raw or "{}")
    except Exception:
        return {"report": "Could not read the database.\n" + (raw or err or "")}
    L = ["Theme: %s" % (info.get("theme") or "?")]
    cl = info.get("custom_logo")
    L.append("Site Identity logo (custom_logo): " + ("id %s, url %s" % (cl.get("id"), cl.get("url")) if cl else "not set"))
    if old_url:
        oid = info.get("old_id")
        L.append("Old logo attachment id: %s" % (oid if oid else "not found for that URL"))
        hits = info.get("url_hits") or []
        L.append("")
        if hits:
            L.append("Old logo URL found in the database (covers every editor):")
            for h in hits:
                where = ", ".join("%s %d" % (k.replace("_", " "), h[k]) for k in ("post_content", "postmeta", "options") if h.get(k))
                L.append("  %s  \u2192  %s" % (h["url"], where))
        else:
            L.append("Old logo URL is NOT stored anywhere as text \u2014 it's referenced only by attachment id "
                     "(Site Identity or a builder widget), which is why URL swaps report 0.")
    else:
        L.append("(type the Old logo URL above and click again to search the whole database)")
    widgets = info.get("widgets") or []
    if widgets:
        L.append("")
        L.append("Elementor logo widget(s):")
        for w in widgets:
            L.append("  Template \u201c%s\u201d (post id %s) \u2014 %s" %
                     (w.get("title") or "(untitled)", w.get("post_id"), w.get("widget")))
            s = w.get("settings") or {}
            shown = False
            for k, v in s.items():
                kl = str(k).lower()
                if isinstance(v, dict) and ("url" in v or "id" in v):
                    L.append("    %s \u2192 id %s, url %s" % (k, v.get("id"), v.get("url")))
                    shown = True
                elif isinstance(v, str) and v and any(t in kl for t in ("logo", "image", "type", "source", "svg")):
                    L.append("    %s \u2192 %s" % (k, v))
                    shown = True
            if not shown:
                L.append("    setting keys: " + ", ".join(map(str, s.keys())))
    return {"report": "\n".join(L)}


_LOGO_REPOINT_RUNNER_PHP = r"""<?php
if ( ! defined( 'WP_CLI' ) || ! WP_CLI ) { return; }
$cfg = json_decode( file_get_contents( $args[0] ), true );
$old_url = isset( $cfg['old_url'] ) ? $cfg['old_url'] : '';
$new_url = isset( $cfg['new_url'] ) ? $cfg['new_url'] : '';
$new_id = attachment_url_to_postid( $new_url );
if ( ! $new_id ) { echo "Repoint skipped: the new logo URL is not a media-library attachment.\n"; echo 'done'; return; }
$canon = wp_get_attachment_url( $new_id );
if ( ! $canon ) { $canon = $new_url; }
$old_id = $old_url ? attachment_url_to_postid( $old_url ) : 0;
global $wpdb;
$rows = $wpdb->get_results( "SELECT post_id, meta_value FROM {$wpdb->postmeta} WHERE meta_key='_elementor_data' AND meta_value LIKE '%logo%'" );
$cnt = 0; $posts = 0;
$walk = function( &$node, $in_logo ) use ( &$walk, $new_id, $canon, $old_id, &$cnt ) {
    if ( ! is_array( $node ) ) { return; }
    $here = $in_logo;
    if ( isset( $node['widgetType'] ) && stripos( $node['widgetType'], 'logo' ) !== false ) { $here = true; }
    // Elementor image control = array with both 'id' and 'url'. Repoint it when it sits
    // inside a logo widget, or when it points at the old logo attachment.
    if ( array_key_exists( 'id', $node ) && array_key_exists( 'url', $node ) && ! is_array( $node['id'] ) ) {
        $match = $here || ( $old_id && (int) $node['id'] === (int) $old_id );
        if ( $match && ( (int) $node['id'] !== (int) $new_id || (string) $node['url'] !== (string) $canon ) ) {
            $node['id']  = $new_id;
            $node['url'] = $canon;
            $cnt++;
        }
    }
    foreach ( $node as $k => &$v ) { if ( is_array( $v ) ) { $walk( $v, $here ); } }
    unset( $v );
};
foreach ( $rows as $r ) {
    $data = json_decode( $r->meta_value, true );
    if ( ! is_array( $data ) ) { continue; }
    $before = $cnt;
    $walk( $data, false );
    if ( $cnt > $before ) {
        update_post_meta( $r->post_id, '_elementor_data', wp_slash( wp_json_encode( $data ) ) );
        $posts++;
    }
}
if ( class_exists( '\\Elementor\\Plugin' ) ) {
    try { \Elementor\Plugin::$instance->files_manager->clear_cache(); } catch ( \Throwable $e ) {}
}
echo "Repointed $cnt logo image reference(s) in $posts Elementor template(s)/page(s) to the new logo.\n";
echo 'done';
"""


_FAVICON_RUNNER_PHP = r"""<?php
if ( ! defined( 'WP_CLI' ) || ! WP_CLI ) { return; }
$cfg = json_decode( file_get_contents( $args[0] ), true );
$id = isset( $cfg['id'] ) ? (int) $cfg['id'] : 0;
if ( ! $id ) { echo "ERROR: no attachment id"; return; }
update_option( 'site_icon', $id );
if ( function_exists( 'wp_cache_flush' ) ) { wp_cache_flush(); }
echo "Favicon set to attachment $id";
"""


def upload_favicon(cfg, data_bytes, filename):
    """Import an image and set it as the WordPress site icon (favicon)."""
    res = upload_logo(cfg, data_bytes, filename)  # push to server + wp media import -> {id, url}
    aid = res["id"]
    rc, out, err = _run_php_with_json(cfg, _FAVICON_RUNNER_PHP, {"id": aid}, timeout=120)
    msg = _clean_stderr(out).strip()
    if rc != 0 and not msg:
        raise RuntimeError(_clean_stderr(err) or "could not set the favicon")
    return {"id": aid, "url": res["url"], "report": msg or ("Favicon set to attachment %d" % aid)}


def replace_logo(cfg, old_url, new_url, set_identity=True):
    parts = []
    if old_url:
        res = run_replace(cfg, [(old_url, new_url)], scope="prefix", apply=True,
                          smart_case=False, include_guid=True)
        parts.append("Replaced the logo URL across the database:\n" + res["report"])
    # Repoint logo widgets / old-logo references that store an attachment, not a URL string.
    rc, out, err = _run_php_with_json(cfg, _LOGO_REPOINT_RUNNER_PHP,
                                      {"old_url": old_url, "new_url": new_url}, timeout=600)
    rep = _clean_stderr(out).replace("done", "").strip()
    if rep:
        parts.append(rep)
    rc, out, err = _run_php_with_json(cfg, _LOGO_FINALIZE_RUNNER_PHP,
                                      {"new_url": new_url, "set_identity": bool(set_identity)}, timeout=300)
    extra = _clean_stderr(out).replace("done", "").strip()
    if extra:
        parts.append(extra)
    elif err:
        parts.append(err)
    return {"report": "\n\n".join(p for p in parts if p) or "Done."}


def case_variants(old, new):
    """Expand a pair into common capitalizations, derived from the *lowercased*
    terms so the result is identical no matter how the search box was typed, and
    each matched capitalization maps to the same capitalization of NEW:
        myanmar -> india,  Myanmar -> India,  MYANMAR -> INDIA
    """
    o, n = old.lower(), new.lower()

    def title(s):
        return re.sub(r"(^|[\s\-_])([a-z])", lambda m: m.group(1) + m.group(2).upper(), s)

    def sentence(s):
        return s[:1].upper() + s[1:] if s else s

    forms = [
        (o, n),                      # lower:    myanmar -> india
        (o.upper(), n.upper()),      # UPPER:    MYANMAR -> INDIA
        (title(o), title(n)),        # Title:    Myanmar -> India (each word / slug part)
        (sentence(o), sentence(n)),  # Sentence: first letter only
    ]
    seen = {}
    for a, b in forms:
        if a and a not in seen and a != b:
            seen[a] = b
    return list(seen.items())


def expand_pairs(pairs, smart_case):
    if not smart_case:
        return [(o, n) for o, n in pairs if o and o != n]
    out = {}
    for o, n in pairs:
        for vo, vn in case_variants(o, n):
            if vo not in out:
                out[vo] = vn
    return list(out.items())


def slugify(s):
    """URL/filename slug: lowercase, runs of non-alphanumerics -> a single hyphen."""
    return re.sub(r"[^a-z0-9]+", "-", (s or "").strip().lower()).strip("-")


_SLUG_SEP_L = ("-", "_", "/")
_SLUG_SEP_R = ("-", "_", ".", "/")


def _url_forms(o):
    """The shapes a term can take inside URLs / media filenames: the hyphen slug,
    the spaced form, and the underscore form. Covers links a previous run may have
    created with a space in them (before slugging was fixed)."""
    lo = (o or "").strip().lower()
    forms = []
    for f in (slugify(o), lo, lo.replace(" ", "_")):
        if f and f not in forms:
            forms.append(f)
    return forms


def _title_each(s):
    """Capitalize each word in a slug: 'new-zealand' -> 'New-Zealand'."""
    return re.sub(r"[A-Za-z]+", lambda m: m.group(0).capitalize(), s)


def _slug_case_pairs(base, sn):
    """Case-preserving (old_form -> new_slug) variants so a Title-Case link stays
    Title-Case after the swap: lower->lower, UPPER->UPPER, Title-Each->Title-Each.
    e.g. base='new-zealand', sn='india' ->
      ('new-zealand','india'), ('NEW-ZEALAND','INDIA'), ('New-Zealand','India')."""
    return [(base, sn), (base.upper(), sn.upper()), (_title_each(base), _title_each(sn))]


def _url_slug_pairs(pairs):
    """Bounded (-, _, /, .) variants that match the search term *inside a URL or
    media filename* — in hyphen, spaced, or underscore form, in any letter case —
    and replace it with the URL slug of NEW while KEEPING the case pattern:
      New Zealand -> India :  …-New-Zealand.jpg  -> …-India.jpg
                              …-new-zealand.jpg  -> …-india.jpg
                              …-NEW-ZEALAND.jpg  -> …-INDIA.jpg
    The boundaries keep these from ever matching prose."""
    out, seen = [], set()
    for o, n in pairs:
        sn = slugify(n)
        if not sn:
            continue
        for base in _url_forms(o):
            for fo, fn in _slug_case_pairs(base, sn):
                if fo == fn:
                    continue
                for L in _SLUG_SEP_L:
                    for R in _SLUG_SEP_R:
                        a = L + fo + R
                        if a not in seen:
                            seen.add(a)
                            out.append((a, L + fn + R))
    return out


def _table_prefix(cfg):
    rc, out, _ = wp_run(cfg, ["config", "get", "table_prefix"], timeout=30)
    return out.strip() if rc == 0 and out.strip() else "wp_"


# Runner executed once via `wp eval-file`; loops search-replace in-process so the
# WordPress stack boots a single time for the whole job instead of once per pair.
# Single-pass replacement: scans each table ONCE and applies ALL find/replace
# pairs per row (instead of one full scan per pair). Serialized values are
# unserialized, replaced, and re-serialized so byte-lengths stay correct;
# values containing PHP objects are left untouched for safety.
_REPLACE_RUNNER_PHP = r"""<?php
if ( ! defined( 'WP_CLI' ) || ! WP_CLI ) { return; }
$spec      = json_decode( file_get_contents( $args[0] ), true );
$search    = isset( $spec['search'] )  ? $spec['search']  : array();
$replace   = isset( $spec['replace'] ) ? $spec['replace'] : array();
$scope     = isset( $spec['scope'] )   ? $spec['scope']   : 'prefix';
$skip_guid = ! empty( $spec['skip_guid'] );
$skip_logs = ! empty( $spec['skip_logs'] );
$dry       = ! empty( $spec['dry_run'] );
$batch     = isset( $spec['batch'] ) ? max( 25, (int) $spec['batch'] ) : 50;
global $wpdb;
$prefix = $wpdb->prefix;

function wprw_walk( $search, $replace, $data ) {
    if ( is_array( $data ) ) {
        $out = array();
        foreach ( $data as $k => $v ) {
            $nk = is_string( $k ) ? str_replace( $search, $replace, $k ) : $k;
            $out[ $nk ] = wprw_walk( $search, $replace, $v );
        }
        return $out;
    }
    if ( is_string( $data ) ) { return str_replace( $search, $replace, $data ); }
    return $data;
}
function wprw_apply( $search, $replace, $value, &$skipped ) {
    if ( ! is_string( $value ) || $value === '' ) { return array( $value, false ); }
    if ( is_serialized( $value ) ) {
        if ( strpos( $value, 'O:' ) !== false ) { $skipped++; return array( $value, false ); }
        $un = @unserialize( $value );
        if ( $un === false && $value !== 'b:0;' ) {
            $new = str_replace( $search, $replace, $value );
            return array( $new, $new !== $value );
        }
        $mod = wprw_walk( $search, $replace, $un );
        $new = serialize( $mod );
        if ( @unserialize( $new ) === false && $new !== 'b:0;' ) { return array( $value, false ); }
        return array( $new, $new !== $value );
    }
    $new = str_replace( $search, $replace, $value );
    return array( $new, $new !== $value );
}

$text_types = array( 'char', 'varchar', 'tinytext', 'text', 'mediumtext', 'longtext' );
$all = $wpdb->get_col( 'SHOW TABLES' );
$tables = array();
foreach ( $all as $t ) {
    if ( $scope === 'posts' ) { if ( $t === $wpdb->posts ) { $tables[] = $t; } continue; }
    if ( $scope === 'prefix' && strpos( $t, $prefix ) !== 0 ) { continue; }
    if ( $skip_logs && strpos( $t, $prefix ) === 0 ) {
        $base = substr( $t, strlen( $prefix ) );
        if ( preg_match( '/^(wsal_|wf|e_submissions|e_events|actionscheduler_|wpil_)/', $base ) ) { continue; }
    }
    $tables[] = $t;
}

$report = array();
$skipped = 0;
$errors = 0;
$skipped_tables = 0;
foreach ( $tables as $table ) {
    $cols_info = $wpdb->get_results( "SHOW COLUMNS FROM `$table`" );
    $text_cols = array(); $pk = null; $pk_count = 0;
    foreach ( $cols_info as $ci ) {
        $type = strtolower( preg_replace( '/\(.*$/', '', $ci->Type ) );
        if ( in_array( $type, $text_types, true ) ) {
            if ( $skip_guid && $ci->Field === 'guid' ) { continue; }
            $text_cols[] = $ci->Field;
        }
        if ( $ci->Key === 'PRI' ) { $pk = $ci->Field; $pk_count++; }
    }
    if ( ! $text_cols || $pk_count !== 1 || ! $pk ) { $skipped_tables++; continue; }

    $select = array( "`$pk`" );
    foreach ( $text_cols as $c ) { if ( $c !== $pk ) { $select[] = "`$c`"; } }
    $select_sql = implode( ', ', $select );

    $last = null;
    while ( true ) {
        if ( $last === null ) {
            $rows = $wpdb->get_results( "SELECT $select_sql FROM `$table` ORDER BY `$pk` ASC LIMIT $batch", ARRAY_A );
        } else {
            $rows = $wpdb->get_results( $wpdb->prepare( "SELECT $select_sql FROM `$table` WHERE `$pk` > %s ORDER BY `$pk` ASC LIMIT $batch", $last ), ARRAY_A );
        }
        if ( empty( $rows ) ) { break; }
        foreach ( $rows as $row ) {
            $last = $row[ $pk ];
            $update = array();
            foreach ( $text_cols as $c ) {
                if ( ! isset( $row[ $c ] ) || $row[ $c ] === null || $row[ $c ] === '' ) { continue; }
                list( $new, $changed ) = wprw_apply( $search, $replace, $row[ $c ], $skipped );
                if ( $changed ) {
                    $update[ $c ] = $new;
                    if ( ! isset( $report[ $table ][ $c ] ) ) { $report[ $table ][ $c ] = 0; }
                    $report[ $table ][ $c ]++;
                }
            }
            if ( $update && ! $dry ) {
                $ok = $wpdb->update( $table, $update, array( $pk => $row[ $pk ] ) );
                if ( $ok === false ) { $errors++; }
            }
        }
        if ( count( $rows ) < $batch ) { break; }
    }
}

if ( ! $dry && function_exists( 'wp_cache_flush' ) ) { wp_cache_flush(); }

$total = 0; $rows_out = array();
foreach ( $report as $table => $cols ) {
    foreach ( $cols as $col => $cnt ) { $rows_out[] = $table . "\t" . $col . "\t" . $cnt; $total += $cnt; }
}
echo json_encode( array(
    'total'           => $total,
    'rows'            => $rows_out,
    'skipped_objects' => $skipped,
    'tables_scanned'  => count( $tables ),
    'tables_skipped'  => $skipped_tables,
    'errors'          => $errors,
    'dry'             => $dry,
) );
"""


def _clean_stderr(text):
    """Drop harmless server PHP-startup noise (e.g. a missing ionCube CLI loader)
    so it doesn't look like the replacement failed."""
    keep = []
    for line in text.splitlines():
        low = line.lower()
        if "ioncube" in low:
            continue
        if "failed loading" in low and ".so" in low:
            continue
        if "cannot open shared object file" in low:
            continue
        keep.append(line)
    return "\n".join(keep).strip()


def _run_replace_batch(cfg, spec):
    """Stage the single-pass runner + spec on the server, run it, return raw output."""
    runner = "/tmp/wprw-replace-%d.php" % os.getpid()
    specf = "/tmp/wprw-spec-%d.json" % os.getpid()
    rc, _, err = ssh_run(cfg, "cat > " + shlex.quote(runner), stdin_data=_REPLACE_RUNNER_PHP)
    if rc != 0:
        return {"rc": rc, "out": "", "err": _clean_stderr(err) or "could not stage the runner on the server"}
    rc, _, err = ssh_run(cfg, "cat > " + shlex.quote(specf), stdin_data=json.dumps(spec))
    if rc != 0:
        ssh_run(cfg, "rm -f " + shlex.quote(runner))
        return {"rc": rc, "out": "", "err": _clean_stderr(err) or "could not stage the job on the server"}
    rc, out, err = wp_run(cfg, ["eval-file", runner, specf], timeout=3600)
    ssh_run(cfg, "rm -f " + shlex.quote(runner) + " " + shlex.quote(specf))
    return {"rc": rc, "out": _clean_stderr(out), "err": _clean_stderr(err)}


def run_replace(cfg, pairs, scope="prefix", apply=False, smart_case=False, include_guid=False, skip_logs=True):
    orig = list(pairs)
    pairs = expand_pairs(pairs, smart_case)
    if not pairs:
        return {"rc": 0, "report": "Nothing to replace."}
    # When URLs are in scope, fix media links to slug form FIRST — before the
    # literal word replacement would leave a space (e.g. '…-united states.jpg').
    # These pairs are bounded by -, _, /, . so they only hit URLs/filenames.
    if include_guid:
        slug_pairs = _url_slug_pairs(orig)
        if slug_pairs:
            keys = {o for o, _ in slug_pairs}
            pairs = slug_pairs + [(o, n) for o, n in pairs if o not in keys]
    spec = {
        "search": [o for o, n in pairs],
        "replace": [n for o, n in pairs],
        "scope": scope,
        "skip_guid": not include_guid,
        "skip_logs": bool(skip_logs) and scope != "posts",
        "dry_run": not apply,
        "batch": 50,
    }
    res = _run_replace_batch(cfg, spec)
    shown = ", ".join("%s\u2192%s" % (o, n) for o, n in pairs[:10])
    if len(pairs) > 10:
        shown += ", \u2026"
    header = "Applying %d replacement(s) in one pass: %s\n%s" % (len(pairs), shown, "\u2500" * 30)
    out = res.get("out", "")
    try:
        data = json.loads(out[out.index("{"): out.rindex("}") + 1])
    except (ValueError, json.JSONDecodeError):
        body = res.get("err") or out or "the replace runner returned no output"
        return {"rc": res.get("rc", 1) or 1, "report": header + "\n" + body}
    lines = [header]
    if data.get("dry"):
        lines.append("(Preview — no changes written.)")
    if data.get("rows"):
        lines.append("Table\tColumn\tReplacements")
        lines += data["rows"]
    lines.append("\u2500" * 30)
    lines.append("Total: %d replacement(s); %d table(s) scanned." % (data.get("total", 0), data.get("tables_scanned", 0)))
    if data.get("tables_skipped"):
        lines.append("(%d table(s) without a single primary-key column were skipped.)" % data["tables_skipped"])
    if data.get("skipped_objects"):
        lines.append("(%d serialized-object value(s) left untouched for safety.)" % data["skipped_objects"])
    if data.get("errors"):
        lines.append("(%d row(s) failed to update.)" % data["errors"])
    if apply and not data.get("dry"):
        lines.append("Object cache flushed.")
    return {"rc": res.get("rc", 0), "report": "\n".join(lines)}


def _uploads_basedir(cfg):
    rc, out, err = wp_run(cfg, ["eval", 'echo wp_upload_dir()["basedir"];'])
    out = _clean_stderr(out)
    if rc != 0 or not out.strip():
        raise RuntimeError(_clean_stderr(err) or "could not determine the uploads directory")
    lines = [ln for ln in out.splitlines() if ln.strip()]
    return (lines[-1] if lines else out).strip()


# Renames every matching upload in ONE server-side PHP pass (one process using
# rename(), instead of spawning thousands of `mv` commands). Scans first, then
# renames, and never overwrites an existing file.
_RENAME_RUNNER_PHP = r"""<?php
if ( ! defined( 'WP_CLI' ) || ! WP_CLI ) { return; }
$spec  = json_decode( file_get_contents( $args[0] ), true );
$pairs = isset( $spec['pairs'] ) ? $spec['pairs'] : array();
$apply = ! empty( $spec['apply'] );
$u = wp_upload_dir();
$base = isset( $u['basedir'] ) ? $u['basedir'] : '';
if ( ! $base || ! is_dir( $base ) ) { echo json_encode( array( 'error' => 'uploads directory not found' ) ); return; }
$todo = array();
$scanned = 0;
$it = new RecursiveIteratorIterator(
    new RecursiveDirectoryIterator( $base, FilesystemIterator::SKIP_DOTS ),
    RecursiveIteratorIterator::LEAVES_ONLY
);
foreach ( $it as $fi ) {
    if ( ! $fi->isFile() ) { continue; }
    $scanned++;
    $name = $fi->getFilename();
    $new  = $name;
    foreach ( $pairs as $p ) {
        $o = (string) $p[0]; $n = (string) $p[1];
        if ( $o !== '' && strpos( $new, $o ) !== false ) { $new = str_replace( $o, $n, $new ); }
    }
    if ( $new !== $name ) { $todo[] = array( $fi->getPathname(), $fi->getPath() . '/' . $new, $name, $new ); }
}
$examples = array();
foreach ( array_slice( $todo, 0, 50 ) as $t ) { $examples[] = $t[2] . ' -> ' . $t[3]; }
$renamed = 0; $failed = 0;
if ( $apply ) {
    foreach ( $todo as $t ) {
        if ( ! file_exists( $t[1] ) && @rename( $t[0], $t[1] ) ) { $renamed++; }
        else { $failed++; }
    }
}
echo json_encode( array(
    'basedir'  => $base,
    'scanned'  => $scanned,
    'matched'  => count( $todo ),
    'renamed'  => $renamed,
    'failed'   => $failed,
    'examples' => $examples,
) );
"""


def rename_media(cfg, pairs, apply=False, smart_case=False):
    """Rename media files whose name contains the search term, so URLs rewritten
    by search-replace still resolve. The scan and the renames run in a single
    server-side PHP pass (fast even for tens of thousands of files)."""
    # Filenames are URL slugs; rename any form of the term (hyphen/space/underscore,
    # any letter case) to the slug of NEW while KEEPING the case pattern, so a
    # Title-Case file 'Bars-in-New-Zealand.jpg' becomes 'Bars-in-India.jpg' and
    # matches the URL update above.
    slug_pairs, seen = [], set()
    for o, n in pairs:
        sn = slugify(n)
        if not sn:
            continue
        for base in _url_forms(o):
            for fo, fn in _slug_case_pairs(base, sn):
                if fo != fn and fo not in seen:
                    seen.add(fo)
                    slug_pairs.append((fo, fn))
    if not slug_pairs:
        return {"rc": 0, "report": "No find/replace pairs to apply to media."}
    spec = {"pairs": [[o, n] for o, n in slug_pairs], "apply": bool(apply)}
    runner = "/tmp/wprw-rename-%d.php" % os.getpid()
    specf = "/tmp/wprw-rename-spec-%d.json" % os.getpid()
    rc, _, err = ssh_run(cfg, "cat > " + shlex.quote(runner), stdin_data=_RENAME_RUNNER_PHP)
    if rc != 0:
        return {"rc": rc, "report": _clean_stderr(err) or "could not stage the rename runner on the server"}
    rc, _, err = ssh_run(cfg, "cat > " + shlex.quote(specf), stdin_data=json.dumps(spec))
    if rc != 0:
        ssh_run(cfg, "rm -f " + shlex.quote(runner))
        return {"rc": rc, "report": _clean_stderr(err) or "could not stage the rename job on the server"}
    rc, out, err = wp_run(cfg, ["eval-file", runner, specf], timeout=1800)
    ssh_run(cfg, "rm -f " + shlex.quote(runner) + " " + shlex.quote(specf))
    out = _clean_stderr(out)
    try:
        data = json.loads(out[out.index("{"): out.rindex("}") + 1])
    except (ValueError, json.JSONDecodeError):
        return {"rc": rc or 1, "report": _clean_stderr(err) or out or "the rename runner returned no output"}
    if data.get("error"):
        return {"rc": 1, "report": data["error"]}

    lines = ["Uploads directory: %s" % data.get("basedir", ""),
             "Scanned %d file(s); %d filename(s) contain the search term." %
             (data.get("scanned", 0), data.get("matched", 0))]
    if not data.get("matched"):
        lines.append("Nothing to rename — the search term is not in any media filename, so the image files "
                     "keep their names. (Image URLs inside content were already updated by the replacement "
                     "above; the pictures themselves don't change.)")
        return {"rc": 0, "report": "\n".join(lines)}

    lines.append("")
    lines += ["  %s" % e for e in data.get("examples", [])]
    extra = data.get("matched", 0) - len(data.get("examples", []))
    if extra > 0:
        lines.append("  ... and %d more" % extra)

    if apply:
        renamed, failed = data.get("renamed", 0), data.get("failed", 0)
        if failed:
            lines.append("\nRenamed %d file(s); %d could not be moved (a name collision, or a file-permission "
                         "issue on the uploads folder). Database references were already updated by search-replace."
                         % (renamed, failed))
        else:
            lines.append("\nRenamed %d file(s). Database references were already updated by search-replace, so the "
                         "URLs now resolve." % renamed)
        return {"rc": 1 if failed else 0, "report": "\n".join(lines)}

    lines.append("\n(Preview — switch to Apply to rename the files.)")
    return {"rc": 0, "report": "\n".join(lines)}
def _looks_like_full_page_xpath(xpath):
    x = xpath.strip().lower()
    return x.startswith("/html") or x.startswith("/body") or x.startswith("html/") or x.startswith("body/")


def _content_summary(root):
    tags = Counter(el.tag for el in root.iter() if isinstance(el.tag, str) and el is not root)
    if not tags:
        return "(this post's content is empty)"
    return ", ".join("%d <%s>" % (count, tag) for tag, count in tags.most_common(12))


def elementor_sample_texts(elementor_json, allowed=None, rendered=None, exclude=None):
    """Ordered, cleaned text of every Elementor widget the filter matches,
    so Fetch sample / Test prompt reflect reality on Elementor pages."""
    try:
        data = json.loads(elementor_json or "[]")
    except Exception:
        return []
    return [_clean_widget_text(s[f])
            for (s, f) in _collect_elementor_targets(data, allowed, rendered=rendered, exclude=exclude)]


def fetch_sample(cfg, post_id, xpath):
    """Public wrapper: attach the page's permalink so the UI can show a Sample
    page link, then return the matched-text result."""
    res = _fetch_sample(cfg, post_id, xpath)
    if isinstance(res, dict):
        res.setdefault("url", post_permalink(cfg, post_id))
    return res


def _fetch_sample(cfg, post_id, xpath):
    if not xpath:
        return {"ok": False, "message": "Provide an XPath expression."}
    if _looks_like_full_page_xpath(xpath):
        return {"ok": False, "message": (
            "That's a full-page XPath copied from the browser (it starts at /html/body). "
            "This tool edits the post's stored content, which doesn't include the theme's "
            "<html>/<body> or layout wrappers — so /html/body/div[...] can't match. Use a "
            "relative path that targets the content itself, e.g. //p, //h2, //p[1], or "
            "//p[contains(., 'some text from it')].")}
    data = read_posts(cfg, [post_id]).get(int(post_id), {})
    elementor = (data.get("elementor", "") or "").strip()

    # Elementor page: show the widget text the rewriter actually targets.
    if elementor not in ("", "[]"):
        allowed = elementor_types_from_xpath(xpath)
        index = xpath_index(xpath)
        rendered = None
        exclude = None
        rnote = ""
        level = getattr(allowed, "level", None)
        kind = getattr(allowed, "kind", None)
        if level in ("h1", "h2", "h3", "h4", "h5", "h6") or kind == "para":
            html_str = fetch_rendered_html(data.get("url") or post_permalink(cfg, post_id))
            if html_str is not None:
                hsets = rendered_heading_sets(html_str)
                if level in ("h1", "h2", "h3", "h4", "h5", "h6"):
                    rendered = hsets.get(level, set())
                elif kind == "para":
                    exclude = _all_heading_texts(hsets)
            elif level in ("h1", "h2", "h3", "h4", "h5", "h6"):
                rnote = ("\n\n(couldn't open the live page to read real tags, so this used the "
                         "stored data instead — check the page is public and reachable)")
        texts = elementor_sample_texts(elementor, allowed, rendered=rendered, exclude=exclude)
        try:
            skipped = _elementor_table_count(json.loads(elementor), allowed)
        except Exception:
            skipped = 0
        note = (("" if not skipped else
                "\n\n(%d table(s) on this page are skipped — tables aren't rewritten so their "
                "structure stays intact; edit those in Elementor directly)" % skipped) + rnote)
        if index is not None:
            picked = _select_targets(texts, index)
            if picked:
                return {"ok": True, "message": picked[0][:500] + note}
            return {"ok": False, "message": (
                "This Elementor page has %d matching widget(s), so [%s] is out of range. "
                "Remove the index to target all, or pick 1–%d."
                % (len(texts), index, len(texts) or 1)) + note}
        if texts:
            if len(texts) == 1:
                return {"ok": True, "message": texts[0][:500] + note}
            lines = ["%d. %s" % (i + 1, t[:160]) for i, t in enumerate(texts[:12])]
            extra = "" if len(texts) <= 12 else ("\n…and %d more" % (len(texts) - 12))
            msg = ("%d matching widget(s) on this page — add an index like [2] to target one:\n\n%s%s"
                   % (len(texts), "\n".join(lines), extra))
            return {"ok": True, "message": msg + note}
        return {"ok": False, "message": (
            "This is an Elementor page and no matching widget was found for that XPath. "
            "Use //h1–//h6 for headings, //p for text, //span for subtitles/taglines, "
            "//a for buttons, or //* for all widgets." + note)}

    # Classic post_content page.
    root = load_root(data.get("content", ""))
    nodes = [n for n in root.xpath(xpath) if hasattr(n, "tag")]
    if nodes:
        return {"ok": True, "message": nodes[0].text_content()[:500]}
    raw = root.xpath(xpath)
    if raw:
        return {"ok": True, "message": node_text(raw[0])[:500]}
    return {"ok": False, "message": (
        "No match. This post's content contains: %s. Use a relative path like //p or //h2 "
        "(add [1], [2]… to pick one)." % _content_summary(root))}


def post_permalink(cfg, pid):
    """Public URL of a post, or '' if it can't be resolved."""
    try:
        rc, out, _ = wp_run(cfg, ["eval", "echo get_permalink(%d);" % int(pid)], timeout=60)
        return out.strip() if rc == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


def test_prompt(cfg, post_id, xpath, prompt, placeholders):
    data = read_posts(cfg, [post_id]).get(int(post_id), {})
    title = data.get("title", "")
    url = post_permalink(cfg, post_id)
    elementor = (data.get("elementor", "") or "").strip()

    # Elementor page: preview against the widget text the rewriter targets.
    if elementor not in ("", "[]"):
        allowed = elementor_types_from_xpath(xpath)
        index = xpath_index(xpath)
        rendered = None
        exclude = None
        level = getattr(allowed, "level", None)
        kind = getattr(allowed, "kind", None)
        if level in ("h1", "h2", "h3", "h4", "h5", "h6") or kind == "para":
            html_str = fetch_rendered_html(data.get("url") or url)
            if html_str is not None:
                hsets = rendered_heading_sets(html_str)
                if level in ("h1", "h2", "h3", "h4", "h5", "h6"):
                    rendered = hsets.get(level, set())
                elif kind == "para":
                    exclude = _all_heading_texts(hsets)
        texts = elementor_sample_texts(elementor, allowed, rendered=rendered, exclude=exclude)
        picked = _select_targets(texts, index)
        cur = picked[0] if picked else ""
        rendered = render_prompt(prompt, None, title, placeholders, current_text=cur)
        evaluations = {}
        if "{{text}}" in prompt:
            evaluations["{{text}}"] = cur[:200]
        if "{{title}}" in prompt:
            evaluations["{{title}}"] = title[:200]
        try:
            reply = ai_complete(cfg, rendered)
            return {"ok": True, "url": url, "prompt": rendered, "evaluations": evaluations, "message": reply}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "url": url, "prompt": rendered, "evaluations": evaluations, "message": str(exc)}

    # Classic post_content page.
    root = load_root(data.get("content", ""))
    evaluations = {}
    rendered = prompt
    for key, expr in (placeholders or {}).items():
        nodes = root.xpath(expr) if expr else []
        val = node_text(nodes[0]) if nodes else ""
        evaluations["{{" + key.strip("{}") + "}}"] = val[:200]
        rendered = rendered.replace("{{" + key.strip("{}") + "}}", val)
    # built-in placeholders
    matched = [n for n in (root.xpath(xpath) if xpath else []) if hasattr(n, "tag")]
    cur = node_text(matched[0]) if matched else ""
    h1 = root.xpath("//h1")
    h1v = node_text(h1[0]) if h1 else ""
    rendered = rendered.replace("{{text}}", cur).replace("{{h1}}", h1v).replace("{{title}}", title)
    for key, val in (("{{text}}", cur), ("{{title}}", title), ("{{h1}}", h1v)):
        if key in prompt:
            evaluations[key] = val[:200]
    try:
        reply = ai_complete(cfg, rendered)
        return {"ok": True, "url": url, "prompt": rendered, "evaluations": evaluations, "message": reply}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "url": url, "prompt": rendered, "evaluations": evaluations, "message": str(exc)}


def process_post(cfg, pid, rows, placeholders, dry_run=False):
    """Returns {'status': 'updated'|'preview'|'skipped'|'error', 'message': str}."""
    try:
        data = read_post(cfg, pid)
        root = load_root(data.get("post_content", ""))
        title = data.get("post_title", "")
        changed = False
        for row in rows:
            expr = row.get("xpath", "")
            nodes = [n for n in (root.xpath(expr) if expr else []) if hasattr(n, "tag")]
            if not nodes:
                continue
            prompt = render_prompt(row.get("prompt", ""), root, title, placeholders)
            reply = ai_complete(cfg, prompt)
            set_node_content(nodes[0], reply)
            changed = True
        if not changed:
            return {"status": "skipped", "message": "no XPath matched"}
        if dry_run:
            return {"status": "preview", "message": "would update"}
        write_post(cfg, pid, inner_html(root))
        return {"status": "updated", "message": "saved"}
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "message": str(exc).replace("\n", " ")}