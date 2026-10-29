"""Build a SolidCraft design from extract.py's JSON.

    python3 build.py design.json out.solidcraft [--cli PATH]

Runs the translated commands one at a time in a headless SolidCraft session, names the bodies
as Fusion does, checks every body's volume against Fusion's after each step, and saves the
design. A report (out.report.txt) lists what was dropped, approximated or doesn't match.
"""

import json
import math
import os
import shutil
import subprocess
import sys

import translate

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CLI = os.environ.get("SOLIDCRAFT_CLI") or shutil.which("solidcraft-cli") or "solidcraft-cli"


class Session:
    def __init__(self, cli, folder):
        self.p = subprocess.Popen([cli, "mcp", "--headless", "--allow-path", folder], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self.n = 0
        # body -> (volume, centre) SolidCraft measures more than Fusion for Fusion's own result
        # of it: SolidCraft's volume of some freeform faces is off. Later steps compare changes.
        self.offsets = {}
        self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "fusion2solidcraft", "version": "1"}})
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def send(self, m):
        self.p.stdin.write(json.dumps(m) + "\n")
        self.p.stdin.flush()

    def rpc(self, method, params):
        self.n += 1
        self.send({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params})
        while True:
            line = self.p.stdout.readline()
            if not line:
                raise RuntimeError("SolidCraft stopped")
            m = json.loads(line)
            if m.get("id") == self.n:
                return m

    def tool(self, name, args):
        res = self.rpc("tools/call", {"name": name, "arguments": args}).get("result", {})
        text = "".join(c.get("text", "") for c in res.get("content", []))
        try:
            body = json.loads(text)
        except ValueError:
            body = text
        return not res.get("isError"), body

    def execute(self, command, params):
        return self.tool("execute", {"command": command, "params": params})

    def bodies(self):
        ok, m = self.tool("measure", {})
        if not ok or not isinstance(m, dict):
            return {}
        return {b["name"]: b for b in m.get("bodies", [])}

    def close(self):
        self.p.stdin.close()
        self.p.wait(timeout=30)


def dist(a, b):
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def rename_pieces(s, pieces, wanted, after, log):
    """Name every piece of a split as Fusion does, by centre of mass (through temporary names,
    since the names are swapped among themselves)."""
    have = s.bodies()
    pieces = [p for p in pieces if p in have and have[p].get("center_of_mass")]
    wanted = [k for k in wanted if k in after and after[k].get("center")]
    pairs = []
    for k in wanted:
        if not pieces:
            break
        p = min(pieces, key=lambda p: dist(have[p]["center_of_mass"], after[k]["center"]))
        pieces.remove(p)
        pairs.append((p, k))
    tmp = []
    for i, (p, k) in enumerate(pairs):
        if p != k:
            s.execute("body.rename", {"body": p, "name": "__piece%d" % i})
            tmp.append(("__piece%d" % i, k))
    for p, k in tmp:
        ok, r = s.execute("body.rename", {"body": p, "name": k})
        if not ok:
            log.append("could not name a split piece %s: %s" % (k, str(r)[:150]))


def inside_box(p, box, pad=0.01):
    return all(box[0][i] - pad <= p[i] <= box[1][i] + pad for i in range(3))


def name_new_bodies(s, created, fusion_new, after, log):
    """Give the bodies a step created Fusion's names, matched by centre of mass. Where Fusion made
    one body of several touching profiles and SolidCraft one body each, they are joined first."""
    if not created:
        return
    have = s.bodies()
    wanted = [k for k in fusion_new if k in after]
    if len(created) > len(wanted) and wanted:
        groups = {}
        for c in created:
            b = have.get(c)
            if not b or b.get("center_of_mass") is None:
                continue
            home = [k for k in wanted if after[k].get("bbox") and inside_box(b["center_of_mass"], after[k]["bbox"])]
            key = home[0] if len(home) == 1 else min(wanted, key=lambda k: dist(after[k].get("center", [0, 0, 0]), b["center_of_mass"]))
            groups.setdefault(key, []).append(c)
        merged = []
        for key, members in groups.items():
            if len(members) > 1:
                ok, r = s.execute("solid.combine", {"target": members[0], "tools": members[1:], "operation": "join"})
                if not ok:
                    log.append("could not join %s into one body as Fusion has it: %s" % (members, str(r)[:200]))
                    merged += members
                    continue
            merged.append(members[0])
        created = merged
        have = s.bodies()
    for c in created:
        b = have.get(c)
        if not b or b.get("center_of_mass") is None or not wanted:
            continue
        best = min(wanted, key=lambda k: dist(after[k].get("center", [0, 0, 0]), b["center_of_mass"]))
        wanted.remove(best)
        if best != c:
            ok, r = s.execute("body.rename", {"body": c, "name": best})
            if not ok:
                log.append("could not name %s as %s: %s" % (c, best, r))


