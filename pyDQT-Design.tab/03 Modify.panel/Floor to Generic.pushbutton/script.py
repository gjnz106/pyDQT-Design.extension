# -*- coding: utf-8 -*-
"""
Floor to Generic Model v2.0 - DQT
Creates a Generic Model family instance with exactly the shape of each
selected floor.

Why a family and not a DirectShape: a floor cannot be joined or cut with a
toposolid, and neither can a DirectShape. A family instance can, so each floor
is turned into a small Generic Model family and placed back where the floor is.
(Revit's API cannot create a Model In-Place itself - that editor is user
interface only - so this is the closest thing it can make: a Generic Model
family instance, built from the floor.)

The shape is taken from the floor's own solid geometry - the finished slab as
Revit shows it - so a sloped or shape-edited floor, a floor with openings and a
floor trimmed by joined walls all come out the same. In the family it is a
solid Freeform form.

Each floor gets a family of its own named "Floor Copy - <floor type> (floor
<id>)". The family keeps its origin near the floor, not at the project origin,
so a model far from the origin does not end up with distant geometry.

Workflow:
  1. Select one or more floors (or run with none selected and pick them).
  2. Run - a family is built for each floor, loaded into the project and placed
     exactly on top of it. The new instances are selected.

Copyright (c) 2026 Dang Quoc Truong (DQT)
All rights reserved.
"""

__title__ = "Floor to\nGeneric"
__author__ = "Dang Quoc Truong (DQT)"
__doc__ = ("Create a Generic Model family instance with exactly the shape of "
           "each selected floor - one that can be joined or cut with a "
           "toposolid.")

# ==============================================================================
# IMPORTS
# ==============================================================================
import os
import re
import tempfile
import clr

clr.AddReference('RevitAPI')
clr.AddReference('RevitAPIUI')

import Autodesk.Revit.DB as DB
from Autodesk.Revit.DB import Transaction, TransactionGroup
from Autodesk.Revit.DB.Structure import StructuralType
from Autodesk.Revit.UI import (
    TaskDialog, TaskDialogCommonButtons, TaskDialogCommandLinkId,
    TaskDialogResult
)
from Autodesk.Revit.UI.Selection import ObjectType, ISelectionFilter
from System.Collections.Generic import List

doc = __revit__.ActiveUIDocument.Document
uidoc = __revit__.ActiveUIDocument
app = __revit__.Application

# ==============================================================================
# CONSTANTS
# ==============================================================================
TITLE = "Floor to Generic Model"
FAMILY_PREFIX = "Floor Copy - "
# "...(floor 123)" or "...(floor 123) v2" - how a copy is traced back to its floor
FAMILY_NAME_RE = re.compile(r"\(floor (\d+)\)(?: v\d+)?$")
TEMP_FOLDER = "DQT_FloorToGeneric"
MIN_VOLUME_FT3 = 1e-9                    # ignore empty / degenerate solids
# Characters a family name cannot have: Revit's own list plus the ones a file
# name cannot have (the family is saved to a file under its name to be loaded).
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


def base_family_name(document, floor):
    return "{0}{1} (floor {2})".format(
        FAMILY_PREFIX, clean_name_part(floor_type_name(document, floor)),
        eid_int(floor.Id))


def unique_family_name(base, taken):
    """base, or "base v2", "base v3"... - whichever is not in taken (lower-case
    names). The name returned is added to taken."""
    name = base
    number = 1
    while name.lower() in taken:
        number += 1
        name = "{0} v{1}".format(base, number)
    taken.add(name.lower())
    return name


def temp_folder():
    folder = os.path.join(tempfile.gettempdir(), TEMP_FOLDER)
    if not os.path.isdir(folder):
        os.makedirs(folder)
    return folder


def delete_file(path):
    try:
        if path and os.path.isfile(path):
            os.remove(path)
    except Exception:
        pass


def remove_temp_folder_if_empty():
    try:
        folder = os.path.join(tempfile.gettempdir(), TEMP_FOLDER)
        if os.path.isdir(folder) and not os.listdir(folder):
            os.rmdir(folder)
    except Exception:
        pass


def roll_back_quietly(transaction):
    """Roll back a transaction that may already have ended (a Commit that
    failed ends it), without letting that hide the original error."""
    try:
        if transaction.HasStarted() and not transaction.HasEnded():
            transaction.RollBack()
    except Exception:
        pass


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


