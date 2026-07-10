#!/usr/bin/env python3
"""Splunk SOAR MCP server — headless playbook authoring/ops via the REST API.

No browser needed. All calls go straight to the SOAR REST API with a ph-auth-token
(or HTTP Basic auth as a fallback).

Env:
  SOAR_HOST        base URL of your SOAR instance, e.g. https://soar.example.com  (required)
  SOAR_TOKEN       ph-auth-token of an automation user with playbook import/edit rights
  SOAR_USER        username for HTTP Basic auth (used only if SOAR_TOKEN is unset)
  SOAR_PASS        password for HTTP Basic auth
  SOAR_OWNER_ID    numeric ph_user id of the automation user actions run as (default 1)
  SOAR_VERIFY_SSL  set to "true" to verify TLS certificates (default: false, for self-signed)

Run: uv run --with fastmcp python server.py   (or: python server.py)
"""
from __future__ import annotations

import base64
import gzip
import io
import json
import os
import ssl
import tarfile
import time
import urllib.request
import urllib.parse
import ast

from fastmcp import FastMCP

SOAR_HOST = os.environ.get("SOAR_HOST", "").rstrip("/")
if not SOAR_HOST:
    raise RuntimeError("SOAR_HOST is not set — export SOAR_HOST=https://your-soar-host")
# ph-auth-token of an automation user with playbook import/edit rights.
SOAR_TOKEN = os.environ.get("SOAR_TOKEN", "")
# Optional username/password fallback — SOAR's REST API also accepts HTTP Basic.
SOAR_USER = os.environ.get("SOAR_USER", "")
SOAR_PASS = os.environ.get("SOAR_PASS", "")
# ph_user id of the automation account playbook/action runs execute as.
DEFAULT_OWNER = int(os.environ.get("SOAR_OWNER_ID", "1"))
# SOAR instances commonly ship self-signed certs; verification is off by default.
if os.environ.get("SOAR_VERIFY_SSL", "").lower() in ("1", "true", "yes"):
    _CTX = ssl.create_default_context()
else:
    _CTX = ssl._create_unverified_context()

mcp = FastMCP("soar")


def _auth_header(r: urllib.request.Request) -> str:
    """Prefer the ph-auth-token; fall back to HTTP Basic when only user/pass is configured."""
    if SOAR_TOKEN:
        r.add_header("ph-auth-token", SOAR_TOKEN)
        return "token"
    if SOAR_USER and SOAR_PASS:
        basic = base64.b64encode(f"{SOAR_USER}:{SOAR_PASS}".encode()).decode()
        r.add_header("Authorization", f"Basic {basic}")
        return "basic"
    raise RuntimeError("no SOAR credentials: set SOAR_TOKEN, or SOAR_USER + SOAR_PASS")


def _req(path: str, data: bytes | None = None, method: str = "GET") -> tuple[int, str]:
    r = urllib.request.Request(SOAR_HOST + path, data=data, method=method)
    _auth_header(r)
    if data is not None:
        r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, context=_CTX, timeout=60) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def _json(path: str, data=None, method="GET"):
    st, body = _req(path, json.dumps(data).encode() if data is not None else None, method)
    try:
        return st, json.loads(body)
    except Exception:
        return st, body


def _build_b64(out_dir: str, name: str) -> str:
    """Pack out_dir/{name}.py + {name}.json into a gzip tarball, base64-encoded.
    Validates the python (ast + no leftover human-prompt constructs) first."""
    py = open(os.path.join(out_dir, f"{name}.py")).read()
    jj = open(os.path.join(out_dir, f"{name}.json")).read()
    ast.parse(py)
    for k in ("prompt2", "launching_user", "action_result.summary.responses"):
        if k in py:
            raise ValueError(f"leftover human-prompt construct in py: {k}")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for fn, body in [(f"{name}.py", py), (f"{name}.json", jj)]:
            b = body.encode()
            ti = tarfile.TarInfo(name=fn)
            ti.size = len(b); ti.mode = 0o660; ti.mtime = int(time.time())
            tar.addfile(ti, io.BytesIO(b))
    return base64.b64encode(gzip.compress(buf.getvalue())).decode()


