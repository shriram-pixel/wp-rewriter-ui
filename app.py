#!/usr/bin/env python3
"""
Web UI server for WP Rewriter.

    pip install -r requirements.txt
    python3 app.py            # then open http://127.0.0.1:5000

Binds to localhost only. It holds your OpenAI key and SSH access, so don't
expose it on a public interface.
"""

import json

import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED

try:
    from flask import Flask, Response, jsonify, request
except ImportError:
    raise SystemExit("Flask is not installed. Run:  pip install -r requirements.txt")

import core

app = Flask(__name__)
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0


@app.after_request
def _no_cache(resp):
    resp.headers["Cache-Control"] = "no-store, max-age=0, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/")
def index():
    # Served raw (not via Jinja) so the literal {{title}}/{{h1}} placeholders in
    # the page aren't interpreted as template variables.
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "index.html")
    with open(path, encoding="utf-8") as fh:
        return Response(fh.read(), mimetype="text/html")


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    if request.method == "GET":
        cfg = core.get_config()
        return jsonify({
            "build": core.BUILD,
            "ssh": {
                "host": cfg["ssh"].get("host", ""),
                "username": cfg["ssh"].get("username", ""),
                "port": cfg["ssh"].get("port", "22"),
                "has_password": bool(cfg["ssh"].get("password", "").strip()),
            },
            "wordpress": {"path": cfg["wordpress"]["path"], "wp_bin": cfg["wordpress"].get("wp_bin", "wp")},
            "openai": {
                "has_api_key": bool(cfg["openai"].get("api_key", "").strip()),
                "base_url": cfg["openai"].get("base_url", "https://api.openai.com/v1"),
                "model": cfg["openai"].get("model", "gpt-4o-mini"),
                "max_tokens": cfg["openai"].get("max_tokens", "256"),
                "temperature": cfg["openai"].get("temperature", "0.7"),
                "provider": cfg["openai"].get("provider", "api"),
            },
            "claude": {
                "command": cfg["claude"].get("command", "claude"),
                "model": cfg["claude"].get("model", ""),
            },
        })

    data = request.get_json(force=True) or {}
    cfg = core.get_config()
    ssh_in = data.get("ssh", {})
    wp_in = data.get("wordpress", {})
    oa_in = data.get("openai", {})
    cl_in = data.get("claude", {})

    out = {
        "ssh": {
            "host": ssh_in.get("host", cfg["ssh"].get("host", "")),
            "username": ssh_in.get("username", cfg["ssh"].get("username", "")),
            "port": ssh_in.get("port", cfg["ssh"].get("port", "22")),
        },
        "wordpress": {
            "path": wp_in.get("path", cfg["wordpress"]["path"]),
            "wp_bin": wp_in.get("wp_bin", cfg["wordpress"].get("wp_bin", "wp")),
        },
        "openai": {
            "base_url": oa_in.get("base_url", cfg["openai"].get("base_url", "https://api.openai.com/v1")) or "https://api.openai.com/v1",
            "model": oa_in.get("model", cfg["openai"].get("model")),
            "max_tokens": oa_in.get("max_tokens", cfg["openai"].get("max_tokens")),
            "temperature": oa_in.get("temperature", cfg["openai"].get("temperature")),
            "provider": (oa_in.get("provider", cfg["openai"].get("provider", "api")) or "api").strip().lower(),
        },
        "claude": {
            "command": (cl_in.get("command", cfg["claude"].get("command", "claude")) or "claude").strip(),
            "model": cl_in.get("model", cfg["claude"].get("model", "")),
        },
    }
    # Only overwrite secrets if a new non-empty value was supplied.
    new_pw = ssh_in.get("password", "")
    out["ssh"]["password"] = new_pw if new_pw.strip() else cfg["ssh"].get("password", "")
    new_key = oa_in.get("api_key", "")
    out["openai"]["api_key"] = new_key if new_key.strip() else cfg["openai"].get("api_key", "")

    core.save_config(out)
    core.reset_client()  # use the new credentials on the next call
    return jsonify({"ok": True})


@app.route("/api/ai/test", methods=["POST"])
def api_ai_test():
    cfg = core.get_config()
    try:
        return jsonify(core.ai_test(cfg))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "message": str(exc)})


@app.route("/api/check", methods=["POST"])
def api_check():
    return jsonify(core.check_connection(core.get_config()))


@app.route("/api/posttypes", methods=["POST"])
def api_posttypes():
    try:
        return jsonify({"ok": True, "types": core.list_post_types(core.get_config())})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)})


@app.route("/api/posts", methods=["POST"])
def api_posts():
    data = request.get_json(force=True) or {}
    post_type = data.get("post_type", "page")
    statuses = data.get("statuses") or ["publish"]
    try:
        return jsonify({"ok": True, "posts": core.list_posts(core.get_config(), post_type, statuses)})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)})