def matched_profiles(s, params, match, log, step):
    """The command with its profiles named by SolidCraft's own profile indices, each the one
    whose area and centre match Fusion's."""
    ok, r = s.execute("sketch.inspect", {"sketch": params["sketch"]})
    if not ok or not isinstance(r, dict):
        return None
    have = r.get("profiles", [])
    picks = []
    for m in match:
        best, best_score = None, None
        for p in have:
            a, c = p.get("area_mm2"), p.get("centroid_sketch")
            if a is None or c is None:
                continue
            da = abs(a - m["area"]) / max(m["area"], 1e-9)
            dc = dist(c, m["centroid"])
            if da < 1e-3 and dc < 0.01 + 1e-4 * math.sqrt(m["area"]):
                score = da + dc
                if best_score is None or score < best_score:
                    best, best_score = p["index"], score
        if best is None:
            log.append("step %s: a profile (area %.1f mm2) has no match in SolidCraft's sketch" % (step, m["area"]))
            return None
        picks.append(best)
    return dict(params, profiles=picks, exact_regions=True)


def totals(s):
    """All bodies' volume and its moment about the origin."""
    v, m = 0.0, [0.0, 0.0, 0.0]
    for b in s.bodies().values():
        if b.get("volume_mm3") and b.get("center_of_mass"):
            v += b["volume_mm3"]
            m = [a + b["volume_mm3"] * c for a, c in zip(m, b["center_of_mass"])]
    return [v] + m


def delta_matches(before, now, want):
    got = [a - b for a, b in zip(now, before)]
    tol_v = 0.005 * abs(want[0]) + 0.5
    tol_m = 0.005 * math.sqrt(sum(x * x for x in want[1:])) + 0.05 * abs(want[0]) + 5.0
    return abs(got[0] - want[0]) <= tol_v and math.sqrt(sum((a - b) ** 2 for a, b in zip(got[1:], want[1:]))) <= tol_m


def measured(s, k, b):
    """SolidCraft's volume and centre of body `k`, less what it measured more than Fusion when
    Fusion's result came in."""
    sv, c = b.get("volume_mm3"), b.get("center_of_mass")
    off = s.offsets.get(k)
    if off and sv is not None:
        sv -= off[0]
        if c:
            c = [a - d for a, d in zip(c, off[1])]
    return sv, c


def mismatched(s, after, skip=(), tol=0.002):
    """Bodies that differ from Fusion's (wrong volume or place, or missing), and bodies Fusion
    doesn't have."""
    have = s.bodies()
    wrong = []
    for k, v in after.items():
        if k.split("/", 1)[0] in skip or v.get("volume") is None:
            continue
        b = have.get(k)
        sv, c = measured(s, k, b) if b else (None, None)
        if (b is None or sv is None or abs(sv - v["volume"]) > tol * max(abs(v["volume"]), 1.0)
                or (v.get("center") and c and dist(v["center"], c) > 0.05 + 1e-4 * v["volume"] ** (1 / 3))):
            wrong.append(k)
    extra = [k for k in have if k not in after and k.split("/", 1)[0] not in skip]
    return wrong, extra


def rename_strays(s, after, wrong, extra):
    """A body SolidCraft has under another name (moved between components) is renamed, not
    replaced: matched by volume and place."""
    have = s.bodies()
    for x in list(extra):
        b = have.get(x, {})
        if b.get("volume_mm3") is None:
            continue
        for k in list(wrong):
            v = after.get(k, {})
            if (v.get("volume") and abs(b["volume_mm3"] - v["volume"]) <= 0.002 * v["volume"]
                    and dist(v.get("center", [0, 0, 0]), b.get("center_of_mass", [1e9] * 3)) < 0.1 + 1e-4 * v["volume"] ** (1 / 3)):
                ok, _ = s.execute("body.rename", {"body": x, "name": k})
                if ok:
                    wrong.remove(k)
                    extra.remove(x)
                break


