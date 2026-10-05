# -*- coding: utf-8 -*-
"""
Toposolid to Void v1.0 - DQT
Creates a void Generic Model with exactly the shape of one toposolid, placed
exactly where the toposolid is - for cutting other elements with
Modify > Cut Geometry.

The toposolid is turned into a small Generic Model family: its solid geometry
becomes an unattached void form, and the family's "Cut with Voids When Loaded"
option is turned on, so the void can cut elements in the project. The family
is loaded into the project and one instance is placed on the toposolid.

The shape is taken from the toposolid's own solid geometry - the finished
terrain as Revit shows it, with its sloped and sculpted top. The family keeps
its origin near the toposolid, not at the project origin, so a site far from
the origin does not end up with distant geometry in the family.

The family is named "Toposolid Void - <toposolid type> (toposolid <id>)".

Workflow:
  1. Select one toposolid (or run with none selected and pick it).
  2. Run - the void is created on the toposolid and selected.
  3. Modify > Cut Geometry: click the element to cut, then the void.

Copyright (c) 2026 Dang Quoc Truong (DQT)
All rights reserved.
"""

__title__ = "Toposolid\nto Void"
__author__ = "Dang Quoc Truong (DQT)"
__doc__ = ("Create a void Generic Model with exactly the shape of a toposolid, "
           "to cut other elements with Cut Geometry.")

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
from Autodesk.Revit.DB import Transaction
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
TITLE = "Toposolid to Void"
FAMILY_PREFIX = "Toposolid Void - "
# "...(toposolid 123)" or "...(toposolid 123) v2" - how a void is traced back
# to its toposolid
FAMILY_NAME_RE = re.compile(r"\(toposolid (\d+)\)(?: v\d+)?$")
TEMP_FOLDER = "DQT_ToposolidToVoid"
MIN_VOLUME_FT3 = 1e-9                    # ignore empty / degenerate solids
MOVE_TOLERANCE_FT = 1e-6
# Characters a family name cannot have: Revit's own list plus the ones a file
# name cannot have (the family is saved to a file under its name to be loaded).
BAD_NAME_CHARS = "\\/:*?\"<>|{}[];`~"
MAX_NAME_PART = 80
FOOTER = "Dang Quoc Truong - DQT (c) 2026"
# Generic Model templates that are NOT the plain, free-standing one.
HOSTED_WORDS = ("face based", "wall based", "ceiling based", "floor based",
                "roof based", "line based", "pattern based", "adaptive",
                "work plane")
# Toposolids exist from Revit 2024.
TOPOSOLID_CLASS = getattr(DB, "Toposolid", None)


class CopyError(Exception):
    """The toposolid cannot be copied, with the reason to show the user."""
    pass


class TemplateError(Exception):
    """The family template cannot be used."""
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


def is_toposolid(element):
    """A Toposolid (sub-divisions included) - not a floor, a topography
    surface or a toposolid type."""
    if element is None or TOPOSOLID_CLASS is None:
        return False
    try:
        return isinstance(element, TOPOSOLID_CLASS)
    except Exception:
        return False


def type_name(document, element):
    """The element type's name, "Toposolid" when it cannot be read."""
    try:
        element_type = document.GetElement(element.GetTypeId())
        param = element_type.get_Parameter(DB.BuiltInParameter.SYMBOL_NAME_PARAM)
        if param is not None and param.AsString():
            return param.AsString()
        return DB.Element.Name.GetValue(element_type)
    except Exception:
        return "Toposolid"


def toposolid_label(document, element):
    """"<type name> (id N)" for messages."""
    return "{0} (id {1})".format(type_name(document, element),
                                 eid_int(element.Id))


def clean_name_part(name):
    """A type name made safe to use inside a family name / file name."""
    text = name or ""
    for ch in BAD_NAME_CHARS:
        text = text.replace(ch, "-")
    text = text.strip().rstrip(".").strip()
    if not text:
        return "Unnamed"
    return text[:MAX_NAME_PART].strip()


def base_family_name(document, element):
    return "{0}{1} (toposolid {2})".format(
        FAMILY_PREFIX, clean_name_part(type_name(document, element)),
        eid_int(element.Id))


