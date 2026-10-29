#!/bin/sh
# Run a Python file inside Fusion via FusionMCPBridge: fx.sh script.py
python3 -c '
import json,sys,urllib.request,pathlib
tok=pathlib.Path.home().joinpath(".fusion-mcp-secret").read_text().strip()
body=json.dumps({"script":open(sys.argv[1]).read()}).encode()
r=urllib.request.Request("http://127.0.0.1:7654/execute",body,{"Authorization":"Bearer "+tok,"Content-Type":"application/json"})
try: out=json.load(urllib.request.urlopen(r,timeout=60))
except urllib.error.HTTPError as e: out=json.load(e)
print(out.get("result") or out.get("output") or json.dumps(out,indent=1))
' "$1"