def replace_with_fusion(s, exporter, step, wrong, extra, log):
    """Put Fusion's own result for these bodies in place (no history for them up to here)."""
    rename_strays(s, step["after"], wrong, extra)
    extra = [x for x in extra if x in s.bodies()]
    if extra:
        ok, r = s.execute("solid.remove", {"bodies": extra})
        if not ok:
            log.append("step %d: removing %s failed: %s" % (step["index"], extra, str(r)[:200]))
    if not wrong:
        return True
    comps = sorted({k.split("/", 1)[0] for k in wrong})
    try:
        files = exporter.export(step["index"], comps)
    except Exception as e:
        log.append("step %d: Fusion's result could not be exported: %s" % (step["index"], str(e)[:200]))
        return False
    present = [k for k in wrong if k in s.bodies()]
    if present:
        # A removed body keeps its name in the timeline: free it for Fusion's result first.
        old = []
        for k in present:
            ok, _ = s.execute("body.rename", {"body": k, "name": "%s (before step %d)" % (k, step["index"])})
            old.append("%s (before step %d)" % (k, step["index"]) if ok else k)
        s.execute("solid.remove", {"bodies": old})
    done = True
    # A component can come in more than one file (bodies Fusion won't show come separately).
    wants = {f["component"]: {k.split("/", 1)[1]: k for k in wrong if k.split("/", 1)[0] == f["component"]} for f in files}
    for f in files:
        want = wants[f["component"]]
        ok, r = s.execute("file.insert_step", {"path": f["path"], "name": "%s from Fusion" % step["name"], "matrix": f["matrix"], "names": "solid"})
        if not ok:
            log.append("step %d: importing Fusion's result failed: %s" % (step["index"], str(r)[:200]))
            done = False
            continue
        # Unique names at once: a STEP name like "Body1" may come again in a later import,
        # and SolidCraft's later features find bodies by name.
        created = []
        for c in r.get("created_bodies", []):
            u = "%s [Fusion step %d]" % (c, step["index"])
            ok2, _ = s.execute("body.rename", {"body": c, "name": u})
            created.append((c, u if ok2 else c))
        drop = []
        have = s.bodies()
        if os.environ.get("F2S_DEBUG"):
            print("import step %d: created %s; want %s" % (step["index"], r.get("created_bodies"), want), file=sys.stderr, flush=True)
        after = step["after"]
        for orig, c in created:
            b = have.get(c, {})
            target = want.pop(orig, None) if orig in want else None
            if target is None and b.get("volume_mm3") is not None:
                # Fusion's STEP names duplicates its own way: find it by volume and place.
                for name, k in list(want.items()):
                    v = after.get(k, {})
                    if (v.get("volume") and abs(b["volume_mm3"] - v["volume"]) <= 0.002 * v["volume"]
                            and dist(v.get("center", [0, 0, 0]), b.get("center_of_mass", [1e9] * 3)) < 0.1 + 1e-4 * v["volume"] ** (1 / 3)):
                        target = want.pop(name)
                        break
            if target is None:
                drop.append(c)
                continue
            ok, r2 = s.execute("body.rename", {"body": c, "name": target})
            if not ok:
                log.append("step %d: could not name Fusion's %s: %s" % (step["index"], target, str(r2)[:150]))
                done = False
                continue
            s.offsets.pop(target, None)
            v = after.get(target, {})
            if b.get("volume_mm3") is not None and v.get("volume") and abs(b["volume_mm3"] - v["volume"]) > 0.002 * v["volume"]:
                s.offsets[target] = (b["volume_mm3"] - v["volume"],
                                     [a - d for a, d in zip(b.get("center_of_mass") or v.get("center", [0, 0, 0]), v.get("center", [0, 0, 0]))])
                log.append("step %d: SolidCraft measures Fusion's own %s %.1f%% %s than Fusion does; later steps compare changes"
                           % (step["index"], target, 100 * abs(b["volume_mm3"] / v["volume"] - 1), "smaller" if b["volume_mm3"] < v["volume"] else "larger"))
        if drop:
            ok, r = s.execute("solid.remove", {"bodies": drop})
            if not ok:
                log.append("step %d: removing the rest of Fusion's export %s failed: %s" % (step["index"], drop, str(r)[:200]))
    for want in wants.values():
        if want:
            log.append("step %d: Fusion's export lacked %s" % (step["index"], list(want.values())))
            done = False
    return done