def copy_origin(floor):
    """The point the copy's family is built around, and where it is placed: the
    middle of the floor's footprint at its lowest level. Keeping the family's
    origin at the floor, instead of the project origin, keeps its geometry
    small even when the model is far from the origin."""
    box = floor.get_BoundingBox(None)
    if box is None:
        raise CopyError("the floor has no bounding box")
    return DB.XYZ((box.Min.X + box.Max.X) / 2.0,
                  (box.Min.Y + box.Max.Y) / 2.0,
                  box.Min.Z)


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
# COPIES MADE EARLIER
# ==============================================================================
def find_existing_copies(document):
    """{floor id (int): [instance ElementId, ...]} for the Generic Models this
    tool made before, found through the floor id in each family's name. Only
    placed instances count - a family left behind after its instance was
    deleted does not."""
    copies = {}
    by_symbol = {}
    try:
        collector = DB.FilteredElementCollector(document) \
            .OfClass(DB.FamilyInstance) \
            .OfCategory(DB.BuiltInCategory.OST_GenericModel)
        for instance in collector:
            try:
                symbol_key = eid_int(instance.GetTypeId())
                if symbol_key not in by_symbol:
                    name = instance.Symbol.Family.Name
                    match = FAMILY_NAME_RE.search(name)
                    by_symbol[symbol_key] = (
                        int(match.group(1))
                        if match and name.startswith(FAMILY_PREFIX) else None)
                floor_key = by_symbol[symbol_key]
                if floor_key is not None:
                    copies.setdefault(floor_key, []).append(instance.Id)
            except Exception:
                continue
    except Exception:
        pass
    return copies


def existing_family_names(document):
    """Lower-case names of every family already in the project."""
    names = set()
    try:
        for family in DB.FilteredElementCollector(document).OfClass(DB.Family):
            try:
                names.add(family.Name.lower())
            except Exception:
                continue
    except Exception:
        pass
    return names


# ==============================================================================
# BUILD THE FAMILY (in a family document of its own)
# ==============================================================================
def build_family_file(application, template_path, solids, origin, family_name):
    """Make a family from the template, put the floor's solids in it as solid
    Freeform forms (moved so the family origin is at `origin`), save it as
    <family_name>.rfa in the temp folder and return that path. The family
    document is always closed again. Raises TemplateError for a template that
    is not a Generic Model one, CopyError when the geometry is refused."""
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

        transaction = Transaction(family_doc, "DQT - Floor form")
        transaction.Start()
        try:
            shift = DB.Transform.CreateTranslation(origin.Negate())
            for solid in solids:
                DB.FreeFormElement.Create(
                    family_doc, DB.SolidUtils.CreateTransformed(solid, shift))
            if transaction.Commit() != DB.TransactionStatus.Committed:
                raise CopyError("Revit rejected the floor's geometry in the family")
        except Exception:
            roll_back_quietly(transaction)
            raise

        path = os.path.join(temp_folder(), family_name + ".rfa")
        options = DB.SaveAsOptions()
        options.OverwriteExistingFile = True
        family_doc.SaveAs(path, options)
        return path
    finally:
        try:
            family_doc.Close(False)
        except Exception:
            pass


def prepare_families(document, application, template_path, floor_ids):
    """Phase 1, with no transaction open on the project: build one family file
    per floor. Returns (ready, failures); ready is [(floor id, family name,
    path, origin)]. Floors are looked up again by id for each one. A bad
    template stops everything (TemplateError, after removing the files made so
    far)."""
    ready = []
    failures = []
    taken = existing_family_names(document)
    try:
        for floor_id in floor_ids:
            floor = document.GetElement(floor_id)
            if floor is None:
                failures.append("id {0}: the floor no longer exists".format(
                    eid_int(floor_id)))
                continue
            label = floor_label(document, floor)
            try:
                solids = collect_solids(floor.get_Geometry(geometry_options()))
                if not solids:
                    raise CopyError("the floor has no solid geometry to copy")
                origin = copy_origin(floor)
                name = unique_family_name(base_family_name(document, floor), taken)
                path = build_family_file(application, template_path, solids,
                                         origin, name)
                ready.append((floor_id, name, path, origin))
            except TemplateError:
                raise
            except Exception as error:
                failures.append("{0}: {1}".format(label, error))
    except Exception:
        for _, _, path, _ in ready:
            delete_file(path)
        raise
    return ready, failures


