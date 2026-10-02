# -*- coding: utf-8 -*-
"""
Floor to Generic Model v1.0 - DQT
Creates a Generic Model with exactly the shape of each selected floor.

The copy is made from the floor's own solid geometry - the finished slab as
Revit shows it - so a sloped or shape-edited floor, a floor with openings
(shafts, cut-outs) and a floor trimmed by joined walls all come out the same.
The Generic Model is a DirectShape in the Generic Models category: it can be
scheduled, filtered, hidden, moved or exported, and it does not change when the
floor does.

Each floor type gets its own Generic Model type ("Floor Copy - <floor type>"),
so the copies can be told apart and scheduled by the type they came from.

Workflow:
  1. Select one or more floors (or run with none selected and pick them).
  2. Run - a Generic Model is created exactly on top of each floor and the new
     ones are selected, ready to move.

Copyright (c) 2026 Dang Quoc Truong (DQT)
All rights reserved.
"""

__title__ = "Floor to\nGeneric"
__author__ = "Dang Quoc Truong (DQT)"
__doc__ = ("Create a Generic Model with exactly the shape of each selected "
           "floor.")

# ==============================================================================
# IMPORTS
# ==============================================================================
import clr

clr.AddReference('RevitAPI')
clr.AddReference('RevitAPIUI')

import Autodesk.Revit.DB as DB
from Autodesk.Revit.DB import Transaction, SubTransaction
from Autodesk.Revit.UI import (
    TaskDialog, TaskDialogCommonButtons, TaskDialogCommandLinkId,
    TaskDialogResult
)
from Autodesk.Revit.UI.Selection import ObjectType, ISelectionFilter
from System.Collections.Generic import List

doc = __revit__.ActiveUIDocument.Document
uidoc = __revit__.ActiveUIDocument

# ==============================================================================
# CONSTANTS
# ==============================================================================
TITLE = "Floor to Generic Model"
APP_ID = "DQT.FloorToGenericModel"       # marks the copies this tool made
TYPE_PREFIX = "Floor Copy - "
MIN_VOLUME_FT3 = 1e-9                    # ignore empty / degenerate solids
BAD_NAME_CHARS = "\\:{}[]|;<>?`~"        # characters Revit refuses in a type name
MAX_TYPE_NAME = 80
FOOTER = "Dang Quoc Truong - DQT (c) 2026"
MAX_LISTED = 10


class CopyError(Exception):
    """A floor that cannot be copied, with the reason to show the user."""
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


def clean_type_name(name):
    """A floor type name made safe to reuse as a Generic Model type name."""
    text = name or ""
    for ch in BAD_NAME_CHARS:
        text = text.replace(ch, "-")
    text = text.strip()
    if not text:
        return "Unnamed"
    return text[:MAX_TYPE_NAME].strip()


def copy_type_name(document, floor):
    return TYPE_PREFIX + clean_type_name(floor_type_name(document, floor))


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


def valid_for_direct_shape(direct_shape, geometry_object):
    """Whether Revit accepts this geometry in a DirectShape. If the check
    itself is unavailable, accept it and let SetShape be the judge."""
    try:
        return bool(direct_shape.IsValidGeometry(geometry_object))
    except Exception:
        return True


# ==============================================================================
# COPIES MADE EARLIER
# ==============================================================================
def find_existing_copies(document):
    """{floor id (int): [copy ElementId, ...]} for the Generic Models this tool
    made before, found through the application id stored on each."""
    copies = {}
    try:
        for direct_shape in DB.FilteredElementCollector(document).OfClass(DB.DirectShape):
            try:
                if direct_shape.ApplicationId != APP_ID:
                    continue
                key = int(direct_shape.ApplicationDataId)
            except Exception:
                continue
            copies.setdefault(key, []).append(direct_shape.Id)
    except Exception:
        pass
    return copies


# ==============================================================================
# CREATION
# ==============================================================================
def existing_copy_types(document, category_id):
    """{type name: ElementId} of the Generic Model DirectShape types that
    already exist."""
    found = {}
    try:
        for shape_type in DB.FilteredElementCollector(document).OfClass(DB.DirectShapeType):
            try:
                category = shape_type.Category
                if category is None or eid_int(category.Id) != eid_int(category_id):
                    continue
                found[shape_type.Name] = shape_type.Id
            except Exception:
                continue
    except Exception:
        pass
    return found


