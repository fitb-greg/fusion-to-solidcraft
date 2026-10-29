"""Calls into Fusion through FusionMCPBridge (development; the add-in calls extract directly)."""
import json, os, pathlib, urllib.request

HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ExportToSolidCraft")


def run(script):
    tok = pathlib.Path.home().joinpath(".fusion-mcp-secret").read_text().strip()
    body = json.dumps({"script": script}).encode()
    req = urllib.request.Request("http://127.0.0.1:7654/execute", body, {"Authorization": "Bearer " + tok, "Content-Type": "application/json"})
    try:
        out = json.load(urllib.request.urlopen(req, timeout=120))
    except urllib.error.HTTPError as e:
        out = json.load(e)
    return out.get("result") or out.get("output") or json.dumps(out)


class BridgeExporter:
    def __init__(self, document, folder):
        self.document, self.folder = document, folder

    def export(self, index, components):
        script = """
import sys, json, importlib
sys.path.insert(0, %r)
import extract as X; importlib.reload(X)
doc = [d for d in app.documents if d.name == %r][0]
if app.activeDocument != doc: doc.activate()
des = adsk.fusion.Design.cast(doc.products.itemByProductType("DesignProductType"))
print("JSON" + json.dumps(X.export_step(des, %d, %r, %r)))
""" % (HERE, self.document, index, list(components), self.folder)
        import time, sys
        t = time.time()
        r = run(script)
        print("export step %d %s %.1fs" % (index, list(components), time.time() - t), file=sys.stderr, flush=True)
        if "JSON" not in r:
            raise RuntimeError(r[:500])
        return json.loads(r.split("JSON", 1)[1].strip())