# ==============================================================================
# LOAD AND PLACE (in the project)
# ==============================================================================
def find_family(document, family_name):
    for family in DB.FilteredElementCollector(document).OfClass(DB.Family):
        try:
            if family.Name == family_name:
                return family
        except Exception:
            continue
    return None


def floor_level(document, floor):
    try:
        level = document.GetElement(floor.LevelId)
        return level if isinstance(level, DB.Level) else None
    except Exception:
        return None


def place_instance(document, symbol, origin, level):
    """Place the family at `origin` on `level` (the floor's own level, so the
    instance reports the same Level as the floor)."""
    try:
        if level is not None:
            return document.Create.NewFamilyInstance(
                origin, symbol, level, StructuralType.NonStructural)
        return document.Create.NewFamilyInstance(
            origin, symbol, StructuralType.NonStructural)
    except AttributeError:
        # Revit versions that removed Document.Create.NewFamilyInstance
        return DB.FamilyInstance.Create(
            document, symbol.Id, origin, level, StructuralType.NonStructural)


def write_notes(instance, document, floor):
    """Comments says where the copy came from; Mark is carried over."""
    try:
        comments = instance.get_Parameter(
            DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
        if comments is not None and not comments.IsReadOnly:
            comments.Set("Copy of floor {0}".format(floor_label(document, floor)))
    except Exception:
        pass
    try:
        source = floor.get_Parameter(DB.BuiltInParameter.ALL_MODEL_MARK)
        target = instance.get_Parameter(DB.BuiltInParameter.ALL_MODEL_MARK)
        if (source is not None and target is not None and source.HasValue
                and source.AsString() and not target.IsReadOnly):
            target.Set(source.AsString())
    except Exception:
        pass


def load_and_place(document, floor, family_name, path, origin):
    """Load the family file and place one instance of it on the floor. Runs
    inside the caller's transaction. Returns the new instance's ElementId."""
    result = document.LoadFamily(path)
    loaded = result[0] if isinstance(result, tuple) else result
    if not loaded:
        raise CopyError("Revit could not load the family")
    family = find_family(document, family_name)
    if family is None:
        raise CopyError("the loaded family could not be found")
    symbol_ids = list(family.GetFamilySymbolIds())
    if not symbol_ids:
        raise CopyError("the loaded family has no type to place")
    symbol = document.GetElement(symbol_ids[0])
    if not symbol.IsActive:
        symbol.Activate()
    instance = place_instance(document, symbol, origin, floor_level(document, floor))
    write_notes(instance, document, floor)
    return instance.Id


def place_families(document, ready):
    """Phase 2: load each family and place its instance. ONE Undo step for the
    lot (a transaction group), each floor in its own transaction so one that
    fails leaves nothing behind - not even its loaded family - and the others
    still go. Returns (created, failures); created is [(floor id int, new
    ElementId)]."""
    created = []
    failures = []
    group = TransactionGroup(document, "DQT - Floor to Generic Model")
    group.Start()
    try:
        for floor_id, family_name, path, origin in ready:
            floor = document.GetElement(floor_id)
            if floor is None:
                failures.append("id {0}: the floor no longer exists".format(
                    eid_int(floor_id)))
                continue
            label = floor_label(document, floor)
            transaction = Transaction(document, "DQT - Floor to Generic Model")
            transaction.Start()
            try:
                new_id = load_and_place(document, floor, family_name, path, origin)
                if transaction.Commit() != DB.TransactionStatus.Committed:
                    raise CopyError("Revit rolled the change back")
                created.append((eid_int(floor_id), new_id))
            except Exception as error:
                roll_back_quietly(transaction)
                failures.append("{0}: {1}".format(label, error))
        if created:
            group.Assimilate()
        else:
            group.RollBack()
    except Exception:
        if group.HasStarted() and not group.HasEnded():
            group.RollBack()
        raise
    return created, failures


def create_copies(document, application, template_path, floor_ids):
    """Copy each floor to a Generic Model family instance. Returns (created,
    failures). The temporary family files are always removed."""
    ready = []
    created = []
    failures = []
    try:
        ready, failures = prepare_families(document, application,
                                           template_path, floor_ids)
        if ready:
            created, placing_failures = place_families(document, ready)
            failures = failures + placing_failures
    finally:
        for _, _, path, _ in ready:
            delete_file(path)
        remove_temp_folder_if_empty()
    return created, failures


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
            "Select the floors to copy as Generic Models, then click Finish")
    except Exception:
        return None             # Escape
    return [reference.ElementId for reference in references
            if is_floor(doc.GetElement(reference.ElementId))]


