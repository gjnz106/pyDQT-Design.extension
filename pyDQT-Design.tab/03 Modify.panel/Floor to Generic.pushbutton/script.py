# -*- coding: utf-8 -*-
"""
Floor to Generic v3.0 - DQT
Turns the selected floors into a Model In-Place (Generic Models) with exactly
the floors' shape - an in-place family of the project, which, unlike a floor,
can be joined or cut with a toposolid.

Revit's API cannot create a Model In-Place, and add-in buttons are locked
while the Model In-Place editor is open, so no add-in can build one by itself.
The tool does every part the API can do and leaves the user a few clicks:

  1. It builds a temporary family that holds the floors' solid geometry as
     solid Freeform forms, at the floors' own coordinates, and opens it with
     the forms already selected.
  2. The user copies them (Ctrl+C), goes back to the project, starts Model
     In-Place (Generic Models), pastes with Paste > Aligned to Same Place and
     clicks Finish Model.

An in-place family's origin is the project's internal origin, so forms kept at
the floors' internal coordinates land exactly on the floors when pasted with
Aligned to Same Place.

The shape is taken from each floor's own solid geometry - the finished slab as
Revit shows it - so a sloped or shape-edited floor, a floor with openings and a
floor trimmed by joined walls all come out the same.

Run the tool again while the temporary family is the active document to select
its forms and see the steps again.

Copyright (c) 2026 Dang Quoc Truong (DQT)
All rights reserved.
"""

__title__ = "Floor to\nGeneric"
__author__ = "Dang Quoc Truong (DQT)"
__doc__ = ("Turn the selected floors into a Model In-Place (Generic Models) "
           "with exactly their shape - one that can be joined or cut with a "
           "toposolid.")

# ==============================================================================
# IMPORTS
# ==============================================================================
import os
import tempfile
import clr

clr.AddReference('RevitAPI')
clr.AddReference('RevitAPIUI')

import Autodesk.Revit.DB as DB
from Autodesk.Revit.DB import Transaction, SubTransaction
from Autodesk.Revit.UI import TaskDialog
from Autodesk.Revit.UI.Selection import ObjectType, ISelectionFilter
from System.Collections.Generic import List

uiapp = __revit__
doc = __revit__.ActiveUIDocument.Document
uidoc = __revit__.ActiveUIDocument
app = __revit__.Application

# ==============================================================================
# CONSTANTS
# ==============================================================================
TITLE = "Floor to Generic - Model In-Place"
NAME_PREFIX = "Floor Copy - "            # the in-place name suggested to the user
TEMP_FOLDER = "DQT_FloorToGeneric"
TEMP_PREFIX = "DQT Temp - "              # every temporary family file starts so
MIN_VOLUME_FT3 = 1e-9                    # ignore empty / degenerate solids
# Characters a family / file name cannot have: Revit's own list plus the ones a
# file name cannot have (the temporary family is saved under this name).
BAD_NAME_CHARS = "\\/:*?\"<>|{}[];`~"
MAX_NAME_PART = 80
FOOTER = "Dang Quoc Truong - DQT (c) 2026"
MAX_LISTED = 10
# Generic Model templates that are NOT the plain, free-standing one.
HOSTED_WORDS = ("face based", "wall based", "ceiling based", "floor based",
                "roof based", "line based", "pattern based", "adaptive",
                "work plane")


class CopyError(Exception):
    """A floor that cannot be copied, with the reason to show the user."""
    pass


class TemplateError(Exception):
    """The family template cannot be used - nothing can be copied."""
    pass


# ==============================================================================
# HELPERS
# ==============================================================================
def eid_int(element_id):
    """ElementId -> int across Revit 2024-2027 (.Value vs .IntegerValue)."""
    try:
        return element_id.Value
    except AttributeError:
        return element_id.IntegerValue


def is_floor(element):
    """A Floor instance (slabs on grade, foundation slabs included) - not a
    FloorType, ceiling or toposolid."""
    try:
        return isinstance(element, DB.Floor)
    except Exception:
        return False


