#!/usr/bin/env python3
"""Check 8 — Live end-to-end probe of mcp-jumphost with a real delegated Entra token.

Runs from inside the VNet (subnet `test`) using an ephemeral Azure Container Instance (ACI)
to reach the internal Container App endpoint https://<jumphost_fqdn>/dlptest/mcp.

Modes:
  Run A (default):
    a) initialize (protocolVersion 2025-06-18, clientInfo name jumphost-e2e)
    b) notifications/initialized (with session id)
    c) tools/list
    d) tools/call of the most harmless read-only tool
    e) control: tools/list with 'Authorization: Bearer bogus' -> expect 401

  Run B / B2 (A/B policy proof):
    a) initialize
    b) notifications/initialized
    c) tools/list
    d) tools/call generate_pii_ssn_ccn (EXPECT blocked by AI Gateway)
    e) tools/call generate_pii_email (EXPECT allowed)
    f) control: tools/list with 'Authorization: Bearer bogus' -> expect 401
"""
import argparse
import datetime
import json
import os
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
STATE = ROOT / "state"


def load_env(name):
    path = ROOT / "config" / name
    if not path.exists():
        raise SystemExit(f"missing {path}")
    out = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"')
    out.update({k: v for k, v in os.environ.items() if k in out})
    return out


CFG = load_env("demo.env")
RG = CFG["AZ_RESOURCE_GROUP"]
VNET = CFG["VNET_NAME"]
SUBNET = "test"

fqdn_file = STATE / "jumphost_fqdn"
if not fqdn_file.exists():
    raise SystemExit(f"missing {fqdn_file} — deploy jumphost first with azure/04-mcp-jumphost.sh")
FQDN = fqdn_file.read_text().strip()

token_file = pathlib.Path("/tmp/aigw-jumphost-secrets/user_token")
if not token_file.exists():
    raise SystemExit("missing /tmp/aigw-jumphost-secrets/user_token — run token acquisition first")
TOKEN = token_file.read_text().strip()
if not TOKEN:
    raise SystemExit("empty token in /tmp/aigw-jumphost-secrets/user_token")