@mcp.tool
def deploy_playbook(out_dir: str, name: str) -> dict:
    """Build a bundle from out_dir/{name}.py + {name}.json, import it to SOAR, and
    verify. Each import creates a NEW playbook id (does not overwrite by name).
    Returns {imported_id, passed_validation, playbook_type, input_spec, message}."""
    b64 = _build_b64(out_dir, name)
    st, body = _json("/rest/import_playbook", {"playbook": b64, "scm": 2, "force": True}, "POST")
    if not isinstance(body, dict) or not body.get("id"):
        return {"ok": False, "status": st, "response": body}
    pid = body["id"]
    st2, meta = _json(f"/rest/playbook/{pid}")
    return {
        "ok": True, "imported_id": pid, "message": body.get("message"),
        "passed_validation": meta.get("passed_validation"),
        "playbook_type": meta.get("playbook_type"),
        "input_spec": [i["name"] for i in (meta.get("input_spec") or [])],
        "active": meta.get("active"),
    }


@mcp.tool
def import_bundle_b64(b64: str, scm: int = 2, force: bool = True) -> dict:
    """Import a pre-built base64 gzip-tarball bundle to SOAR. Returns the raw response."""
    st, body = _json("/rest/import_playbook", {"playbook": b64, "scm": scm, "force": force}, "POST")
    return {"status": st, "response": body}


@mcp.tool
def get_playbook(playbook_id: int) -> dict:
    """Get a playbook's metadata: name, passed_validation, playbook_type, input_spec, active."""
    st, j = _json(f"/rest/playbook/{playbook_id}")
    if not isinstance(j, dict):
        return {"status": st, "response": j}
    return {
        "id": playbook_id, "name": j.get("name"),
        "passed_validation": j.get("passed_validation"),
        "playbook_type": j.get("playbook_type"),
        "input_spec": [i["name"] for i in (j.get("input_spec") or [])],
        "active": j.get("active"), "labels": j.get("labels"),
    }


@mcp.tool
def list_playbooks(name_contains: str = "", limit: int = 25) -> list:
    """List playbooks, optionally filtered by a substring of the name."""
    q = {"page_size": str(limit), "sort": "id", "order": "desc"}
    if name_contains:
        q["_filter_name__icontains"] = f'"{name_contains}"'
    st, j = _json("/rest/playbook?" + urllib.parse.urlencode(q))
    if not isinstance(j, dict):
        return [{"status": st, "response": j}]
    return [{"id": p["id"], "name": p.get("name"), "type": p.get("playbook_type"),
             "active": p.get("active"), "passed_validation": p.get("passed_validation")}
            for p in j.get("data", [])]


@mcp.tool
def run_playbook(container_id: int, playbook_id: int, inputs: dict | None = None,
                 scope: str = "all") -> dict:
    """Trigger a playbook_run on a container with optional input-playbook inputs.
    Returns {playbook_run_id}. Use playbook_run_status() to poll."""
    body = {"container_id": container_id, "playbook_id": playbook_id,
            "scope": scope, "run": True}
    if inputs:
        body["inputs"] = inputs
    st, j = _json("/rest/playbook_run", body, "POST")
    return {"status": st, "response": j}


@mcp.tool
def playbook_run_status(playbook_run_id: int) -> dict:
    """Get a playbook run's status and message (success/failed/running)."""
    st, j = _json(f"/rest/playbook_run/{playbook_run_id}")
    if not isinstance(j, dict):
        return {"status": st, "response": j}
    return {"playbook_run_id": playbook_run_id, "status": j.get("status"),
            "message": j.get("message"), "num_actions": j.get("num_actions"),
            "start_time": j.get("start_time"), "end_time": j.get("end_time")}