def compare(s, after, step, log, tol=0.002, skip=()):
    have = s.bodies()
    bad = []
    for k, v in after.items():
        if k.split("/", 1)[0] in skip:
            continue
        fv = v.get("volume")
        if fv is None:
            continue
        b = have.get(k)
        if b is None:
            bad.append("%s missing" % k)
            continue
        sv, c = measured(s, k, b)
        if sv is None or abs(sv - fv) > tol * max(abs(fv), 1.0):
            bad.append("%s volume %s, Fusion %.1f" % (k, "open" if sv is None else "%.1f" % sv, fv))
        elif v.get("center") and c and dist(v["center"], c) > 0.05 + 1e-4 * fv ** (1 / 3):
            bad.append("%s is %.1f mm from where Fusion has it" % (k, dist(v["center"], c)))
    extra = [k for k in have if k not in after and k.split("/", 1)[0] not in skip and not k.endswith(":%s)" % k.rsplit(":", 1)[-1][:-1])]
    if extra:
        bad.append("extra bodies %s" % extra)
    return bad


class Watched(list):
    """A list of report lines that also tells `event` about each one as it comes."""

    def __init__(self, kind, event):
        super().__init__()
        self.kind, self.event = kind, event

    def append(self, m):
        super().append(m)
        if self.event:
            self.event(self.kind, m)