def floor_type_name(document, floor):
    """The floor type's name, "Floor" when it cannot be read."""
    try:
        floor_type = document.GetElement(floor.GetTypeId())
        param = floor_type.get_Parameter(DB.BuiltInParameter.SYMBOL_NAME_PARAM)
        if param is not None and param.AsString():
            return param.AsString()
        return DB.Element.Name.GetValue(floor_type)
    except Exception:
        return "Floor"


def floor_label(document, floor):
    """"<type name> (id N)" for messages."""
    return "{0} (id {1})".format(floor_type_name(document, floor),
                                 eid_int(floor.Id))


def clean_name_part(name):
    """A floor type name made safe to use inside a family name / file name."""
    text = name or ""
    for ch in BAD_NAME_CHARS:
        text = text.replace(ch, "-")
    text = text.strip().rstrip(".").strip()
    if not text:
        return "Unnamed"
    return text[:MAX_NAME_PART].strip()


def suggested_name(document, floors):
    """The Model In-Place name to suggest: "Floor Copy - <floor type>" for one
    floor, "Floor Copy - <n> floors" for several."""
    if len(floors) == 1:
        return NAME_PREFIX + clean_name_part(floor_type_name(document, floors[0]))
    return "{0}{1} floors".format(NAME_PREFIX, len(floors))


def temp_file_base(document, floors):
    """File name (no extension) of the temporary family."""
    base = TEMP_PREFIX + suggested_name(document, floors)
    if len(floors) == 1:
        base += " (floor {0})".format(eid_int(floors[0].Id))
    return base


def roll_back_quietly(transaction):
    """Roll back a (sub)transaction that may already have ended (a Commit that
    failed ends it), without letting that hide the original error."""
    try:
        if transaction.HasStarted() and not transaction.HasEnded():
            transaction.RollBack()
    except Exception:
        pass


# ==============================================================================
# TEMPORARY FILES
# ==============================================================================
def temp_folder_path():
    return os.path.join(tempfile.gettempdir(), TEMP_FOLDER)


def temp_folder():
    folder = temp_folder_path()
    if not os.path.isdir(folder):
        os.makedirs(folder)
    return folder


def path_key(path):
    return os.path.normcase(os.path.normpath(path))


def open_document_paths(application):
    """Normalized paths of every document open in this Revit session."""
    keys = set()
    try:
        for document in application.Documents:
            try:
                if document.PathName:
                    keys.add(path_key(document.PathName))
            except Exception:
                continue
    except Exception:
        pass
    return keys


def clear_old_temp_files(folder, open_paths):
    """Remove the temporary families earlier runs left behind - except the
    ones still open in Revit (the user may not have pasted them yet)."""
    try:
        names = os.listdir(folder)
    except Exception:
        return
    for name in names:
        if not (name.startswith(TEMP_PREFIX) and name.lower().endswith(".rfa")):
            continue
        path = os.path.join(folder, name)
        if path_key(path) in open_paths:
            continue
        try:
            os.remove(path)
        except Exception:
            pass                # locked or already gone - left for next time


def free_file_path(folder, base, open_paths):
    """<folder>/<base>.rfa, or "<base> v2.rfa", "v3"... - a path that is not
    on disk and not open in Revit. A path that is still open must never be
    reused: opening it again would just show the old document."""
    path = os.path.join(folder, base + ".rfa")
    number = 1
    while os.path.exists(path) or path_key(path) in open_paths:
        number += 1
        path = os.path.join(folder, "{0} v{1}.rfa".format(base, number))
    return path


def is_temp_family(document):
    """True for a temporary family this tool made (by its folder and name)."""
    try:
        if not document.IsFamilyDocument or not document.PathName:
            return False
        path = document.PathName
        return (path_key(os.path.dirname(path)) == path_key(temp_folder_path())
                and os.path.basename(path).startswith(TEMP_PREFIX))
    except Exception:
        return False


# ==============================================================================
# GEOMETRY
# ==============================================================================
def geometry_options():
    """Finished geometry at Fine detail, with no references or hidden objects
    - nothing a copy needs, and all of it extra work for Revit."""
    options = DB.Options()
    options.DetailLevel = DB.ViewDetailLevel.Fine
    options.ComputeReferences = False
    options.IncludeNonVisibleObjects = False
    return options