@app.route("/api/replace", methods=["POST"])
def api_replace():
    data = request.get_json(force=True) or {}
    pairs = [(p[0], p[1]) for p in data.get("pairs", []) if p and p[0] != ""]
    if not pairs:
        return jsonify({"ok": False, "error": "Add at least one find/replace pair."})
    cfg = core.get_config()
    apply = bool(data.get("apply"))
    smart = bool(data.get("smart_case"))
    result = core.run_replace(cfg, pairs, data.get("scope", "prefix"), apply,
                              smart_case=smart, include_guid=bool(data.get("include_guid")),
                              skip_logs=bool(data.get("skip_logs", True)))
    report, rc = result["report"], result["rc"]
    if data.get("rename_media"):
        media = core.rename_media(cfg, pairs, apply, smart_case=smart)
        report += "\n\n──────── media files ────────\n" + media["report"]
        rc = rc or media["rc"]
    return jsonify({"ok": rc == 0, "report": report, "rc": rc})


@app.route("/api/preview", methods=["POST"])
def api_preview():
    data = request.get_json(force=True) or {}
    cfg = core.get_config()
    pid = data.get("post_id")
    if not pid:
        return jsonify({"ok": False, "message": "Select a post to preview against."})
    try:
        if data.get("action") == "fetch":
            return jsonify(core.fetch_sample(cfg, pid, data.get("xpath", "")))
        return jsonify(core.test_prompt(cfg, pid, data.get("xpath", ""), data.get("prompt", ""), data.get("placeholders", {})))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "message": str(exc)})


# Registry of running rewrite jobs -> a stop Event. A separate /api/rewrite/stop
# request sets the Event; the streaming job checks it between pages.
_rewrite_stops = {}
_rewrite_stops_lock = threading.Lock()


def _register_job(job_id):
    ev = threading.Event()
    with _rewrite_stops_lock:
        _rewrite_stops[job_id] = ev
    return ev


def _unregister_job(job_id):
    with _rewrite_stops_lock:
        _rewrite_stops.pop(job_id, None)


@app.route("/api/rewrite/stop", methods=["POST"])
def api_rewrite_stop():
    data = request.get_json(force=True) or {}
    job_id = data.get("job_id")
    with _rewrite_stops_lock:
        ev = _rewrite_stops.get(job_id)
    if ev is not None:
        ev.set()
        return jsonify({"ok": True, "stopping": True})
    return jsonify({"ok": False, "error": "That rewrite isn't running (it may have already finished)."})