def _action_runs(playbook_run_id: int) -> list:
    q = {"_filter_playbook_run_id": str(playbook_run_id), "page_size": "50"}
    st, j = _json("/rest/action_run?" + urllib.parse.urlencode(q))
    if not isinstance(j, dict):
        return [{"status": st, "response": j}]
    return [{"id": a["id"], "action": a.get("action"), "status": a.get("status"),
             "message": a.get("message")} for a in j.get("data", [])]


@mcp.tool
def action_runs(playbook_run_id: int) -> list:
    """List the action runs (with status + message) produced by a playbook run.
    Use this to confirm which ITSM actions succeeded/failed."""
    return _action_runs(playbook_run_id)


def _app_run_detail(action_run_id: int) -> list:
    q = {"_filter_action_run": str(action_run_id)}
    st, j = _json("/rest/app_run?" + urllib.parse.urlencode(q))
    if not isinstance(j, dict):
        return [{"status": st, "response": j}]
    out = []
    for a in j.get("data", []):
        # the list endpoint omits result_data; only the detail endpoint with
        # include_expensive returns it (and with it, the real action parameters).
        _, full = _json(f"/rest/app_run/{a['id']}?include_expensive=true")
        rd = (full.get("result_data") if isinstance(full, dict) else None) or []
        out.append({"app_run_id": a["id"], "action": a.get("action"),
                    "status": a.get("status"), "message": a.get("message"),
                    "parameter": rd[0].get("parameter") if rd else None,
                    "result_message": rd[0].get("message") if rd else None})
    return out


@mcp.tool
def app_run_detail(action_run_id: int) -> list:
    """Get per-app-run detail (parameters + message) for an action run — shows the
    exact parameters sent to the app (e.g. the resolved string group_id)."""
    return _app_run_detail(action_run_id)


@mcp.tool
def wait_for_playbook_run(playbook_run_id: int, timeout_secs: int = 180,
                          poll_secs: int = 5) -> dict:
    """Poll a playbook run until it leaves 'running', then return its final status
    together with its action runs. Avoids hand-rolled sleep loops."""
    deadline = time.time() + timeout_secs
    status, j = "running", {}
    while time.time() < deadline:
        st, j = _json(f"/rest/playbook_run/{playbook_run_id}")
        if not isinstance(j, dict):
            return {"status": st, "response": j}
        status = j.get("status") or "unknown"
        if status != "running":
            break
        time.sleep(poll_secs)
    return {"playbook_run_id": playbook_run_id, "status": status,
            "timed_out": status == "running",
            "message": j.get("message"),
            "action_runs": _action_runs(playbook_run_id)}


@mcp.tool
def add_note(container_id: int, title: str, content: str,
             note_type: str = "general") -> dict:
    """Add a note to a container (e.g. an analyst note)."""
    st, j = _json("/rest/container_note", {"container_id": container_id, "title": title,
                                           "content": content, "note_type": note_type}, "POST")
    return {"status": st, "response": j}


@mcp.tool
def get_container(container_id: int) -> dict:
    """Get a container's key fields + custom_fields (status, tenant, name, PromptCare id...)."""
    st, j = _json(f"/rest/container/{container_id}?_exclude_fields=artifacts")
    if not isinstance(j, dict):
        return {"status": st, "response": j}
    return {"id": container_id, "name": j.get("name"), "status": j.get("status"),
            "tenant": j.get("tenant"), "label": j.get("label"),
            "close_time": j.get("close_time"), "custom_fields": j.get("custom_fields")}


@mcp.tool
def search_containers(name_contains: str = "", status: str = "", tenant: str = "",
                      limit: int = 25) -> list:
    """Search containers by name substring / status / tenant. Returns id, status, name, custom_fields."""
    q = {"page_size": str(limit), "sort": "id", "order": "desc"}
    if name_contains:
        q["_filter_name__icontains"] = f'"{name_contains}"'
    if status:
        q["_filter_status"] = f'"{status}"'
    if tenant != "":
        q["_filter_tenant"] = tenant
    st, j = _json("/rest/container?" + urllib.parse.urlencode(q))
    if not isinstance(j, dict):
        return [{"status": st, "response": j}]
    out = []
    for c in j.get("data", []):
        out.append({"id": c["id"], "status": c.get("status"), "name": c.get("name"),
                    "custom_fields": c.get("custom_fields") or {}})
    return out