# ==============================================================================
# MESSAGES
# ==============================================================================
def show(instruction, content=""):
    dialog = TaskDialog(TITLE)
    dialog.MainInstruction = instruction
    dialog.MainContent = content
    dialog.FooterText = FOOTER
    dialog.Show()


def ask_about_existing(selected_count, existing_count):
    """"skip" / "again", or None to cancel."""
    dialog = TaskDialog(TITLE)
    dialog.MainInstruction = (
        "{0} of the {1} selected floor(s) already have a Generic Model copy "
        "made by this tool.".format(existing_count, selected_count))
    dialog.MainContent = ("Copying them again puts a second Generic Model "
                          "exactly on top of the first.")
    dialog.AddCommandLink(
        TaskDialogCommandLinkId.CommandLink1,
        "Skip the floors that already have a copy",
        "Only the {0} other floor(s) are copied.".format(
            selected_count - existing_count))
    dialog.AddCommandLink(
        TaskDialogCommandLinkId.CommandLink2,
        "Copy all of them again",
        "Use this after the floor changed shape - delete the old copy "
        "afterwards.")
    dialog.CommonButtons = TaskDialogCommonButtons.Cancel
    dialog.DefaultButton = TaskDialogResult.CommandLink1
    dialog.FooterText = FOOTER
    result = dialog.Show()
    if result == TaskDialogResult.CommandLink1:
        return "skip"
    if result == TaskDialogResult.CommandLink2:
        return "again"
    return None


def summary_text(created, failures, skipped):
    """(instruction, content) for the result dialog."""
    if created:
        instruction = "Created {0} Generic Model(s).".format(len(created))
        content = ("Each is a Generic Model family instance sitting exactly on "
                   "its floor, and is selected now - move it, or hide the "
                   "original floor to see it. The families are named "
                   "\"{0}<floor type> (floor <id>)\".".format(FAMILY_PREFIX))
    else:
        instruction = "No Generic Model was created."
        content = ""
    if skipped:
        content += "\n\n{0} floor(s) already had a copy and were skipped.".format(skipped)
    if failures:
        content += "\n\nNot copied ({0}):\n".format(len(failures)) + \
            "\n".join(failures[:MAX_LISTED])
        if len(failures) > MAX_LISTED:
            content += "\n..."
    return instruction, content.strip()


# ==============================================================================
# MAIN
# ==============================================================================
def run():
    if doc.IsFamilyDocument:
        show("Open a project first.",
             "Floor to Generic Model works in a project, not in a family.")
        return

    floor_ids = get_target_floor_ids()
    if floor_ids is None:
        return
    if not floor_ids:
        show("No floor was selected.",
             "Select one or more floors, then run the tool again.")
        return

    skipped = 0
    existing = find_existing_copies(doc)
    already = [fid for fid in floor_ids if eid_int(fid) in existing]
    if already:
        choice = ask_about_existing(len(floor_ids), len(already))
        if choice is None:
            return
        if choice == "skip":
            skip_keys = set(eid_int(fid) for fid in already)
            floor_ids = [fid for fid in floor_ids if eid_int(fid) not in skip_keys]
            skipped = len(skip_keys)
            if not floor_ids:
                show("Nothing to copy.",
                     "Every selected floor already has a Generic Model copy.")
                return

    template_path = find_generic_model_template(app) or ask_for_template()
    if not template_path:
        show("No Generic Model family template was found.",
             "The tool builds a small Generic Model family for each floor, and "
             "needs Revit's \"Metric Generic Model.rft\" template for that. Set "
             "its folder in Options > File Locations > Default path for family "
             "templates, then run the tool again.")
        return

    try:
        created, failures = create_copies(doc, app, template_path, floor_ids)
    except TemplateError as error:
        show("The family template cannot be used.", str(error))
        return
    except Exception as error:
        show("The Generic Models could not be created.",
             "Nothing was changed.\n\n{0}".format(error))
        return

    if created:
        try:
            uidoc.Selection.SetElementIds(List[DB.ElementId]([new_id for _, new_id in created]))
        except Exception:
            pass

    instruction, content = summary_text(created, failures, skipped)
    show(instruction, content)


run()
