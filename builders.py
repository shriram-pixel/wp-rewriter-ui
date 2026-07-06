"""Page-builder adapters (v2, multi-builder rewrite).

Pure parsing/edit logic with no SSH/AI/config dependencies, so it can be unit
tested against fixtures without a live WordPress. core.py wires these into the
rewrite pipeline (AI in the middle, SSH read/write around the edges).

Each adapter exposes a document object with:
  .targets  -> list of _Tgt (each has .kind, .level, .text, and .set(new))
  .serialize() -> the new source string after edits

Design guarantees:
  * Shortcode edits are surgical: only the exact attribute value or the inner
    HTML of an *edited* block is changed; every other byte is preserved.
  * Attribute encoding (HTML entities vs rawurlencode) is detected on read and
    reproduced on write, so we never change a value's storage format.
"""

import re
import json
import html
import urllib.parse

import lxml.html as _lh


# --------------------------------------------------------------------------
# tiny HTML helpers (mirror core.load_root / inner_html / node_text / set_node)
# --------------------------------------------------------------------------
def _load_root(content):
    return _lh.fragment_fromstring(content or "", create_parent="div")


def _inner_html(root):
    parts = [root.text or ""]
    for child in root:
        parts.append(_lh.tostring(child, encoding="unicode"))
    return "".join(parts)


def _node_text(node):
    return node.text_content() if hasattr(node, "text_content") else str(node)