# Ephemeral probe script to run inside ACI
PROBE_CODE = r"""
import datetime, json, os, sys, httpx

fqdn = os.environ['JUMPHOST_FQDN']
token = os.environ['USER_TOKEN']
run_mode = os.environ.get('RUN_MODE', 'A')
url = 'https://' + fqdn + '/dlptest/mcp'

headers_base = {
    'Accept': 'application/json, text/event-stream',
    'Content-Type': 'application/json',
}
headers_auth = {
    **headers_base,
    'Authorization': 'Bearer ' + token,
}

results = {
    'run': run_mode,
    'fqdn': fqdn,
    'url': url,
    'calls': []
}

def log_call(name, status, headers, body, note=''):
    utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
    hdrs = {str(k): str(v) for k, v in headers.items()}
    ct = hdrs.get('content-type', '')
    entry = {
        'call': name,
        'utc': utc,
        'status': status,
        'content_type': ct,
        'headers': hdrs,
        'body': str(body)[:800],
        'body_preview': str(body)[:800],
        'note': note
    }
    results['calls'].append(entry)
    print(f'[{utc}] {name} -> {status} (content-type: {ct}) ({note})', flush=True)

session_id = None

with httpx.Client(timeout=30.0, verify=False) as client:
    # a) initialize
    init_body = {
        'jsonrpc': '2.0',
        'id': 1,
        'method': 'initialize',
        'params': {
            'protocolVersion': '2025-06-18',
            'capabilities': {},
            'clientInfo': {'name': 'jumphost-e2e', 'version': '1.0'}
        }
    }
    try:
        r_init = client.post(url, headers=headers_auth, json=init_body)
        session_id = r_init.headers.get('mcp-session-id') or r_init.headers.get('Mcp-Session-Id')
        note = f'session_id={session_id}' if session_id else 'no session_id'
        log_call('a) initialize', r_init.status_code, r_init.headers, r_init.text, note=note)
    except Exception as e:
        log_call('a) initialize', 0, {}, str(e), note='exception')

    active_headers = dict(headers_auth)
    if session_id:
        active_headers['Mcp-Session-Id'] = session_id

    # b) notifications/initialized
    notif_body = {
        'jsonrpc': '2.0',
        'method': 'notifications/initialized'
    }
    try:
        r_notif = client.post(url, headers=active_headers, json=notif_body)
        log_call('b) notifications/initialized', r_notif.status_code, r_notif.headers, r_notif.text)
    except Exception as e:
        log_call('b) notifications/initialized', 0, {}, str(e), note='exception')

    # c) tools/list
    list_body = {
        'jsonrpc': '2.0',
        'id': 2,
        'method': 'tools/list'
    }
    tools = []
    try:
        r_list = client.post(url, headers=active_headers, json=list_body)
        tools_note = ''
        try:
            j = r_list.json()
            tools = j.get('result', {}).get('tools', [])
            names = [t.get('name') for t in tools[:5]]
            tools_note = f'tool_count={len(tools)}, first_5={names}'
        except Exception:
            tools_note = 'failed to parse json'
        log_call('c) tools/list', r_list.status_code, r_list.headers, r_list.text, note=tools_note)
    except Exception as e:
        log_call('c) tools/list', 0, {}, str(e), note='exception')

    if run_mode in ('B', 'B2'):
        # d) tools/call generate_pii_ssn_ccn (EXPECT blocked by AI Gateway)
        call_ssn_body = {
            'jsonrpc': '2.0',
            'id': 3,
            'method': 'tools/call',
            'params': {
                'name': 'generate_pii_ssn_ccn',
                'arguments': {'count': 1, 'format': 'json'}
            }
        }
        try:
            r_ssn = client.post(url, headers=active_headers, json=call_ssn_body)
            preview = r_ssn.text[:800]
            log_call('d) tools/call generate_pii_ssn_ccn', r_ssn.status_code, r_ssn.headers, preview, note='EXPECT BLOCKED')
        except Exception as e:
            log_call('d) tools/call generate_pii_ssn_ccn', 0, {}, str(e), note='exception')

        # e) tools/call generate_pii_email (EXPECT allowed)
        call_email_body = {
            'jsonrpc': '2.0',
            'id': 4,
            'method': 'tools/call',
            'params': {
                'name': 'generate_pii_email',
                'arguments': {'count': 1, 'format': 'json'}
            }
        }
        try:
            r_email = client.post(url, headers=active_headers, json=call_email_body)
            preview = r_email.text[:800]
            log_call('e) tools/call generate_pii_email', r_email.status_code, r_email.headers, preview, note='EXPECT ALLOWED')
        except Exception as e:
            log_call('e) tools/call generate_pii_email', 0, {}, str(e), note='exception')

        # f) control: tools/list with 'Authorization: Bearer bogus'
        bogus_headers = {
            **headers_base,
            'Authorization': 'Bearer bogus',
        }
        if session_id:
            bogus_headers['Mcp-Session-Id'] = session_id

        try:
            r_ctrl = client.post(url, headers=bogus_headers, json=list_body)
            log_call('f) control (Bearer bogus)', r_ctrl.status_code, r_ctrl.headers, r_ctrl.text, note='expect 401')
        except Exception as e:
            log_call('f) control (Bearer bogus)', 0, {}, str(e), note='exception')

    else:
        # Run A behaviour
        selected_tool = None
        call_args = {}
        reason = ''
        for t in tools:
            tname = t.get('name', '')
            tdesc = t.get('description', '').lower()
            if any(w in tname.lower() or w in tdesc for w in ['sample', 'test', 'demo', 'ping', 'echo', 'dummy', 'example']):
                selected_tool = tname
                reason = f'tool {tname!r} selected because name/description indicates test/sample: {tdesc[:80]}'
                break

        if not selected_tool and tools:
            selected_tool = tools[0].get('name')
            reason = f'first available tool {selected_tool!r} selected (read-only probe)'

        if selected_tool:
            call_body = {
                'jsonrpc': '2.0',
                'id': 3,
                'method': 'tools/call',
                'params': {
                    'name': selected_tool,
                    'arguments': call_args
                }
            }
            try:
                r_call = client.post(url, headers=active_headers, json=call_body)
                preview = r_call.text[:800]
                log_call('d) tools/call', r_call.status_code, r_call.headers, preview, note=f'{reason} | preview={preview[:100]}')
            except Exception as e:
                log_call('d) tools/call', 0, {}, str(e), note=f'{reason} | exception')
        else:
            log_call('d) tools/call', 0, {}, 'no tools available', note='no tools found in tools/list')

        # e) control: tools/list with 'Authorization: Bearer bogus'
        bogus_headers = {
            **headers_base,
            'Authorization': 'Bearer bogus',
        }
        if session_id:
            bogus_headers['Mcp-Session-Id'] = session_id

        try:
            r_ctrl = client.post(url, headers=bogus_headers, json=list_body)
            log_call('e) control (Bearer bogus)', r_ctrl.status_code, r_ctrl.headers, r_ctrl.text, note='expect 401')
        except Exception as e:
            log_call('e) control (Bearer bogus)', 0, {}, str(e), note='exception')

print('\n--- E2E_JSON_START ---')
print(json.dumps(results, indent=2))
print('--- E2E_JSON_END ---')
"""


