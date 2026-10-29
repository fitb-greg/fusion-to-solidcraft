# Driver for fx.sh: extracts timeline steps [START, STOP) of DOC into OUT (appending).
import sys, json, importlib, os
sys.path.insert(0, ADDIN)
import extract as X
if START == 0: importlib.reload(X)
doc = [d for d in app.documents if d.name.startswith(DOC)][0]
if app.activeDocument != doc: doc.activate()
des = adsk.fusion.Design.cast(doc.products.itemByProductType("DesignProductType"))
if START == 0 or not hasattr(sys, "_f2s_state"):
    sys._f2s_state = {"sketches": {}, "steps": []}
st = sys._f2s_state
import time; t = time.time()
r = X.extract(des, START, STOP, st["sketches"])
st["steps"] += r["steps"]
out = {"document": doc.name, "units": des.unitsManager.defaultLengthUnits, "parameters": X.parameters(des),
       "occurrences": X.components(des), "count": des.timeline.count, "steps": st["steps"], "looks": X.looks(des)}
json.dump(out, open(OUT, "w"), indent=1)
print("steps", len(st["steps"]), "of", des.timeline.count, "in %.1fs" % (time.time() - t),
      "errors", [(s["index"], s["error"]) for s in r["steps"] if "error" in s])
