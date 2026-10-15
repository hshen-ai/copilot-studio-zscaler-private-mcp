#!/usr/bin/env python3
"""Check 7 — Copilot Studio conversation transcripts (Dataverse table conversationtranscript).

Reading the table needs the security role "Bot Transcript Viewer" (System Administrator alone is not
enough); a 403 here means the role is missing. Transcripts are written about 30 minutes after a
conversation starts. Prints, per transcript, whether it contains an AI Guard block.

    python3 verify/07-transcripts.py [conversation-id]
Web UI: <DATAVERSE_URL>/main.aspx?pagetype=entitylist&etn=conversationtranscript
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "lib"))
from pp import CFG, bot, dv

b = bot() or sys.exit("agent not found")
s, _, r = dv("GET", "conversationtranscripts?$select=conversationtranscriptid,conversationstarttime,createdon,content"
                    f"&$filter=_bot_conversationtranscriptid_value eq {b['botid']}&$orderby=createdon desc&$top=10")
if s == 403:
    sys.exit("FAIL  403 — assign the 'Bot Transcript Viewer' security role to your user (README, Troubleshooting)")
want = sys.argv[1] if len(sys.argv) > 1 else None
for t in r.get("value", []):
    c = t.get("content") or ""
    print(t["conversationtranscriptid"], "| started", t["conversationstarttime"], "| written", t["createdon"],
          "| AI Guard block:", "blocked by Zscaler AI Guard" in c, "| match:" if want else "", (want in c) if want else "")
print(f"open: {CFG['DATAVERSE_URL']}/main.aspx?pagetype=entitylist&etn=conversationtranscript")