def usable_solid(solid):
    try:
        return solid.Volume > MIN_VOLUME_FT3 and solid.Faces.Size > 0
    except Exception:
        return False


def collect_solids(geometry, depth=0):
    """Every non-empty Solid in a GeometryElement, looking inside geometry
    instances too (a floor normally has none, but a family-based or grouped
    slab can)."""
    found = []
    if geometry is None or depth > 4:
        return found
    for item in geometry:
        if isinstance(item, DB.Solid):
            if usable_solid(item):
                found.append(item)
        elif isinstance(item, DB.GeometryInstance):
            try:
                found.extend(collect_solids(item.GetInstanceGeometry(), depth + 1))
            except Exception:
                continue
    return found


def read_floor_solids(document, floor_ids):
    """[(floor, [solid, ...])] for the floors that have solid geometry, and
    the failures for the others."""
    jobs = []
    failures = []
    for floor_id in floor_ids:
        floor = document.GetElement(floor_id)
        if floor is None:
            failures.append("id {0}: the floor no longer exists".format(
                eid_int(floor_id)))
            continue
        try:
            solids = collect_solids(floor.get_Geometry(geometry_options()))
        except Exception as error:
            failures.append("{0}: {1}".format(floor_label(document, floor), error))
            continue
        if not solids:
            failures.append("{0}: the floor has no solid geometry to copy".format(
                floor_label(document, floor)))
            continue
        jobs.append((floor, solids))
    return jobs, failures


# ==============================================================================
# FAMILY TEMPLATE
# ==============================================================================
def template_search_roots(application):
    """Folders to look for the template in: the one set in Options > File
    Locations, then the standard install location for this Revit version."""
    roots = []
    try:
        configured = application.FamilyTemplatePath
        if configured:
            roots.append(configured)
    except Exception:
        pass
    try:
        program_data = os.environ.get("ProgramData")
        version = application.VersionNumber
        if program_data and version:
            roots.append(os.path.join(program_data, "Autodesk",
                                      "RVT " + version, "Family Templates"))
    except Exception:
        pass
    return roots


def template_rank(file_name):
    """How good a match this file is for the plain Generic Model template: 0
    best, None not a candidate (not a Generic Model template, or a hosted /
    adaptive / line-based kind)."""
    base = os.path.splitext(file_name)[0].lower()
    if "generic model" not in base:
        return None
    for word in HOSTED_WORDS:
        if word in base:
            return None
    if base == "metric generic model":
        return 0
    if base == "generic model":
        return 1
    return 2


def find_generic_model_template(application, max_depth=3):
    """Path of the Generic Model family template, or None."""
    searched = set()
    for root in template_search_roots(application):
        key = os.path.normcase(os.path.normpath(root))
        if key in searched or not os.path.isdir(root):
            continue
        searched.add(key)
        best = None
        base_depth = root.rstrip("\\/").count(os.sep)
        for folder, subfolders, files in os.walk(root):
            if folder.count(os.sep) - base_depth >= max_depth:
                del subfolders[:]
            for file_name in files:
                if not file_name.lower().endswith(".rft"):
                    continue
                rank = template_rank(file_name)
                if rank is None:
                    continue
                order = (rank, len(folder), folder.lower(), file_name.lower())
                if best is None or order < best[0]:
                    best = (order, os.path.join(folder, file_name))
        if best is not None:
            return best[1]
    return None


def ask_for_template():
    """Let the user browse for the template when it cannot be found (a
    localized install names it differently). None if they cancel."""
    try:
        clr.AddReference('System.Windows.Forms')
        from System.Windows.Forms import OpenFileDialog, DialogResult
        dialog = OpenFileDialog()
        dialog.Title = ("Pick the Generic Model family template "
                        "(Metric Generic Model.rft)")
        dialog.Filter = "Family templates (*.rft)|*.rft"
        if dialog.ShowDialog() == DialogResult.OK:
            return dialog.FileName
    except Exception:
        pass
    return None


