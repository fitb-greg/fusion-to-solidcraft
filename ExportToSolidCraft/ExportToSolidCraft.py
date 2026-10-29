"""Export to SolidCraft: a button in Fusion (Utilities > Export to SolidCraft) that turns the
open design into a .solidcraft design with its history, and opens it in SolidCraft."""

import adsk.core
import adsk.fusion
import json
import os
import platform
import shutil
import subprocess
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

CMD_ID = "ExportToSolidCraftCmd"
PALETTE_ID = "ExportToSolidCraftStatus"
SETTINGS = os.path.join(os.path.expanduser("~"), ".fusion2solidcraft.json")
_handlers = []


def settings():
    try:
        with open(SETTINGS) as f:
            return json.load(f)
    except Exception:
        return {}


def save_settings(s):
    with open(SETTINGS, "w") as f:
        json.dump(s, f, indent=1)


def find_cli(ui):
    """solidcraft-cli: remembered, next to an installed SolidCraft, or picked once."""
    s = settings()
    candidates = [s.get("cli"),
                  shutil.which("solidcraft-cli"),
                  "/Applications/SolidCraft.app/Contents/MacOS/solidcraft-cli",
                  "/opt/homebrew/bin/solidcraft-cli",
                  "/usr/local/bin/solidcraft-cli",
                  os.path.expanduser("~/.cargo/bin/solidcraft-cli")]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    dlg = ui.createFileDialog()
    dlg.title = "Where is solidcraft-cli?"
    if dlg.showOpen() != adsk.core.DialogResults.DialogOK:
        return None
    s["cli"] = dlg.filename
    save_settings(s)
    return dlg.filename


def find_app(cli):
    for c in (os.path.join(os.path.dirname(cli), "solidcraft"), "/Applications/SolidCraft.app"):
        if os.path.exists(c):
            return c
    return None


def open_in_solidcraft(cli, out):
    appl = find_app(cli)
    if platform.system() == "Darwin":
        if appl and appl.endswith(".app"):
            subprocess.Popen(["open", "-a", appl, out])
        elif appl:
            subprocess.Popen([appl, out], start_new_session=True)
        else:
            subprocess.Popen(["open", out])
    else:
        subprocess.Popen([appl, out] if appl else ["cmd", "/c", "start", "", out])


def reveal(path):
    if platform.system() == "Darwin":
        subprocess.Popen(["open", path] if os.path.isdir(path) else ["open", "-R", path])
    elif os.path.isdir(path):
        subprocess.Popen(["explorer", path])
    else:
        subprocess.Popen(["explorer", "/select,", path])


class StatusHandler(adsk.core.HTMLEventHandler):
    def notify(self, args):
        a = adsk.core.HTMLEventArgs.cast(args)
        st = _status[0]
        try:
            if st is None:
                pass
            elif a.action == "ready":
                st.ready = True
            elif a.action == "open" and st.out:
                open_in_solidcraft(st.cli, st.out)
            elif a.action == "report" and st.out:
                reveal_file = os.path.splitext(st.out)[0] + ".report.txt"
                subprocess.Popen(["open", "-t", reveal_file] if platform.system() == "Darwin" else ["notepad", reveal_file])
            elif a.action == "folder" and st.out:
                reveal(st.out)
            a.returnData = "OK"
        except Exception:
            a.returnData = traceback.format_exc()


class Status:
    """The status window: what the export is doing, what came in as Fusion's result, and at the
    end buttons to open the result. It stays open after the export."""

    def __init__(self, ui, cli):
        self.ui, self.cli, self.out, self.ready = ui, cli, None, False
        self.seen, self.fusion, self.failed = set(), 0, 0
        _status[0] = self
        pal = ui.palettes.itemById(PALETTE_ID)
        if pal:
            self.ready = True
        else:
            pal = ui.palettes.add(PALETTE_ID, "Export to SolidCraft", os.path.join(HERE, "status.html"),
                                  True, True, True, 380, 520)
            pal.dockingState = adsk.core.PaletteDockingStates.PaletteDockStateFloating
            h = StatusHandler()
            pal.incomingFromHTML.add(h)
            _handlers.append(h)
        _status[0] = self
        self.palette = pal
        pal.isVisible = True
        import time
        t = time.time()
        while not self.ready and time.time() - t < 5:  # the page loads in the background
            adsk.doEvents()
            time.sleep(0.02)
        self.ready = True  # if it never said so, send anyway
        self.show(reset=True)

    def show(self, **m):
        if self.ready:
            try:
                self.palette.sendInfoToHTML("show", json.dumps(m))
            except Exception:
                pass
        adsk.doEvents()

    def line(self, kind, text):
        if kind == "fusion":
            self.fusion += 1
        elif kind == "failed":
            self.failed += 1
        self.show(lines=[[kind, text]], fusion=self.fusion, failed=self.failed,
                  kept=max(len(self.seen) - self.fusion, 0))


_status = [None]


class Exporter:
    """Fusion's own result of a step, for steps SolidCraft can't rebuild the same."""

    def __init__(self, design, folder):
        self.design, self.folder = design, folder

    def export(self, index, components):
        import extract
        return extract.export_step(self.design, index, components, self.folder)


