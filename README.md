# Splunk SOAR MCP Server

An [MCP (Model Context Protocol)](https://modelcontextprotocol.io) server for **Splunk SOAR** (formerly Phantom). It lets AI assistants like Claude Code operate a SOAR instance headlessly — no browser needed. Every call goes straight to the SOAR REST API using a `ph-auth-token` (or HTTP Basic auth).

**What you can do with it:**

- Triage containers (cases): search, read fields/artifacts, add analyst notes
- Author and deploy playbooks programmatically (the `import_playbook` bundle path — the only write path that actually works for modern playbooks)
- Run playbooks and single app actions, then poll status, action runs, and debug logs
- Manage custom lists and asset configurations
- Fall back to any raw REST call via the `rest` escape hatch

Tested against **Splunk SOAR 6.4.x**.

---

## Requirements

- Python 3.10+
- [`uv`](https://docs.astral.sh/uv/) (recommended) or `pip install fastmcp`
- Network access to your SOAR instance
- A SOAR **automation user** with an auth token (see below)

## Step 1 — Create an automation user & token in SOAR

1. Log in to your SOAR web UI as an admin.
2. Go to **Administration → User Management → Users → + User**.
3. Set **User Type = Automation**. Give it a name like `mcp-auto`.
4. Grant a role with the permissions you need (at minimum: view containers/playbooks; add **Edit/Import Playbooks** and **Execute Actions** if you want deploy/run capability).
5. Save the user, then open it — the **Authorization Configuration for REST API** section shows the JSON with the `ph-auth-token` value. Copy that token.
6. Note the user's numeric **id** (visible in the URL when viewing the user, or via `GET /rest/ph_user?_filter_type="automation"`). You'll use it as `SOAR_OWNER_ID` — it is required when running single actions via `run_action`.

> Alternatively, you can skip the token and use HTTP Basic auth with `SOAR_USER` / `SOAR_PASS`, but a scoped automation token is strongly recommended.

## Step 2 — Configure environment variables

| Variable | Required | Description |
|---|---|---|
| `SOAR_HOST` | ✅ | Base URL of your SOAR instance, e.g. `https://soar.example.com` (no trailing slash needed) |
| `SOAR_TOKEN` | ✅ (or user/pass) | The `ph-auth-token` from Step 1 |
| `SOAR_USER` / `SOAR_PASS` | — | HTTP Basic fallback, used only when `SOAR_TOKEN` is unset |
| `SOAR_OWNER_ID` | — | Numeric `ph_user` id of the automation user (default `1`). Used as the `owner` for `run_action` |
| `SOAR_VERIFY_SSL` | — | `true` to verify TLS certificates. Default `false` because most on-prem SOAR instances use self-signed certs |

## Step 3 — Register with your MCP client

### Claude Code (project scope)

Create `.mcp.json` in your project root (see [`.mcp.json.example`](.mcp.json.example)):

```json
{
  "mcpServers": {
    "soar": {
      "command": "uv",
      "args": ["run", "--with", "fastmcp", "python", "/absolute/path/to/splunk-soar-mcp/server.py"],
      "env": {
        "SOAR_HOST": "https://YOUR-SOAR-HOST",
        "SOAR_TOKEN": "YOUR-PH-AUTH-TOKEN",
        "SOAR_OWNER_ID": "1"
      }
    }
  }
}
```

Then start Claude Code in that project and approve the server when prompted (or run `/mcp` to check the connection).

> **Don't commit your real `.mcp.json`** — it contains your token. Keep it in `.gitignore` or use an env-var reference your client supports.

### Claude Desktop

Add the same block to `claude_desktop_config.json`:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

### Plain pip instead of uv

```json
"command": "python3",
"args": ["/absolute/path/to/splunk-soar-mcp/server.py"]
```

with `pip install fastmcp` done beforehand.

## Step 4 — Verify the connection

Ask your assistant to call the `whoami` tool. Expected output:

```json
{
  "host": "https://YOUR-SOAR-HOST",
  "auth_mode": "token",
  "auth_ok": true,
  "version": "6.4.1.361",
  "run_as": {"id": 1, "username": "mcp-auto", "type": "automation"}
}
```

If `auth_ok` is `false`, the token is wrong or lacks REST access. If the server fails to start with `SOAR_HOST is not set`, fix your `env` block.

---

## Tool reference

### Connection & generic

| Tool | Purpose |
|---|---|
| `whoami()` | Check credentials, SOAR version, and which automation user actions run as |
| `rest(method, path, json_body)` | Generic REST escape hatch (`GET`/`POST`/`DELETE`, path like `/rest/...`) |

### Containers (cases)

| Tool | Purpose |
|---|---|
| `search_containers(name_contains, status, tenant, limit)` | Find containers by name substring / status / tenant |
| `get_container(container_id)` | Key fields + `custom_fields` of one container |
| `get_artifacts(container_id, limit)` | List artifacts with their full CEF dict |
| `update_artifact(artifact_id, cef)` | **Merge** keys into an artifact's CEF (a bare POST would silently wipe every other key — this tool reads-then-merges) |
| `update_container_fields(container_id, custom_fields, status, name)` | Update a container's `custom_fields` / `status` / `name`. Container `custom_fields` **merge server-side**, so partial writes are safe |
| `add_note(container_id, title, content, note_type)` | Add a note to a container |

### Playbooks — read & author

| Tool | Purpose |
|---|---|
| `list_playbooks(name_contains, limit)` | Find playbooks by name |
| `get_playbook(playbook_id)` | Metadata: name, `passed_validation`, type, `input_spec`, active |
| `get_playbook_source(playbook_id)` | The generated Python + input/output spec — read the real datapaths before transforming a playbook |
| `deploy_playbook(out_dir, name)` | Build a tgz bundle from `out_dir/{name}.py` + `{name}.json`, validate (AST parse), import, and verify. **Each import creates a NEW playbook id** |
| `import_bundle_b64(b64, scm, force)` | Import a pre-built base64 gzip-tarball bundle |

> **Why bundles?** Modern (`is_modern: true`) playbooks are *not* writable via plain `POST /rest/playbook/{id}` — that endpoint returns `success` but silently changes nothing. `POST /rest/import_playbook` with a tar.gz bundle is the real write path.

### Playbooks & actions — run and debug

| Tool | Purpose |
|---|---|
| `run_playbook(container_id, playbook_id, inputs, scope)` | Trigger a playbook run (supports input playbooks) |
| `playbook_run_status(playbook_run_id)` | Poll one run: status + message |
| `wait_for_playbook_run(playbook_run_id, timeout_secs, poll_secs)` | Poll until the run finishes, return final status + its action runs |
| `action_runs(playbook_run_id)` | List the action runs a playbook run produced |
| `app_run_detail(action_run_id)` | Per-app-run parameters + result message — shows the exact parameters sent to the app |
| `playbook_run_log(playbook_run_id, contains, limit)` | The run's debug log — where SOAR reports the real failure (e.g. "phantom.collect2() returned an empty list") |
| `run_action(action, asset, app_id, container_id, parameters, ...)` | Run a single app action outside any playbook and wait for the result |

### Custom lists & assets

| Tool | Purpose |
|---|---|
| `get_custom_list(name)` | Read a custom list (`decided_list`) by name |
| `update_custom_list_rows(name, rows)` | Replace whole rows (`{"0-based index": [full row]}`). Row 0 is the header; always send every column |
| `update_asset(asset_id, config_updates)` | **Merge** keys into an asset config (e.g. rotate a password). Reads-then-merges — a partial POST would drop the other keys. Secrets are never echoed back |

---

## Typical workflow: build → deploy → test a playbook

```text
1. Produce out_dir/{NAME}.py and out_dir/{NAME}.json (playbook source + graph)
2. deploy_playbook("out_dir", "NAME")            → imported id + passed_validation
3. run_playbook(container_id, id, {..inputs..})  → playbook_run_id
4. wait_for_playbook_run(run_id)                 → final status + action runs
5. playbook_run_log(run_id)                      → debug the failure if any
```

## Gotchas learned the hard way

- `import_playbook` always creates a **new** playbook id — it never overwrites by name.
- REST `DELETE /rest/playbook/{id}` returns **405** — playbooks can only be deleted in the UI.
- A bare `POST /rest/artifact/{id} {"cef": {...}}` **replaces** the whole CEF and destroys every other key. Same for asset configurations. Use the merge tools here.
- Write semantics differ per object: container `custom_fields` **merge** (partial write safe), artifact top-level fields (name/label/severity) **merge**, but `artifact.cef` and `asset.configuration` **replace**.
- Container `status` values are picky — `"in progress"` is rejected; the valid value is `in-progress` (with hyphen).
- `run_action` requires `owner` and `app_id` **both** at the top level *and* inside the target, or SOAR rejects the request with a misleading error.
- One `phantom.collect2()` call collects over ONE block — never span two `playbook_input:*` fields in a single datapath list.

## Security notes

- Use a dedicated **automation user** with the minimum role, never a personal admin account.
- Keep the token out of version control (`.mcp.json` is in this repo's `.gitignore`).
- TLS verification is **off by default** (self-signed certs are the norm on-prem). Set `SOAR_VERIFY_SSL=true` if your instance has a valid certificate.
- Rotate the token from **User Management** if it ever leaks.

## License

[MIT](LICENSE)