@app.route("/api/rewrite", methods=["POST"])
def api_rewrite():
    data = request.get_json(force=True) or {}
    cfg = core.get_config()
    rows = data.get("rows", [])
    placeholders = data.get("placeholders", {})
    ids = [int(i) for i in data.get("ids", [])]
    dry_run = bool(data.get("dry_run"))
    elementor_mode = bool(data.get("elementor"))
    elementor_specs = [{
        "prompt": r.get("prompt", ""),
        "allowed": core.elementor_types_from_xpath(r.get("xpath", "")),
        "index": core.xpath_index(r.get("xpath", "")),
    } for r in rows]
    job_id = data.get("job_id") or ("job-" + os.urandom(4).hex())
    stop_event = _register_job(job_id)

    def stream():
        if not rows:
            yield json.dumps({"event": "error", "message": "No XPath/prompt rows."}) + "\n"
            return
        if not ids:
            yield json.dumps({"event": "error", "message": "No posts selected."}) + "\n"
            return
        yield json.dumps({"event": "start", "total": len(ids), "dry_run": dry_run, "job_id": job_id}) + "\n"

        try:
            posts = core.read_posts(cfg, ids)
        except Exception as exc:  # noqa: BLE001
            yield json.dumps({"event": "error", "message": "Reading posts failed: " + str(exc)}) + "\n"
            return

        def work(pid):
            post = posts.get(pid)
            builder = core.builder_of_post(post) if post else "classic"
            if elementor_mode and core.is_elementor(post):
                res = core.process_one_elementor(cfg, post, elementor_specs, placeholders)
                res["_kind"] = "elementor"
                builder = "elementor"
            elif elementor_mode and builder in ("wpbakery", "divi", "oxygen", "bricks", "beaver"):
                res = core.process_builder(cfg, post, builder, rows, placeholders)
                res.setdefault("_kind", "content")
            else:
                res = core.process_one(cfg, post, rows, placeholders)
                res["_kind"] = "content"
            res["_builder"] = builder
            return pid, res

        counts = {"updated": 0, "preview": 0, "skipped": 0, "error": 0}
        updates = {}
        pending = list(ids)
        inflight = {}
        with ThreadPoolExecutor(max_workers=4) as pool:
            def fill():
                # Start new pages only while not stopping. Pages already running
                # are always allowed to finish, so a page is never cut off
                # half-rewritten and its finished result is still saved.
                while pending and len(inflight) < 4 and not stop_event.is_set():
                    p = pending.pop(0)
                    inflight[pool.submit(work, p)] = p
            fill()
            while inflight:
                done, _ = wait(list(inflight), return_when=FIRST_COMPLETED)
                for fut in done:
                    inflight.pop(fut, None)
                    pid, res = fut.result()
                    status = res["status"]
                    if status == "updated":
                        if dry_run:
                            status = "preview"
                        else:
                            k = res["_kind"]
                            if k == "elementor":
                                updates[pid] = {"type": "elementor", "value": res["elementor"]}
                            elif k == "meta":
                                updates[pid] = {"type": "meta", "key": res["meta_key"],
                                                "value": res["value"]}
                            elif k == "bricks":
                                updates[pid] = {"type": "bricks", "value": res["value"]}
                            elif k == "beaver":
                                updates[pid] = {"type": "beaver", "edits": res["edits"]}
                            else:
                                updates[pid] = {"type": "content", "value": res["content"]}
                    counts[status] = counts.get(status, 0) + 1
                    b = res.get("_builder")
                    where = (" (%s)" % b) if (b and b != "classic") else ""
                    yield json.dumps({"event": "post", "id": pid, "status": status,
                                      "message": res["message"] + where}) + "\n"
                fill()

        stopped = stop_event.is_set()
        not_started = len(pending)
        if stopped:
            yield json.dumps({"event": "stopping", "not_started": not_started}) + "\n"

        if updates and not dry_run:
            yield json.dumps({"event": "writing", "count": len(updates)}) + "\n"
            wres = core.write_posts(cfg, updates)
            if wres["rc"] != 0:
                yield json.dumps({"event": "post", "id": 0, "status": "error",
                                  "message": "writing failed: " + (wres["report"] or "")}) + "\n"
            else:
                yield json.dumps({"event": "written", "count": wres["count"]}) + "\n"

        yield json.dumps({"event": "done", "counts": counts, "stopped": stopped,
                          "not_started": not_started}) + "\n"

    def stream_wrapped():
        try:
            for chunk in stream():
                yield chunk
        finally:
            _unregister_job(job_id)

    return Response(stream_wrapped(), mimetype="application/x-ndjson")


@app.route("/api/logo/upload", methods=["POST"])
def api_logo_upload():
    f = request.files.get("file")
    if f is None:
        return jsonify({"ok": False, "error": "No file was selected."})
    data = f.read()
    if not data:
        return jsonify({"ok": False, "error": "The selected file is empty."})
    try:
        res = core.upload_logo(core.get_config(), data, f.filename or "logo.png")
        return jsonify({"ok": True, "url": res["url"], "id": res["id"]})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)})


@app.route("/api/favicon/upload", methods=["POST"])
def api_favicon_upload():
    f = request.files.get("file")
    if f is None:
        return jsonify({"ok": False, "error": "No file was selected."})
    data = f.read()
    if not data:
        return jsonify({"ok": False, "error": "The selected file is empty."})
    try:
        res = core.upload_favicon(core.get_config(), data, f.filename or "favicon.png")
        return jsonify({"ok": True, "url": res["url"], "id": res["id"], "report": res["report"]})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)})


@app.route("/api/logo/inspect", methods=["POST"])
def api_logo_inspect():
    data = request.get_json(force=True) or {}
    try:
        return jsonify({"ok": True, "report": core.inspect_logo(core.get_config(), (data.get("old_url") or "").strip())["report"]})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)})


@app.route("/api/logo/replace", methods=["POST"])
def api_logo_replace():
    data = request.get_json(force=True) or {}
    old_url = (data.get("old_url") or "").strip()
    new_url = (data.get("new_url") or "").strip()
    if not new_url:
        return jsonify({"ok": False, "error": "Enter the new logo URL, or upload a logo first."})
    try:
        res = core.replace_logo(core.get_config(), old_url, new_url, bool(data.get("set_identity", True)))
        return jsonify({"ok": True, "report": res["report"]})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)})


if __name__ == "__main__":
    import os as _os
    import sys as _sys
    if _os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        _sys.stderr.write(
            "\n  WP Rewriter — %s\n"
            "  Open http://127.0.0.1:5000   (Ctrl+C to stop)\n"
            "  Auto-reload is on: saving a .py file restarts the server automatically.\n\n"
            % core.BUILD
        )
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True, use_reloader=True)