#!/usr/bin/env python3
"""Step 5a — make sure the environment has a Dataverse database (Copilot Studio agents live in it).

    python3 power-platform/01-dataverse.py            # report only
    python3 power-platform/01-dataverse.py --apply    # provision Dataverse if missing (irreversible)

Adding Dataverse to an environment cannot be undone. The reference lab did this to its default
environment; a dedicated environment is the cleaner choice for a production design.
"""
import sys, time, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "lib"))
from pp import BAP, ENV_ID, bap

URL = f"{BAP}/scopes/admin/environments/{ENV_ID}?api-version=2021-04-01"


def state():
    s, _, b = bap("GET", URL)
    p = b.get("properties", {}) if isinstance(b, dict) else {}
    return p.get("databaseType"), (p.get("linkedEnvironmentMetadata") or {})


db, linked = state()
print("databaseType:", db, "| instanceUrl:", linked.get("instanceUrl"), "| instanceState:", linked.get("instanceState"))
if linked.get("instanceUrl"):
    print("OK — Dataverse present. Put instanceUrl (without the trailing slash) into DATAVERSE_URL.")
    sys.exit(0)
if "--apply" not in sys.argv:
    sys.exit("No Dataverse. Re-run with --apply to provision it (irreversible).")
s, _, b = bap("POST", f"{BAP}/environments/{ENV_ID}/provisionInstance?api-version=2021-04-01",
              {"baseLanguage": 1033, "currency": {"code": "USD"}, "templates": []})
print("provisionInstance:", s, b if s >= 300 else "")
for _ in range(30):
    time.sleep(30)
    db, linked = state()
    if linked.get("instanceUrl"):
        print("OK — instanceUrl:", linked["instanceUrl"], "| version:", linked.get("version"))
        break
else:
    print("still provisioning — re-run later to read the URL")