# ==============================================================================
# THE TEMPORARY FAMILY
# ==============================================================================
def build_form_family(application, template_path, document, jobs):
    """Make ONE family from the template holding every floor's solids as
    solid Freeform forms, left at the floors' own (internal) coordinates, and
    save it in the temp folder. A floor whose geometry is refused is skipped
    on its own (sub-transaction), the others still go. The family document is
    always closed again - it is reopened in the user interface afterwards.

    Returns (path, copied floors, form count, failures); path is None when no
    floor could be copied. Raises TemplateError for a template that is not a
    Generic Model one."""
    family_doc = application.NewFamilyDocument(template_path)
    if family_doc is None:
        raise TemplateError("Revit could not open the family template "
                            "{0}".format(template_path))
    try:
        category = family_doc.OwnerFamily.FamilyCategory
        if (category is None or
                eid_int(category.Id) != int(DB.BuiltInCategory.OST_GenericModel)):
            raise TemplateError(
                "{0} is not a Generic Model family template.".format(template_path))

        copied = []
        failures = []
        form_count = 0
        transaction = Transaction(family_doc, "DQT - Floor forms")
        transaction.Start()
        try:
            for floor, solids in jobs:
                step = SubTransaction(family_doc)
                step.Start()
                try:
                    for solid in solids:
                        DB.FreeFormElement.Create(family_doc, solid)
                    step.Commit()
                    copied.append(floor)
                    form_count += len(solids)
                except Exception as error:
                    roll_back_quietly(step)
                    failures.append("{0}: {1}".format(
                        floor_label(document, floor), error))
            if not copied:
                transaction.RollBack()
                return None, [], 0, failures
            if transaction.Commit() != DB.TransactionStatus.Committed:
                raise CopyError("Revit rejected the floors' geometry in the family")
        except Exception:
            roll_back_quietly(transaction)
            raise

        folder = temp_folder()
        open_paths = open_document_paths(application)
        clear_old_temp_files(folder, open_paths)
        path = free_file_path(folder, temp_file_base(document, copied), open_paths)
        options = DB.SaveAsOptions()
        options.OverwriteExistingFile = True
        family_doc.SaveAs(path, options)
        return path, copied, form_count, failures
    finally:
        try:
            family_doc.Close(False)
        except Exception:
            pass


def form_ids(document):
    """ElementIds of the Freeform forms in the temporary family."""
    ids = List[DB.ElementId]()
    for form in DB.FilteredElementCollector(document).OfClass(DB.FreeFormElement):
        ids.Add(form.Id)
    return ids


def select_forms(ui_document):
    """Select the forms (ready for Ctrl+C) and show them in a 3D view.
    Returns how many were selected."""
    ids = form_ids(ui_document.Document)
    try:
        ui_document.Selection.SetElementIds(ids)
    except Exception:
        pass
    try:
        for view in DB.FilteredElementCollector(ui_document.Document) \
                .OfClass(DB.View3D):
            if not view.IsTemplate:
                ui_document.ActiveView = view
                break
    except Exception:
        pass
    try:
        if ids.Count:
            ui_document.ShowElements(ids)
    except Exception:
        pass
    return ids.Count


# ==============================================================================
# SELECTION
# ==============================================================================
class FloorFilter(ISelectionFilter):
    def AllowElement(self, element):
        return is_floor(element)

    def AllowReference(self, reference, position):
        return False


def get_target_floor_ids():
    """ElementIds of the floors to copy: the floors already selected, or else
    the ones the user picks. None if the pick was cancelled."""
    ids = []
    try:
        for element_id in uidoc.Selection.GetElementIds():
            if is_floor(doc.GetElement(element_id)):
                ids.append(element_id)
    except Exception:
        pass
    if ids:
        return ids

    try:
        references = uidoc.Selection.PickObjects(
            ObjectType.Element, FloorFilter(),
            "Select the floors to turn into a Model In-Place, then click Finish")
    except Exception:
        return None             # Escape
    return [reference.ElementId for reference in references
            if is_floor(doc.GetElement(reference.ElementId))]