@mcp.tool
def whoami() -> dict:
    """Check which credentials the server is using and whether they still work.
    Returns the auth mode (token/basic), the SOAR version, and the automation user
    that playbook/action runs execute as."""
    st, ver = _json("/rest/version")
    mode = "token" if SOAR_TOKEN else ("basic" if (SOAR_USER and SOAR_PASS) else "none")
    out = {"host": SOAR_HOST, "auth_mode": mode, "auth_ok": st == 200,
           "version": ver.get("version") if isinstance(ver, dict) else ver}
    st2, u = _json(f"/rest/ph_user/{DEFAULT_OWNER}")
    if isinstance(u, dict) and u.get("username"):
        out["run_as"] = {"id": u["id"], "username": u["username"], "type": u.get("type")}
    return out


@mcp.tool
def get_artifacts(container_id: int, limit: int = 25) -> list:
    """List a container's artifacts with their full cef dict. Note that playbooks
    typically read the ARTIFACT cef, not the container custom_fields."""
    q = {"page_size": str(limit), "sort": "id", "order": "asc"}
    st, j = _json(f"/rest/container/{container_id}/artifacts?" + urllib.parse.urlencode(q))
    if not isinstance(j, dict):
        return [{"status": st, "response": j}]
    return [{"id": a["id"], "name": a.get("name"), "label": a.get("label"),
             "in_case": a.get("in_case"), "cef": a.get("cef")} for a in j.get("data", [])]


@mcp.tool
def update_artifact(artifact_id: int, cef: dict) -> dict:
    """Merge keys into an artifact's cef. Used to satisfy phase guards, e.g.
    {"t4": "2026-07-10 00:30:00", "t5": ""}.

    DANGER: a bare POST /rest/artifact/<id> {"cef": {...}} REPLACES the whole cef and
    silently destroys every other key (rule_title, alert_template, the enrichment
    templates...). This tool always reads the current cef and posts it back merged."""
    st, a = _json(f"/rest/artifact/{artifact_id}")
    if not isinstance(a, dict) or "cef" not in a:
        return {"ok": False, "status": st, "response": a}
    merged = dict(a["cef"])
    merged.update(cef)
    st2, j = _json(f"/rest/artifact/{artifact_id}", {"cef": merged}, "POST")
    return {"ok": st2 == 200, "status": st2, "response": j,
            "cef_keys_before": len(a["cef"]), "cef_keys_after": len(merged)}


@mcp.tool
def get_custom_list(name: str) -> dict:
    """Read a SOAR custom list (decided_list) by name."""
    st, j = _json(f"/rest/decided_list/{urllib.parse.quote(name)}")
    if not isinstance(j, dict):
        return {"status": st, "response": j}
    return {"name": j.get("name"), "id": j.get("id"), "content": j.get("content")}


@mcp.tool
def update_custom_list_rows(name: str, rows: dict) -> dict:
    """Replace whole rows of a custom list. `rows` maps a 0-based row index (as a string)
    to the full row list — a partial row truncates the rest, so send every column.
    Row 0 is the header. Read the list first with get_custom_list()."""
    st, j = _json(f"/rest/decided_list/{urllib.parse.quote(name)}",
                  {"update_rows": {str(k): v for k, v in rows.items()}}, "POST")
    return {"status": st, "response": j}


@mcp.tool
def get_playbook_source(playbook_id: int) -> dict:
    """Fetch a playbook's generated python + its block/edge graph. Use this to read the
    real datapaths of a prod playbook before transforming or debugging it."""
    st, j = _json(f"/rest/playbook/{playbook_id}?json_mode=true")
    if not isinstance(j, dict):
        return {"status": st, "response": j}
    return {"id": playbook_id, "name": j.get("name"), "python": j.get("python"),
            "playbook_type": j.get("playbook_type"),
            "input_spec": [i["name"] for i in (j.get("input_spec") or [])],
            "output_spec": [o["name"] for o in (j.get("output_spec") or [])]}