def get_or_create_type(document, name, category_id, known):
    """The Generic Model type for this name, created when missing. None if
    Revit refuses - the copy is then made without a type of its own."""
    if name in known:
        return known[name]
    try:
        shape_type = DB.DirectShapeType.Create(document, name, category_id)
        known[name] = shape_type.Id
        return shape_type.Id
    except Exception:
        known[name] = None
        return None


def write_notes(direct_shape, document, floor):
    """Comments says where the copy came from; Mark is carried over."""
    try:
        comments = direct_shape.get_Parameter(
            DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
        if comments is not None and not comments.IsReadOnly:
            comments.Set("Copy of floor {0}".format(floor_label(document, floor)))
    except Exception:
        pass
    try:
        source = floor.get_Parameter(DB.BuiltInParameter.ALL_MODEL_MARK)
        target = direct_shape.get_Parameter(DB.BuiltInParameter.ALL_MODEL_MARK)
        if (source is not None and target is not None and source.HasValue
                and source.AsString() and not target.IsReadOnly):
            target.Set(source.AsString())
    except Exception:
        pass


def copy_floor_to_generic(document, floor, category_id, type_id):
    """Create the Generic Model for one floor and return its ElementId. Raises
    CopyError (or a Revit exception) when it cannot - the caller rolls the
    attempt back."""
    solids = collect_solids(floor.get_Geometry(geometry_options()))
    if not solids:
        raise CopyError("the floor has no solid geometry to copy")

    direct_shape = DB.DirectShape.CreateElement(document, category_id)
    direct_shape.ApplicationId = APP_ID
    direct_shape.ApplicationDataId = str(eid_int(floor.Id))
    if type_id is not None:
        try:
            direct_shape.SetTypeId(type_id)
        except Exception:
            pass

    shape = List[DB.GeometryObject]()
    for solid in solids:
        clone = DB.SolidUtils.Clone(solid)
        if valid_for_direct_shape(direct_shape, clone):
            shape.Add(clone)
    if shape.Count == 0:
        raise CopyError("Revit cannot use this floor's geometry in a Generic Model")

    direct_shape.SetShape(shape)
    write_notes(direct_shape, document, floor)
    return direct_shape.Id


def create_copies(document, floor_ids):
    """Copy each floor to a Generic Model in ONE transaction (one Undo). Each
    floor is attempted on its own sub-transaction, so one that fails leaves
    nothing behind and the others still go. Returns (created, failures):
    created is [(floor id int, new ElementId)], failures [text]. Floors are
    looked up again by id for each copy rather than held across the
    modifications."""
    created = []
    failures = []
    category_id = DB.ElementId(DB.BuiltInCategory.OST_GenericModel)

    transaction = Transaction(document, "DQT - Floor to Generic Model")
    transaction.Start()
    try:
        known_types = existing_copy_types(document, category_id)

        # Types first and outside the per-floor sub-transactions: one that
        # was rolled back with a failed floor would leave the next floor of
        # the same type pointing at a type that no longer exists.
        type_for_name = {}
        for floor_id in floor_ids:
            floor = document.GetElement(floor_id)
            if floor is None:
                continue
            name = copy_type_name(document, floor)
            if name not in type_for_name:
                type_for_name[name] = get_or_create_type(
                    document, name, category_id, known_types)

        for floor_id in floor_ids:
            floor = document.GetElement(floor_id)
            if floor is None:
                failures.append("id {0}: the floor no longer exists".format(
                    eid_int(floor_id)))
                continue
            label = floor_label(document, floor)
            type_id = type_for_name.get(copy_type_name(document, floor))

            attempt = SubTransaction(document)
            attempt.Start()
            try:
                new_id = copy_floor_to_generic(document, floor, category_id, type_id)
                attempt.Commit()
                created.append((eid_int(floor_id), new_id))
            except Exception as error:
                attempt.RollBack()
                failures.append("{0}: {1}".format(label, error))

        if created:
            transaction.Commit()
        else:
            transaction.RollBack()
    except Exception:
        if transaction.HasStarted() and not transaction.HasEnded():
            transaction.RollBack()
        raise
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
        content = ("Each sits exactly on top of its floor and is selected now "
                   "- move it, or hide the original floor to see it.")
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

    try:
        created, failures = create_copies(doc, floor_ids)
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