def convert(app, ui, doc, out, cli, progress, done, status):
    """Convert one document (its linked designs first) to `out`; returns the report lines."""
    import extract
    import build
    if app.activeDocument != doc:
        doc.activate()
    design = adsk.fusion.Design.cast(doc.products.itemByProductType("DesignProductType"))
    folder = os.path.dirname(out)
    # Linked designs become .solidcraft files next to this one, linked the same way.
    for occ in design.rootComponent.allOccurrences:
        if occ.isReferencedComponent and occ.documentReference:
            name = occ.documentReference.dataFile.name
            target = os.path.join(folder, name + ".solidcraft")
            if name in done or os.path.exists(target):
                continue
            done.add(name)
            sub = app.documents.open(occ.documentReference.dataFile, True)
            try:
                convert(app, ui, sub, target, cli, progress, done, status)
            finally:
                doc.activate()
    tl = design.timeline
    status.seen, status.fusion, status.failed = set(), 0, 0
    status.show(title=doc.name, phase="Reading the timeline…", value=0, max=1, kept="–", fusion=0, failed=0)
    data = {"document": doc.name, "units": design.unitsManager.defaultLengthUnits,
            "parameters": extract.parameters(design), "occurrences": extract.components(design),
            "count": tl.count, "steps": []}
    sketches = {}
    progress.maximumValue = max(tl.count, 1)
    for i in range(tl.count):
        progress.message = "%s: reading step %d of %d" % (doc.name, i + 1, tl.count)
        progress.progressValue = i
        status.show(phase="Reading step %d of %d: %s" % (i + 1, tl.count, tl.item(i).name), value=i, max=2 * tl.count)
        if progress.wasCancelled:
            raise RuntimeError("cancelled")
        got = extract.extract(design, i, i + 1, sketches)["steps"]
        for st in got:
            if st.get("error"):
                status.line("note", "%s: couldn't read it (%s)" % (st["name"], st["error"]))
        data["steps"] += got
    data["looks"] = extract.looks(design)
    with open(os.path.splitext(out)[0] + ".fusion.json", "w") as f:
        json.dump(data, f)
    progress.message = "%s: building in SolidCraft…" % doc.name
    status.show(phase="Building in SolidCraft…")
    count = max(tl.count, 1)

    def tick(i, n, name):
        if name:
            status.seen.add(name)
        if i % 10 == 0:
            progress.maximumValue = max(n, 1)
            progress.progressValue = i
            progress.message = "%s: building in SolidCraft (%d of %d)" % (doc.name, i, n)
            status.show(phase="Building in SolidCraft: %s" % (name or "finishing"), value=count + count * i / max(n, 1),
                        max=2 * count, kept=max(len(status.seen) - status.fusion, 0))
            if progress.wasCancelled:
                raise RuntimeError("cancelled")
    report = build.build(data, out, cli, exporter=Exporter(design, folder), tick=tick, event=status.line)
    status.show(value=1, max=1, kept=max(len(status.seen) - status.fusion, 0))
    return report


class ExecuteHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        app = adsk.core.Application.get()
        ui = app.userInterface
        try:
            doc = app.activeDocument
            design = adsk.fusion.Design.cast(doc.products.itemByProductType("DesignProductType")) if doc else None
            if design is None:
                ui.messageBox("Open a design first.")
                return
            if design.designType != adsk.fusion.DesignTypes.ParametricDesignType:
                ui.messageBox("This design has no timeline (direct modeling): there is no history to carry over.")
                return
            cli = find_cli(ui)
            if not cli:
                return
            dlg = ui.createFileDialog()
            dlg.title = "Export to SolidCraft"
            dlg.filter = "SolidCraft design (*.solidcraft)"
            dlg.initialFilename = doc.name + ".solidcraft"
            s = settings()
            if s.get("folder") and os.path.isdir(s["folder"]):
                dlg.initialDirectory = s["folder"]
            if dlg.showSave() != adsk.core.DialogResults.DialogOK:
                return
            out = dlg.filename
            if not out.endswith(".solidcraft"):
                out += ".solidcraft"
            s["folder"] = os.path.dirname(out)
            save_settings(s)
            status = Status(ui, cli)
            progress = ui.createProgressDialog()
            progress.isCancelButtonShown = True
            progress.show("Export to SolidCraft", "Starting…", 0, 1, 0)
            try:
                report = convert(app, ui, doc, out, cli, progress, set(), status)
            finally:
                progress.hide()
            status.out = out
            status.show(title=doc.name, phase="Done. Saved to " + out,
                        summary="\n".join(report[2:4]), done=True)
        except Exception:
            st = _status[0]
            if "cancelled" in traceback.format_exc():
                if st:
                    st.show(phase="Cancelled.")
                return
            if st:
                st.show(phase="Failed.", lines=[["failed", traceback.format_exc().strip().splitlines()[-1]]])
            ui.messageBox("Export to SolidCraft failed:\n" + traceback.format_exc())


class CreatedHandler(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        h = ExecuteHandler()
        args.command.execute.add(h)
        _handlers.append(h)


def run(context):
    app = adsk.core.Application.get()
    ui = app.userInterface
    try:
        cmd = ui.commandDefinitions.itemById(CMD_ID) or ui.commandDefinitions.addButtonDefinition(
            CMD_ID, "Export to SolidCraft", "Export this design to SolidCraft with its timeline as editable history.")
        h = CreatedHandler()
        cmd.commandCreated.add(h)
        _handlers.append(h)
        panel = ui.allToolbarPanels.itemById("SolidScriptsAddinsPanel")
        if panel and not panel.controls.itemById(CMD_ID):
            panel.controls.addCommand(cmd).isPromoted = True
    except Exception:
        ui.messageBox("Export to SolidCraft could not start:\n" + traceback.format_exc())


def stop(context):
    ui = adsk.core.Application.get().userInterface
    pal = ui.palettes.itemById(PALETTE_ID)
    if pal:
        pal.deleteMe()
    panel = ui.allToolbarPanels.itemById("SolidScriptsAddinsPanel")
    c = panel.controls.itemById(CMD_ID) if panel else None
    if c:
        c.deleteMe()
    d = ui.commandDefinitions.itemById(CMD_ID)
    if d:
        d.deleteMe()
