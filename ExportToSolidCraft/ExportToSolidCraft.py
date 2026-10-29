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


class Exporter:
    """Fusion's own result of a step, for steps SolidCraft can't rebuild the same."""

    def __init__(self, design, folder):
        self.design, self.folder = design, folder

    def export(self, index, components):
        import extract
        return extract.export_step(self.design, index, components, self.folder)


def convert(app, ui, doc, out, cli, progress, done):
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
                convert(app, ui, sub, target, cli, progress, done)
            finally:
                doc.activate()
    tl = design.timeline
    data = {"document": doc.name, "units": design.unitsManager.defaultLengthUnits,
            "parameters": extract.parameters(design), "occurrences": extract.components(design),
            "count": tl.count, "steps": []}
    sketches = {}
    progress.maximumValue = max(tl.count, 1)
    for i in range(tl.count):
        progress.message = "%s: reading step %d of %d" % (doc.name, i + 1, tl.count)
        progress.progressValue = i
        adsk.doEvents()
        if progress.wasCancelled:
            raise RuntimeError("cancelled")
        data["steps"] += extract.extract(design, i, i + 1, sketches)["steps"]
    data["looks"] = extract.looks(design)
    with open(os.path.splitext(out)[0] + ".fusion.json", "w") as f:
        json.dump(data, f)
    progress.message = "%s: building in SolidCraft…" % doc.name
    adsk.doEvents()
    def tick(i, n):
        if i % 10 == 0:
            progress.maximumValue = max(n, 1)
            progress.progressValue = i
            progress.message = "%s: building in SolidCraft (%d of %d)" % (doc.name, i, n)
            adsk.doEvents()
    return build.build(data, out, cli, exporter=Exporter(design, folder), tick=tick)


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
            progress = ui.createProgressDialog()
            progress.isCancelButtonShown = True
            progress.show("Export to SolidCraft", "Starting…", 0, 1, 0)
            try:
                report = convert(app, ui, doc, out, cli, progress, set())
            finally:
                progress.hide()
            summary = "\n".join(report[2:4])
            if ui.messageBox("Exported to\n%s\n\n%s\n\nThe full report is next to it. Open it in SolidCraft?" % (out, summary),
                             "Export to SolidCraft", adsk.core.MessageBoxButtonTypes.YesNoButtonType) == adsk.core.DialogResults.DialogYes:
                appl = find_app(cli)
                if platform.system() == "Darwin":
                    if appl and appl.endswith(".app"):
                        subprocess.Popen(["open", "-a", appl, out])
                    elif appl:
                        subprocess.Popen([appl, out], start_new_session=True)
                    else:
                        subprocess.Popen(["open", out])
                else:
                    subprocess.Popen([appl or out, out] if appl else ["cmd", "/c", "start", "", out])
        except Exception:
            if "cancelled" in traceback.format_exc():
                return
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
    panel = ui.allToolbarPanels.itemById("SolidScriptsAddinsPanel")
    c = panel.controls.itemById(CMD_ID) if panel else None
    if c:
        c.deleteMe()
    d = ui.commandDefinitions.itemById(CMD_ID)
    if d:
        d.deleteMe()