def run_e2e(run_mode="A"):
    print(f"Running e2e probe (mode {run_mode}) against {FQDN} in resource group {RG}...")
    jumphost_app = os.environ.get("JUMPHOST_APP", "mcp-jumphost")
    image = subprocess.check_output(
        ["az", "containerapp", "show", "-g", RG, "-n", jumphost_app, "--query", "properties.template.containers[0].image", "-o", "tsv"]
    ).decode().strip()

    mi = subprocess.check_output(
        ["az", "identity", "show", "-g", RG, "-n", "verify-probe-id", "--query", "id", "-o", "tsv"]
    ).decode().strip()

    name = f"probe-e2e-{run_mode.lower()}-{int(time.time())}"
    print(f"Creating ephemeral ACI container {name} in {VNET}/{SUBNET}...")

    cmd = [
        "az", "container", "create",
        "-g", RG,
        "-n", name,
        "--image", image,
        "--os-type", "Linux",
        "--cpu", "1",
        "--memory", "1",
        "--vnet", VNET,
        "--subnet", SUBNET,
        "--restart-policy", "Never",
        "--assign-identity", mi,
        "--acr-identity", mi,
        "--environment-variables", f"JUMPHOST_FQDN={FQDN}", f"PROBE_PY={PROBE_CODE}", f"RUN_MODE={run_mode}",
        "--secure-environment-variables", f"USER_TOKEN={TOKEN}",
        "--command-line", "python -c \"import os; exec(os.environ['PROBE_PY'])\"",
        "-o", "none"
    ]

    try:
        subprocess.check_call(cmd)
        print(f"Container {name} created, waiting for execution...")

        for _ in range(36):
            state = subprocess.check_output(
                ["az", "container", "show", "-g", RG, "-n", name,
                 "--query", "containers[0].instanceView.currentState.state", "-o", "tsv"]
            ).decode().strip()
            if state == "Terminated":
                break
            time.sleep(5)

        time.sleep(2)
        raw_logs = subprocess.check_output(["az", "container", "logs", "-g", RG, "-n", name]).decode("utf-8", "replace")
        print("\n--- ACI CONTAINER LOGS ---")
        print(raw_logs)
        print("--------------------------\n")

        # Parse JSON results
        if "--- E2E_JSON_START ---" in raw_logs:
            part = raw_logs.split("--- E2E_JSON_START ---")[1].split("--- E2E_JSON_END ---")[0].strip()
            res = json.loads(part)
            return res
        return {"error": "JSON output not found in container logs", "raw_logs": raw_logs}
    finally:
        print(f"Deleting ephemeral ACI container {name}...")
        subprocess.call(["az", "container", "delete", "-g", RG, "-n", name, "--yes", "-o", "none"])
        print("Cleanup complete.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Live E2E probe of mcp-jumphost")
    parser.add_argument("--run", choices=["A", "B", "B2"], default="A", help="Probe run mode (A, B, or B2)")
    args = parser.parse_args()

    res = run_e2e(run_mode=args.run)
    print("\nSummary Result:")
    print(json.dumps(res, indent=2))