def unique_family_name(base, taken):
    """base, or "base v2", "base v3"... - whichever is not in taken (lower-case
    names)."""
    name = base
    number = 1
    while name.lower() in taken:
        number += 1
        name = "{0} v{1}".format(base, number)
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
    instances too."""
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


def copy_origin(element):
    """The point the family is built around, and where it is placed: the
    middle of the toposolid's footprint at its lowest level. Keeping the
    family's origin at the toposolid, instead of the project origin, keeps
    its geometry small even when the site is far from the origin."""
    box = element.get_BoundingBox(None)
    if box is None:
        raise CopyError("the toposolid has no bounding box")
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
# VOIDS MADE EARLIER
# ==============================================================================
def find_existing_copies(document):
    """{toposolid id (int): [instance ElementId, ...]} for the voids this tool
    made before, found through the toposolid id in each family's name. Only
    placed instances count."""
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
                topo_key = by_symbol[symbol_key]
                if topo_key is not None:
                    copies.setdefault(topo_key, []).append(instance.Id)
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
def allow_cut_with_voids(family):
    """Turn on the family's "Cut with Voids When Loaded" - without it the
    void cannot cut anything in the project."""
    param = family.get_Parameter(DB.BuiltInParameter.FAMILY_ALLOW_CUT_WITH_VOIDS)
    if param is None or param.IsReadOnly:
        raise CopyError("the family's \"Cut with Voids When Loaded\" option "
                        "cannot be turned on")
    param.Set(1)


def build_void_family(application, template_path, solids, origin, family_name):
    """Make a family from the template, put the toposolid's solids in it as
    unattached void forms (moved so the family origin is at `origin`), turn on
    Cut with Voids When Loaded, save it as <family_name>.rfa in the temp folder
    and return that path. The family document is always closed again. Raises
    TemplateError for a template that is not a Generic Model one, CopyError
    when the geometry is refused."""
    family_doc = application.NewFamilyDocument(template_path)
    if family_doc is None:
        raise TemplateError("Revit could not open the family template "
                            "{0}".format(template_path))
    try:
        family = family_doc.OwnerFamily
        category = family.FamilyCategory
        if (category is None or
                eid_int(category.Id) != int(DB.BuiltInCategory.OST_GenericModel)):
            raise TemplateError(
                "{0} is not a Generic Model family template.".format(template_path))

        transaction = Transaction(family_doc, "DQT - Toposolid void")
        transaction.Start()
        try:
            shift = DB.Transform.CreateTranslation(origin.Negate())
            for solid in solids:
                form = DB.FreeFormElement.Create(
                    family_doc, DB.SolidUtils.CreateTransformed(solid, shift))
                form.IsSolid = False
            allow_cut_with_voids(family)
            if transaction.Commit() != DB.TransactionStatus.Committed:
                raise CopyError("Revit rejected the toposolid's geometry in "
                                "the family")
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


def element_level(document, element):
    try:
        level = document.GetElement(element.LevelId)
        return level if isinstance(level, DB.Level) else None
    except Exception:
        return None


def place_instance(document, symbol, origin, level):
    """Place the family at `origin`, on `level` (the toposolid's own level)
    when it has one."""
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


def snap_to(document, instance, target):
    """Move the instance so its insertion point is exactly `target`, whatever
    height Revit gave it from the level. Returns True when it was moved."""
    try:
        point = instance.Location.Point
    except Exception:
        return False
    delta = target.Subtract(point)
    if delta.GetLength() <= MOVE_TOLERANCE_FT:
        return False
    DB.ElementTransformUtils.MoveElement(document, instance.Id, delta)
    return True


def write_notes(instance, document, toposolid):
    """Comments says where the void came from; Mark is carried over."""
    try:
        comments = instance.get_Parameter(
            DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
        if comments is not None and not comments.IsReadOnly:
            comments.Set("Void copy of toposolid {0}".format(
                toposolid_label(document, toposolid)))
    except Exception:
        pass
    try:
        source = toposolid.get_Parameter(DB.BuiltInParameter.ALL_MODEL_MARK)
        target = instance.get_Parameter(DB.BuiltInParameter.ALL_MODEL_MARK)
        if (source is not None and target is not None and source.HasValue
                and source.AsString() and not target.IsReadOnly):
            target.Set(source.AsString())
    except Exception:
        pass


def load_and_place(document, toposolid, family_name, path, origin):
    """Load the family file and place one instance of it on the toposolid.
    Runs inside the caller's transaction. Returns the new instance's
    ElementId."""
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
    instance = place_instance(document, symbol, origin,
                              element_level(document, toposolid))
    document.Regenerate()
    snap_to(document, instance, origin)
    write_notes(instance, document, toposolid)
    return instance.Id


def create_void(document, application, template_path, toposolid_id):
    """Copy the toposolid to a void Generic Model. Returns (new instance id,
    family name). The family is built first, with no transaction open on the
    project; then it is loaded and placed in ONE transaction (one Undo). The
    temporary family file is always removed."""
    path = None
    try:
        toposolid = document.GetElement(toposolid_id)
        if toposolid is None:
            raise CopyError("the toposolid no longer exists")
        solids = collect_solids(toposolid.get_Geometry(geometry_options()))
        if not solids:
            raise CopyError("the toposolid has no solid geometry to copy")
        origin = copy_origin(toposolid)
        name = unique_family_name(base_family_name(document, toposolid),
                                  existing_family_names(document))
        path = build_void_family(application, template_path, solids, origin, name)

        transaction = Transaction(document, "DQT - Toposolid to Void")
        transaction.Start()
        try:
            new_id = load_and_place(document, toposolid, name, path, origin)
            if transaction.Commit() != DB.TransactionStatus.Committed:
                raise CopyError("Revit rolled the change back")
        except Exception:
            roll_back_quietly(transaction)
            raise
        return new_id, name
    finally:
        delete_file(path)
        remove_temp_folder_if_empty()


# ==============================================================================
# SELECTION
# ==============================================================================
class ToposolidFilter(ISelectionFilter):
    def AllowElement(self, element):
        return is_toposolid(element)

    def AllowReference(self, reference, position):
        return False


def get_target_toposolid_id():
    """ElementId of the toposolid to copy: the one already selected (when
    exactly one is), or else the one the user picks. None if cancelled."""
    selected = []
    try:
        for element_id in uidoc.Selection.GetElementIds():
            if is_toposolid(doc.GetElement(element_id)):
                selected.append(element_id)
    except Exception:
        pass
    if len(selected) == 1:
        return selected[0]

    try:
        reference = uidoc.Selection.PickObject(
            ObjectType.Element, ToposolidFilter(),
            "Select the toposolid to copy as a void")
    except Exception:
        return None             # Escape
    if is_toposolid(doc.GetElement(reference.ElementId)):
        return reference.ElementId
    return None


# ==============================================================================
# MESSAGES
# ==============================================================================
def show(instruction, content=""):
    dialog = TaskDialog(TITLE)
    dialog.MainInstruction = instruction
    dialog.MainContent = content
    dialog.FooterText = FOOTER
    dialog.Show()


def ask_about_existing(count):
    """True to make another void for a toposolid that already has one."""
    dialog = TaskDialog(TITLE)
    dialog.MainInstruction = ("This toposolid already has {0} void copy(ies) "
                              "made by this tool.".format(count))
    dialog.MainContent = ("Making another one puts a second void exactly on "
                          "top of the first.")
    dialog.AddCommandLink(
        TaskDialogCommandLinkId.CommandLink1,
        "Create another void",
        "Use this after the toposolid changed shape - delete the old void "
        "afterwards.")
    dialog.CommonButtons = TaskDialogCommonButtons.Cancel
    dialog.DefaultButton = TaskDialogResult.Cancel
    dialog.FooterText = FOOTER
    return dialog.Show() == TaskDialogResult.CommandLink1


def done_text(family_name):
    return (
        "The void sits exactly where the toposolid is and is selected now. "
        "Its family \"{0}\" has \"Cut with Voids When Loaded\" on.\n\n"
        "To cut an element with it: Modify > Cut Geometry, click the element "
        "to cut, then click the void. Walls, floors, roofs, ceilings, "
        "structural framing, columns and foundations, and generic models can "
        "be cut.\n\n"
        "The void is a snapshot: it does not follow the toposolid if the "
        "toposolid is edited later.").format(family_name)


# ==============================================================================
# MAIN
# ==============================================================================
def run():
    if doc.IsFamilyDocument:
        show("Open a project first.",
             "Toposolid to Void works in a project, not in a family.")
        return
    if TOPOSOLID_CLASS is None:
        show("This Revit version has no toposolids.",
             "Toposolids exist from Revit 2024.")
        return

    toposolid_id = get_target_toposolid_id()
    if toposolid_id is None:
        return
    label = toposolid_label(doc, doc.GetElement(toposolid_id))

    earlier = find_existing_copies(doc).get(eid_int(toposolid_id))
    if earlier and not ask_about_existing(len(earlier)):
        return

    template_path = find_generic_model_template(app) or ask_for_template()
    if not template_path:
        show("No Generic Model family template was found.",
             "The tool builds the void in a small Generic Model family, and "
             "needs Revit's \"Metric Generic Model.rft\" template for that. "
             "Set its folder in Options > File Locations > Default path for "
             "family templates, then run the tool again.")
        return

    try:
        new_id, family_name = create_void(doc, app, template_path, toposolid_id)
    except TemplateError as error:
        show("The family template cannot be used.", str(error))
        return
    except Exception as error:
        show("The void could not be created.",
             "Toposolid {0} - nothing was changed.\n\n{1}".format(label, error))
        return

    try:
        uidoc.Selection.SetElementIds(List[DB.ElementId]([new_id]))
    except Exception:
        pass
    show("Created a void from toposolid {0}.".format(label),
         done_text(family_name))


run()