@mcp.tool
def update_asset(asset_id: int, config_updates: dict) -> dict:
    """Merge keys into an asset's configuration (e.g. rotate a password). Reads the current
    configuration first and posts it back whole — posting a partial config DROPS the other
    keys (HOST included). Secrets are never echoed back."""
    st, a = _json(f"/rest/asset/{asset_id}")
    if not isinstance(a, dict) or "configuration" not in a:
        return {"ok": False, "status": st, "response": a}
    cfg = dict(a["configuration"])
    cfg.update(config_updates)
    st2, j = _json(f"/rest/asset/{asset_id}", {"configuration": cfg}, "POST")
    st3, a2 = _json(f"/rest/asset/{asset_id}")
    safe = {k: ("<set>" if any(s in k.lower() for s in ("pass", "secret", "token")) else v)
            for k, v in (a2.get("configuration", {}) if isinstance(a2, dict) else {}).items()}
    return {"ok": st2 == 200, "status": st2, "response": j,
            "name": a.get("name"), "configuration": safe}


@mcp.tool
def run_action(action: str, asset: str, app_id: int, container_id: int,
               parameters: dict | None = None, action_type: str = "investigate",
               owner: int = DEFAULT_OWNER, timeout_secs: int = 120) -> dict:
    """Run a single app action (outside any playbook) and wait for the result.
    `owner` and `app_id` are BOTH mandatory to SOAR and must be sent at the top level AND
    inside the target — omitting either gives "Invalid or missing value for 'owner'" or
    "app_id has invalid format". Example (safe read-only check):
      run_action("get ticket info", "<asset name>", <app_id>, <container_id>, {"ticket_id": "..."})"""
    body = {"action": action, "type": action_type, "container_id": container_id,
            "name": f"mcp {action}", "owner": owner, "app_id": app_id,
            "targets": [{"assets": [asset], "app_id": app_id,
                         "parameters": [parameters or {}]}]}
    st, j = _json("/rest/action_run", body, "POST")
    if not isinstance(j, dict) or not j.get("action_run_id"):
        return {"ok": False, "status": st, "response": j}
    rid = j["action_run_id"]
    deadline = time.time() + timeout_secs
    status = "running"
    while time.time() < deadline:
        _, a = _json(f"/rest/action_run/{rid}")
        status = a.get("status") if isinstance(a, dict) else "unknown"
        if status != "running":
            return {"ok": status == "success", "action_run_id": rid, "status": status,
                    "message": a.get("message"), "app_runs": _app_run_detail(rid)}
        time.sleep(3)
    return {"ok": False, "action_run_id": rid, "status": "running", "timed_out": True}


@mcp.tool
def playbook_run_log(playbook_run_id: int, contains: str = "", limit: int = 100) -> list:
    """Read a playbook run's debug log. This is where SOAR reports the real failure —
    e.g. "the preceding call to phantom.collect2() returned an empty list"."""
    q = {"page_size": str(limit)}
    if contains:
        q["_filter_message__icontains"] = f'"{contains}"'
    st, j = _json(f"/rest/playbook_run/{playbook_run_id}/log?" + urllib.parse.urlencode(q))
    if not isinstance(j, dict):
        return [{"status": st, "response": j}]
    return [{"time": m.get("time"), "type": m.get("message_type"), "message": m.get("message")}
            for m in j.get("data", [])]


@mcp.tool
def rest(method: str, path: str, json_body: dict | None = None) -> dict:
    """Generic SOAR REST escape hatch. method=GET/POST/DELETE, path like '/rest/...'.
    Use only when a dedicated tool doesn't fit."""
    st, j = _json(path, json_body, method.upper())
    return {"status": st, "response": j}


if __name__ == "__main__":
    mcp.run()