# ==============================================================================
# MESSAGES
# ==============================================================================
WHY_TEXT = ("Why the manual steps: Revit's API cannot create a Model In-Place, "
            "and add-in buttons are locked while the Model In-Place editor is "
            "open - so the tool prepares the exact shape and Revit's own "
            "Copy / Paste puts it into the in-place family.")


def show(instruction, content="", expanded=""):
    dialog = TaskDialog(TITLE)
    dialog.MainInstruction = instruction
    dialog.MainContent = content
    if expanded:
        dialog.ExpandedContent = expanded
    dialog.FooterText = FOOTER
    dialog.Show()


def steps_text(form_count, project_title, name):
    """The steps from the selected forms to a finished Model In-Place."""
    project = "the project \"{0}\"".format(project_title) if project_title \
        else "the project"
    return (
        "The temporary family is open with its {0} form(s) selected.\n\n"
        "1. Press Ctrl+C.\n"
        "2. Go back to {1} (its tab, or View > Switch Windows).\n"
        "3. Architecture > Component > Model In-Place: pick Generic Models "
        "and give it a name, e.g. \"{2}\".\n"
        "4. Modify > Paste > Aligned to Same Place - the shape lands exactly "
        "on the floor.\n"
        "5. Finish Model. Then close the temporary family (no need to save).\n\n"
        "Run Floor to Generic again here to select the forms and see these "
        "steps again.").format(form_count, project, name)


def failures_text(failures):
    if not failures:
        return ""
    text = "Not copied ({0}):\n".format(len(failures)) + \
        "\n".join(failures[:MAX_LISTED])
    if len(failures) > MAX_LISTED:
        text += "\n..."
    return text


# ==============================================================================
# MAIN
# ==============================================================================
def run_in_temp_family():
    """The temporary family is active: select its forms again, repeat the
    steps."""
    count = select_forms(uidoc)
    if not count:
        show("This temporary family has no floor forms.",
             "Close it, select the floors in the project and run Floor to "
             "Generic again.")
        return
    show("Copy the shape into a Model In-Place.",
         steps_text(count, "", NAME_PREFIX + "..."), WHY_TEXT)


def run():
    if doc.IsFamilyDocument:
        if is_temp_family(doc):
            run_in_temp_family()
        else:
            show("Open a project first.",
                 "Floor to Generic works in a project, not in a family.")
        return

    floor_ids = get_target_floor_ids()
    if floor_ids is None:
        return
    if not floor_ids:
        show("No floor was selected.",
             "Select one or more floors, then run the tool again.")
        return

    jobs, failures = read_floor_solids(doc, floor_ids)
    if not jobs:
        show("No floor shape could be copied.", failures_text(failures))
        return

    template_path = find_generic_model_template(app) or ask_for_template()
    if not template_path:
        show("No Generic Model family template was found.",
             "The tool builds the floor shape in a temporary Generic Model "
             "family, and needs Revit's \"Metric Generic Model.rft\" template "
             "for that. Set its folder in Options > File Locations > Default "
             "path for family templates, then run the tool again.")
        return

    try:
        path, copied, form_count, form_failures = build_form_family(
            app, template_path, doc, jobs)
    except TemplateError as error:
        show("The family template cannot be used.", str(error))
        return
    except Exception as error:
        show("The floor shape could not be prepared.",
             "Nothing was changed.\n\n{0}".format(error))
        return
    failures = failures + form_failures
    if path is None:
        show("No floor shape could be copied.", failures_text(failures))
        return

    name = suggested_name(doc, copied)
    project_title = doc.Title
    try:
        family_uidoc = uiapp.OpenAndActivateDocument(path)
        count = select_forms(family_uidoc)
    except Exception as error:
        show("The floor shape could not be opened.",
             ("It was saved to:\n{0}\n\nOpen it with File > Open > Family, "
              "select every form and press Ctrl+C. Then, in the project: "
              "Model In-Place (Generic Models), Modify > Paste > Aligned to "
              "Same Place, Finish Model.\n\n{1}").format(path, error),
             WHY_TEXT)
        return

    content = steps_text(count or form_count, project_title, name)
    if failures:
        content += "\n\n" + failures_text(failures)
    show("Now copy the shape into a Model In-Place.", content, WHY_TEXT)


run()