def build(data, out, cli=DEFAULT_CLI, check_every_step=True, exporter=None, tick=None, event=None):
    """`tick(i, n, step_name)` is called before each command, `event(kind, line)` for each step
    brought in as Fusion's result ("fusion") and each failure ("failed")."""
    t = translate.Translator(data)
    commands, notes = t.run(), t.notes
    skip = t.linked_components()
    folder = os.path.dirname(os.path.abspath(out))
    for c in commands:  # a linked design is converted on its own first, next to this one
        p = c["params"].get("path")
        if c["command"] == "component.insert_design" and isinstance(p, dict):
            c["params"]["path"] = os.path.join(folder, p["linked"] + ".solidcraft")
            c["params"]["translate"] = [float(v.split()[0]) for v in c["params"]["translate"]]
            if not os.path.exists(c["params"]["path"]):
                notes.append((c["step"], "linked design %s not converted yet: convert it first" % p["linked"]))
    steps = {st["index"]: st for st in data["steps"]}
    s = Session(cli, os.path.dirname(os.path.abspath(out)))
    log, dropped = [], []
    failed, replaced = Watched("failed", event), Watched("fusion", event)
    checked = set()
    end_checked = False
    occ_ids = {}  # component name -> occurrence id of an inserted design
    sketch_ids = {}  # sketch name -> SolidCraft feature id
    final_names = {}  # "component/body" -> the plain name it got at the end
    last_step = None
    try:
        for i, c in enumerate(commands):
            step = c.get("step")
            if tick:
                tick(i, len(commands), steps.get(step, {}).get("name"))
            if check_every_step and last_step is not None and step != last_step and last_step not in checked and "after" in steps.get(last_step, {}):
                checked.add(last_step)
                st = steps[last_step]
                wrong, extra = mismatched(s, st["after"], skip)
                if (wrong or extra) and os.environ.get("F2S_DEBUG"):
                    print("step %d mismatch %s extra %s" % (last_step, wrong, extra), file=sys.stderr, flush=True)
                if (wrong or extra) and exporter is not None:
                    if replace_with_fusion(s, exporter, st, wrong, extra, log):
                        replaced.append("step %d %s: %s" % (last_step, st["name"], ", ".join(wrong) or "extra bodies removed"))
                        wrong, extra = mismatched(s, st["after"], skip)
                if wrong or extra:
                    log.append("after step %d %s: %s" % (last_step, st["name"], "; ".join(compare(s, st["after"], last_step, log, skip=skip))))
            last_step = step if step is not None else last_step
            if step is None and last_step is not None and not end_checked and exporter is not None and "after" in data["steps"][-1]:
                end_checked = True
                for _ in range(2):  # the end state, once more, before components and copies are set up
                    wrong, extra = mismatched(s, data["steps"][-1]["after"], skip)
                    if not (wrong or extra):
                        break
                    replace_with_fusion(s, exporter, data["steps"][-1], wrong, extra, log)
            if c["command"] == "fusion.fallback":
                if exporter is None:
                    failed.append("step %s %s: no SolidCraft equivalent yet" % (step, c.get("step_name")))
                continue  # the check after the step brings in Fusion's result
            params = c["params"]
            if "paths" in params:  # placements named by component, from earlier replies
                params = dict(params, paths=[[occ_ids.get(p[len("@occurrence:"):], p) if isinstance(p, str) and p.startswith("@occurrence:") else p
                                              for p in path] for path in params["paths"]])
            if c["command"] == "solid.split_body" and params.get("sketch") in sketch_ids:
                params = dict(params, sketch=sketch_ids[params["sketch"]])
            if c.get("match"):
                params = matched_profiles(s, params, c["match"], log, step) or params
            before = totals(s) if c.get("tries") and c.get("delta") else None
            ok, r = s.execute(c["command"], params)
            first = r
            if ok and before is not None and not delta_matches(before, totals(s), c["delta"]):
                # Built, but not as Fusion has it: try the other ways (flipped, axis reversed).
                for t in c["tries"]:
                    if not t or not s.tool("undo", {})[0]:
                        break
                    ok2, r2 = s.execute(c["command"], t)
                    if ok2 and delta_matches(before, totals(s), c["delta"]):
                        log.append("step %s %s: built the other way round" % (step, c["command"]))
                        r = r2
                        break
                    if ok2:
                        s.tool("undo", {})
                    ok3, r3 = s.execute(c["command"], params)
                    if not ok3:
                        ok, r = ok2, r2
                        break
                    r = r3
            for alt in c.get("alts") or []:
                if ok:
                    break
                if alt:
                    if c.get("match"):
                        alt = dict(alt, profiles=params["profiles"]) if params is not c["params"] else alt
                    ok, r = s.execute(c["command"], alt)
                    if ok:
                        log.append("step %s %s: done another way (%s)" % (step, c["command"], str(first)[:120]))
            if not ok:
                msg = "step %s %s %s: %s" % (step, c["command"], json.dumps(c["params"])[:200], str(r)[:300])
                (dropped if c["optional"] else failed).append(msg)
                continue
            if c.get("then_move") and isinstance(r, dict) and r.get("occurrence") is not None:
                ok2, r2 = s.execute("occurrence.move", {"occurrence": r["occurrence"], "matrix": c["then_move"], "space": "world"})
                if not ok2:
                    failed.append("placing a copy of %s: %s" % (c["params"]["component"], str(r2)[:200]))
            if c["command"] == "body.rename" and step is None:
                final_names[c["params"]["body"]] = c["params"]["name"]
                if c["params"]["body"] in s.offsets:
                    s.offsets[c["params"]["name"]] = s.offsets.pop(c["params"]["body"])
            if c["command"] == "component.insert_design" and isinstance(r, dict) and r.get("occurrence") is not None:
                occ_ids[c["params"].get("name")] = r["occurrence"]
            if c["command"] == "sketch.create" and isinstance(r, dict) and r.get("sketch") is not None:
                sketch_ids[c["params"].get("name")] = r["sketch"]
            if isinstance(r, dict) and c.get("renames"):
                rename_pieces(s, [c["params"]["body"]] + r.get("created_bodies", []), c["renames"], steps[step].get("after", {}), log)
            elif isinstance(r, dict) and "new" in c:
                name_new_bodies(s, r.get("created_bodies", []), c["new"], steps[step].get("after", {}), log)
        final = data["steps"][-1].get("after", {}) if data["steps"] else {}
        final = {final_names.get(k, k): v for k, v in final.items()}

        mismatch = compare(s, final, None, log, skip=skip)
        ok, r = s.tool("save", {"path": os.path.abspath(out)})
        if not ok:
            failed.append("save: %s" % r)
    finally:
        s.close()
    report = ["%s -> %s" % (data["document"], out), "",
              "%d commands, %d failed, %d optional dropped, %d steps brought in as Fusion's result" % (len(commands), len(failed), len(dropped), len(replaced)),
              "final bodies: %s" % ("all match Fusion" if not mismatch else "; ".join(mismatch)), ""]
    report += ["FUSION RESULT (no history) " + m for m in replaced]
    report += ["FAILED: " + m for m in failed]
    report += ["NOTE step %s: %s" % n for n in notes]
    report += ["CHECK " + m for m in log]
    report += ["dropped: " + m for m in dropped]
    with open(os.path.splitext(out)[0] + ".report.txt", "w") as f:
        f.write("\n".join(report) + "\n")
    return report


if __name__ == "__main__":
    args = sys.argv[1:]
    cli = DEFAULT_CLI
    if "--cli" in args:
        i = args.index("--cli")
        cli = args[i + 1]
        del args[i:i + 2]
    data = json.load(open(args[0]))
    exporter = None
    if "--fusion" in args:  # development: Fusion's results come through FusionMCPBridge
        args.remove("--fusion")
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "dev"))
        import bridge
        exporter = bridge.BridgeExporter(data["document"], os.path.dirname(os.path.abspath(args[1])))
    print("\n".join(build(data, args[1], cli, exporter=exporter)[:60]))