def _strip_html(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()


def _set_node_content(node, reply):
    for child in list(node):
        node.remove(child)
    node.text = None
    if re.sub(r"<[^>]+>", "", reply) == reply:
        node.text = reply
    else:
        frag = _lh.fragment_fromstring(reply, create_parent="span")
        node.text = frag.text
        for child in frag:
            node.append(child)


# --------------------------------------------------------------------------
# Link preservation: rewriting flattens a node to plain text, which drops any
# inline <a> links. To keep them, we swap each <a>…</a> for a token (⟦Ln⟧)
# before the AI sees the text, then restore the original anchors afterwards.
# The URL never reaches the model, so it can't be changed or dropped.
# --------------------------------------------------------------------------
_LINK_RE = re.compile(r"<a\b[^>]*>.*?</a>", re.I | re.S)
_TOK_OPEN = "\u27e6"   # ⟦
_TOK_CLOSE = "\u27e7"  # ⟧
_TOK_RE = re.compile(_TOK_OPEN + r"L(\d+)" + _TOK_CLOSE)

LINK_HINT = ("Some words are replaced with markers like " + _TOK_OPEN + "L0" + _TOK_CLOSE +
             ". Keep every marker exactly as written, once each, in a natural spot — "
             "they are links. Do not add, remove, renumber, or alter them.")


def mask_links(fragment):
    """Replace <a>…</a> in an HTML fragment with ⟦Ln⟧ tokens and reduce the rest
    to plain text. Returns (masked_plaintext, links)."""
    links = []

    def repl(m):
        links.append(m.group(0))
        return _TOK_OPEN + "L" + str(len(links) - 1) + _TOK_CLOSE
    masked = _LINK_RE.sub(repl, fragment or "")
    text = re.sub(r"<[^>]+>", "", masked)
    text = html.unescape(text)
    return re.sub(r"[ \t\r\n]+", " ", text).strip(), links


def mask_links_inplace(s):
    """Tokenise <a>…</a> but leave all other HTML untouched — used for fields
    whose surrounding markup (paragraphs, bold, lists) must be preserved
    (Elementor text-editor, Beaver rich-text). Returns (masked, links)."""
    links = []

    def repl(m):
        links.append(m.group(0))
        return _TOK_OPEN + "L" + str(len(links) - 1) + _TOK_CLOSE
    return _LINK_RE.sub(repl, s or ""), links


def restore_links(reply, links):
    """Put the original anchors back in place of their tokens. Returns
    (result, all_present) — all_present is False if the model dropped a token."""
    if not links:
        return reply, True
    seen = set(int(m.group(1)) for m in _TOK_RE.finditer(reply))
    result = _TOK_RE.sub(
        lambda m: links[int(m.group(1))] if int(m.group(1)) < len(links) else "", reply)
    return result, all(i in seen for i in range(len(links)))


def has_link_tokens(text):
    return bool(text) and (_TOK_OPEN + "L") in text


def node_inner_html(node):
    """Inner HTML of an lxml element (its text plus serialized children)."""
    parts = [node.text or ""]
    for child in node:
        parts.append(_lh.tostring(child, encoding="unicode"))
    return "".join(parts)


# --------------------------------------------------------------------------
# selector parsing (shared by all non-Elementor adapters)
# --------------------------------------------------------------------------
def _sel_index(xpath):
    x = (xpath or "").strip()
    m = re.search(r"\[(\d+)\]\s*$", x)
    if m:
        return int(m.group(1))
    if re.search(r"\[\s*last\(\s*\)\s*\]\s*$", x):
        return "last"
    return None


def parse_selector(xpath):
    """Interpret a selector for the non-Elementor adapters.
    Returns {'kind': 'heading'|'para'|'button'|'any', 'level': 'h2'|None,
    'index': int|'last'|None}."""
    x = (xpath or "").lower().strip()
    idx = _sel_index(xpath)
    m = re.search(r"h([1-6])", x)
    if m:
        return {"kind": "heading", "level": "h" + m.group(1), "index": idx}
    if re.search(r"(/|//)p(\[|/|$)", x):
        return {"kind": "para", "level": None, "index": idx}
    if "button" in x or re.search(r"(/|//)(a|button)(\[|/|$)", x):
        return {"kind": "button", "level": None, "index": idx}
    return {"kind": "any", "level": None, "index": idx}


def select_targets(targets, index):
    """Filter a target list by a 1-based positional index (None -> all)."""
    if index is None:
        return list(targets)
    if index == "last":
        return list(targets[-1:])
    if 1 <= index <= len(targets):
        return [targets[index - 1]]
    return []


def spec_matches(sel, tgt):
    """Does target `tgt` match parsed selector `sel` (kind/level)?"""
    kind = sel.get("kind")
    if kind == "any":
        return True
    if kind == "heading":
        return tgt.kind == "heading" and (sel.get("level") is None or tgt.level == sel["level"])
    if kind == "para":
        return tgt.kind == "para"
    if kind == "button":
        return tgt.kind == "button"
    return False


# --------------------------------------------------------------------------
# target wrapper
# --------------------------------------------------------------------------
class _Tgt:
    __slots__ = ("kind", "level", "_get", "_set")

    def __init__(self, kind, level, get, set_):
        self.kind = kind
        self.level = level
        self._get = get
        self._set = set_

    @property
    def text(self):
        return self._get()

    def set(self, new):
        return self._set(new)


# --------------------------------------------------------------------------
# attribute encoding helpers
# --------------------------------------------------------------------------
def _looks_urlencoded(v):
    return bool(re.search(r"%[0-9A-Fa-f]{2}", v)) and \
        urllib.parse.unquote(v) != v and html.unescape(v) == v


def _decode_attr(raw, enc):
    if enc == "url":
        return urllib.parse.unquote(raw)
    return html.unescape(raw)


def _encode_attr(new, quote, enc):
    if enc == "url":
        return urllib.parse.quote(new, safe="")
    s = new.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    if quote == '"':
        s = s.replace('"', "&quot;")
    elif quote == "'":
        s = s.replace("'", "&#039;")
    return s


_ATTR_RE = re.compile(
    r"""([\w\-]+)\s*=\s*("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|[^\s\]]+)"""
)


def _parse_attrs(source, astart, aend):
    """Parse attributes in source[astart:aend]; spans are absolute into source."""
    out = {}
    seg = source[astart:aend]
    for m in _ATTR_RE.finditer(seg):
        name = m.group(1).lower()
        raw = m.group(2)
        s, e = m.start(2), m.end(2)
        if raw and raw[0] in "\"'":
            q = raw[0]
            val = raw[1:-1]
            vs, ve = s + 1, e - 1
        else:
            q = ""
            val = raw
            vs, ve = s, e
        enc = "url" if _looks_urlencoded(val) else "html"
        out.setdefault(name, {"val": val, "vs": astart + vs, "ve": astart + ve, "q": q, "enc": enc})
    return out


def _find_open_tags(source, tag):
    out = []
    for m in re.finditer(r"\[" + re.escape(tag) + r"(\s[^\]]*)?\]", source):
        if m.group(1):
            out.append((m.start(1), m.end(1)))
        else:
            out.append((m.end() - 1, m.end() - 1))
    return out


def _find_blocks(source, tag):
    """Find [tag ...]inner[/tag] blocks (nearest matching close)."""
    res = []
    close = "[/" + tag + "]"
    for m in re.finditer(r"\[" + re.escape(tag) + r"(\s[^\]]*)?\]", source):
        inner_start = m.end()
        ci = source.find(close, inner_start)
        res.append({"inner_start": inner_start, "inner_end": ci if ci != -1 else inner_start})
    return res


def _level_from(attrs, level_attr, default="h2"):
    if not level_attr or level_attr not in attrs:
        return default
    m = re.search(r"h[1-6]", (attrs[level_attr]["val"] or "").lower())
    return m.group(0) if m else default


# builder -> [(tag, text_attr, kind, level_attr_or_None)]
_SC_ATTR = {
    "wpbakery": [
        ("vc_custom_heading", "text", "heading", "font_container"),
        ("vc_btn", "title", "button", None),
    ],
    "divi": [
        ("et_pb_heading", "title", "heading", "title_level"),
        ("et_pb_blurb", "title", "heading", "header_level"),
        ("et_pb_cta", "title", "heading", "header_level"),
        ("et_pb_button", "button_text", "button", None),
        ("et_pb_toggle", "title", "heading", None),
        ("et_pb_accordion_item", "title", "heading", None),
        ("et_pb_slide", "heading", "heading", None),
    ],
    "oxygen": [],
}

# builder -> [tags whose inner content is real HTML to parse]
_SC_INNER = {
    "wpbakery": ["vc_column_text"],
    "divi": ["et_pb_text", "et_pb_cta"],
    "oxygen": ["ct_text_block", "ct_headline", "oxy-rich_text", "ct_code_block"],
}


class ShortcodeDoc:
    """Adapter for shortcode-based builders (WPBakery, Divi, Oxygen)."""

    def __init__(self, source, builder):
        self.source = source or ""
        self.builder = builder
        self.targets = []
        self._edits = []      # (start, end, new_raw)
        self._blocks = []     # {start,end,root,dirty}
        self._build()

    def _add_attr_target(self, kind, level, a):
        def _get(raw=self.source[a["vs"]:a["ve"]], enc=a["enc"]):
            return _decode_attr(raw, enc)

        def _set(new, vs=a["vs"], ve=a["ve"], q=a["q"], enc=a["enc"]):
            self._edits.append((vs, ve, _encode_attr(new, q, enc)))
            return True
        self.targets.append(_Tgt(kind, level, _get, _set))

    def _add_inner_block(self, istart, iend):
        root = _load_root(self.source[istart:iend])
        blk = {"start": istart, "end": iend, "root": root, "dirty": False}
        self._blocks.append(blk)
        for node in root.iter():
            tag = getattr(node, "tag", None)
            if not isinstance(tag, str):
                continue
            tag = tag.lower()
            if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
                kind, level = "heading", tag
            elif tag == "p":
                kind, level = "para", None
            elif tag in ("a", "button"):
                kind, level = "button", None
            else:
                continue
            holder = {"links": []}

            def _get(node=node, holder=holder):
                masked, links = mask_links(node_inner_html(node))
                holder["links"] = links
                return masked

            def _set(new, node=node, blk=blk, holder=holder):
                restored, ok = restore_links(new, holder["links"])
                if holder["links"] and not ok:
                    return False  # a link token was dropped — keep original, never lose a link
                _set_node_content(node, restored)
                blk["dirty"] = True
                return True
            self.targets.append(_Tgt(kind, level, _get, _set))

    def _build(self):
        for tag, attr, kind, level_attr in _SC_ATTR.get(self.builder, []):
            for astart, aend in _find_open_tags(self.source, tag):
                if aend <= astart:
                    continue
                attrs = _parse_attrs(self.source, astart, aend)
                if attr not in attrs:
                    continue
                lvl = _level_from(attrs, level_attr) if kind == "heading" else None
                self._add_attr_target(kind, lvl, attrs[attr])
        for tag in _SC_INNER.get(self.builder, []):
            for blk in _find_blocks(self.source, tag):
                if blk["inner_end"] > blk["inner_start"]:
                    self._add_inner_block(blk["inner_start"], blk["inner_end"])

    def serialize(self):
        edits = list(self._edits)
        for b in self._blocks:
            if b["dirty"]:
                edits.append((b["start"], b["end"], _inner_html(b["root"])))
        edits.sort(key=lambda x: x[0], reverse=True)
        out = self.source
        prev_start = None
        for s, e, new in edits:
            if prev_start is not None and e > prev_start:
                continue  # defensive: skip any overlap
            out = out[:s] + new + out[e:]
            prev_start = s
        return out


# --------------------------------------------------------------------------
# Bricks: JSON array of elements in _bricks_page_content_2 (pure Python)
# --------------------------------------------------------------------------
def _bricks_kind(name, settings):
    n = (name or "").lower()
    if n == "heading":
        lvl = str(settings.get("tag") or "").lower()
        lvl = lvl if re.fullmatch(r"h[1-6]", lvl or "") else "h2"
        return "heading", lvl
    if n in ("button",):
        return "button", None
    return "para", None


class BricksDoc:
    def __init__(self, source):
        self.source = source or ""
        self.targets = []
        self.data = None
        try:
            self.data = json.loads(self.source) if self.source.strip() else []
        except Exception:
            self.data = None
        if isinstance(self.data, dict):
            self._container = self.data.get("content", [])
        elif isinstance(self.data, list):
            self._container = self.data
        else:
            self._container = []
        self._build()

    @property
    def ok(self):
        return self.data is not None

    def _build(self):
        for el in self._container:
            if not isinstance(el, dict):
                continue
            name = el.get("name") or ""
            settings = el.get("settings")
            if not isinstance(settings, dict):
                continue
            for fld in ("text", "content"):
                if isinstance(settings.get(fld), str) and settings[fld].strip():
                    kind, level = _bricks_kind(name, settings)
                    holder = {"links": []}

                    def _get(s=settings, f=fld, holder=holder):
                        masked, links = mask_links(s[f])
                        holder["links"] = links
                        return masked

                    def _set(new, s=settings, f=fld, holder=holder):
                        restored, ok = restore_links(new, holder["links"])
                        if holder["links"] and not ok:
                            return False  # keep original rather than drop a link
                        s[f] = restored
                        return True
                    self.targets.append(_Tgt(kind, level, _get, _set))
                    break

    def serialize(self):
        obj = self.data if isinstance(self.data, dict) else self._container
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


# --------------------------------------------------------------------------
# Beaver Builder: PHP-serialized objects in _fl_builder_data.
# Handled entirely in PHP (unserialize -> edit -> serialize) so stdClass
# round-trips faithfully. These runners are staged/run by core via WP-CLI.
# --------------------------------------------------------------------------
# Module slug -> settings field holding editable text, with kind/level.
# (level for 'heading' is read from settings->tag when present.)
BEAVER_FIELDS_JSON = json.dumps({
    "heading": {"field": "heading", "kind": "heading"},
    "rich-text": {"field": "text", "kind": "para"},
    "text-editor": {"field": "text", "kind": "para"},
    "text": {"field": "text", "kind": "para"},
    "button": {"field": "text", "kind": "button"},
    "button-group": {"field": "text", "kind": "button"},
    "callout": {"field": "text", "kind": "para"},
})

# Extract: emit one row per editable node: {node, field, kind, level, text}
BEAVER_EXTRACT_PHP = r"""<?php
if ( ! defined( 'WP_CLI' ) || ! WP_CLI ) { return; }
$in   = json_decode( file_get_contents( $args[0] ), true );
$pid  = (int) $in['id'];
$map  = json_decode( $in['map'], true );
$data = get_post_meta( $pid, '_fl_builder_data', true );
$out  = array();
if ( is_array( $data ) ) {
    foreach ( $data as $nid => $node ) {
        if ( ! is_object( $node ) || ! isset( $node->settings ) ) { continue; }
        $type = isset( $node->settings->type ) ? $node->settings->type : '';
        if ( ! isset( $map[ $type ] ) ) { continue; }
        $field = $map[ $type ]['field'];
        $kind  = $map[ $type ]['kind'];
        if ( ! isset( $node->settings->$field ) || ! is_string( $node->settings->$field ) ) { continue; }
        $txt = $node->settings->$field;
        if ( trim( $txt ) === '' ) { continue; }
        $level = '';
        if ( $kind === 'heading' && isset( $node->settings->tag ) ) { $level = (string) $node->settings->tag; }
        $out[] = array( 'node' => (string) $nid, 'field' => $field, 'kind' => $kind,
                        'level' => $level, 'text' => $txt );
    }
}
echo json_encode( $out );
"""

# Apply: edits = [{node, field, value}]; set each and re-save the meta.
BEAVER_APPLY_PHP = r"""<?php
if ( ! defined( 'WP_CLI' ) || ! WP_CLI ) { return; }
$in    = json_decode( file_get_contents( $args[0] ), true );
$pid   = (int) $in['id'];
$edits = isset( $in['edits'] ) ? $in['edits'] : array();
$data  = get_post_meta( $pid, '_fl_builder_data', true );
$n = 0;
if ( is_array( $data ) ) {
    foreach ( (array) $edits as $ed ) {
        $nid   = (string) $ed['node'];
        $field = (string) $ed['field'];
        if ( isset( $data[ $nid ] ) && is_object( $data[ $nid ] ) && isset( $data[ $nid ]->settings ) ) {
            $data[ $nid ]->settings->$field = $ed['value'];
            $n++;
        }
    }
    update_post_meta( $pid, '_fl_builder_data', $data );
    clean_post_cache( $pid );
    if ( class_exists( 'FLBuilderModel' ) && method_exists( 'FLBuilderModel', 'delete_asset_cache_for_post' ) ) {
        try { FLBuilderModel::delete_asset_cache_for_post( $pid ); } catch ( \Throwable $e ) {}
    }
}
echo "UPDATED $n";
"""


# builder -> which post field / meta key holds its source (for non-PHP docs)
SOURCE_META = {
    "oxygen": "ct_builder_shortcodes",
    "bricks": "_bricks_page_content_2",
}