# -*- coding: utf-8 -*-
"""Revise Base
Change the base level / reference level of many elements at once and keep their
position by compensating the offset.
Supports: Walls, Floors, Columns, Structural Columns, Beams, Structural Framing,
Doors, Windows

Copyright (c) 2025 by Dang Quoc Truong (DQT)
"""

__title__ = "Revise\nBase"
__author__ = "DQT"
__doc__ = """Change the base/reference level of selected elements and keep their
absolute position by adjusting the offset automatically.

Supports: Walls, Floors, Columns, Structural Columns, Beams, Structural Framing,
Doors, Windows

For Doors/Windows, "Level" (Schedule Level) and "Sill Height" stand in for
Base Constraint/Offset - a hosted door or window whose family/type has no
Sill Height parameter is reported as failed rather than silently skipped.

Usage:
1. Select elements to revise
2. Run this tool
3. Select the new base/reference level
4. The tool updates the level and compensates the offset

Copyright (c) 2025 by Dang Quoc Truong (DQT)"""

from Autodesk.Revit.DB import *
from Autodesk.Revit.UI import *
from Autodesk.Revit.UI.Selection import ObjectType
from pyrevit import revit, forms, script
import sys

# Get current document
doc = __revit__.ActiveUIDocument.Document
uidoc = __revit__.ActiveUIDocument

def get_all_levels():
    """Get all levels in the project sorted by elevation"""
    collector = FilteredElementCollector(doc).OfClass(Level)
    levels = list(collector)
    # Sort by elevation
    levels.sort(key=lambda x: x.Elevation)
    return levels

def get_level_elevation(level):
    """Get level elevation in project units"""
    return level.Elevation

def _cat_bic(elem):
    """Return the element's BuiltInCategory (version-safe). Avoids
    category.Id.IntegerValue, which was removed in Revit 2026."""
    try:
        return elem.Category.BuiltInCategory
    except:
        return None

def is_column(elem):
    """Check if element is a column (Architectural or Structural)"""
    if not isinstance(elem, FamilyInstance):
        return False
    return _cat_bic(elem) in (BuiltInCategory.OST_StructuralColumns,
                              BuiltInCategory.OST_Columns)

def is_beam(elem):
    """Check if element is a beam or structural framing"""
    if not isinstance(elem, FamilyInstance):
        return False
    return _cat_bic(elem) == BuiltInCategory.OST_StructuralFraming

def is_door_or_window(elem):
    """Check if element is a Door or Window family instance"""
    if not isinstance(elem, FamilyInstance):
        return False
    return _cat_bic(elem) in (BuiltInCategory.OST_Doors, BuiltInCategory.OST_Windows)


# =============================================================================
# Door / Window (hosted openings)
#
# A hosted opening has no Base Constraint of its own. Which parameter carries
# its level depends on the family and the Revit version, and on a wall-hosted
# instance several of them are read-only because the opening follows its host.
# So: try every candidate, prefer one that is actually writable, and verify
# after regeneration that the value stuck instead of assuming it did.
# =============================================================================

_OPENING_LEVEL_BIP_NAMES = (
    "FAMILY_LEVEL_PARAM",
    "INSTANCE_SCHEDULE_ONLY_LEVEL_PARAM",
    "SCHEDULE_LEVEL_PARAM",
    "INSTANCE_REFERENCE_LEVEL_PARAM",
    "FAMILY_BASE_LEVEL_PARAM",
)

_OPENING_LEVEL_NAMES = ("Level", "Schedule Level", "Reference Level")


def _bip(name):
    """BuiltInParameter member by name, or None if this build doesn't have it."""
    try:
        return getattr(BuiltInParameter, name, None)
    except:
        return None


def _get_param(elem, bip_name):
    bip = _bip(bip_name)
    if bip is None:
        return None
    try:
        return elem.get_Parameter(bip)
    except:
        return None


def opening_level_params(elem):
    """Every level-ish parameter found on this opening, in preference order.

    Returned as a list of (label, param) so the caller can both pick one and
    report what was available when none of them turn out to be writable."""
    found = []
    seen = set()
    for name in _OPENING_LEVEL_BIP_NAMES:
        param = _get_param(elem, name)
        if param is not None:
            found.append((name, param))
            try:
                seen.add(param.Definition.Name)
            except:
                pass
    for name in _OPENING_LEVEL_NAMES:
        if name in seen:
            continue
        try:
            param = elem.LookupParameter(name)
        except:
            param = None
        if param is not None:
            found.append(('LookupParameter("{}")'.format(name), param))
    return found


def pick_writable_level_param(elem):
    """(param, label) of the first writable level parameter, else the first
    readable one, else (None, None)."""
    candidates = opening_level_params(elem)
    for label, param in candidates:
        try:
            if not param.IsReadOnly:
                return param, label
        except:
            continue
    if candidates:
        return candidates[0][1], candidates[0][0]
    return None, None


def describe_opening_level_params(elem):
    """Human-readable list of the level parameters this opening exposes and
    whether each can be written - printed when nothing could be changed, so
    the reason is visible instead of the tool just doing nothing."""
    lines = []
    for label, param in opening_level_params(elem):
        try:
            read_only = param.IsReadOnly
        except:
            read_only = "unknown"
        try:
            current = param.AsElementId()
            level = doc.GetElement(current) if current else None
            value = level.Name if level is not None else "(none)"
        except:
            value = "(unreadable)"
        lines.append("      {} -> {} (read-only: {})".format(label, value, read_only))
    if not lines:
        lines.append("      (no level parameter found on this element)")
    return "\n".join(lines)


def get_opening_sill_param(elem):
    """Sill Height - the offset that positions the opening inside its host.
    Many door families simply don't have one; that means offset 0, not an
    error, so it must not stop the level from being changed."""
    return _get_param(elem, "INSTANCE_SILL_HEIGHT_PARAM")


def get_opening_level(elem, level_param=None):
    """Current level of a Door/Window.

    Falls back to FamilyInstance.LevelId and then to the host's level, so a
    family whose level parameter is missing or empty can still be read."""
    if level_param is not None:
        try:
            level_id = level_param.AsElementId()
            if level_id is not None and level_id != ElementId.InvalidElementId:
                level = doc.GetElement(level_id)
                if isinstance(level, Level):
                    return level
        except:
            pass
    try:
        level = doc.GetElement(elem.LevelId)
        if isinstance(level, Level):
            return level
    except:
        pass
    try:
        host = elem.Host
        if host is not None:
            level = doc.GetElement(host.LevelId)
            if isinstance(level, Level):
                return level
    except:
        pass
    return None


def get_opening_base_info(elem):
    """(level, sill_offset, actual_elevation) for a Door/Window."""
    level_param, _label = pick_writable_level_param(elem)
    level = get_opening_level(elem, level_param)
    if level is None:
        return None, None, None

    sill_param = get_opening_sill_param(elem)
    sill = 0.0
    if sill_param is not None:
        try:
            sill = sill_param.AsDouble()
        except:
            sill = 0.0
    return level, sill, get_level_elevation(level) + sill


def get_opening_z(elem):
    """Absolute Z of the opening in the model.

    Used to find out whether changing the level actually moved the element,
    rather than assuming it did. Some level parameters on an opening are
    schedule bookkeeping only and move nothing - compensating the Sill
    Height in that case would displace a window that was never going to
    move. Measuring is the only way to tell the two apart."""
    try:
        location = elem.Location
        if location is not None and hasattr(location, "Point"):
            return location.Point.Z
    except:
        pass
    try:
        box = elem.get_BoundingBox(None)
        if box is not None:
            return box.Min.Z
    except:
        pass
    return None


def adjust_opening_base(elem, new_level):
    """Point a Door/Window at a new level.

    Only the level is written here. Whether the opening physically moved -
    and therefore whether Sill Height needs compensating to hold its
    position - is measured after the document regenerates, in
    restore_opening_position().

    Returns (applied, reason); `applied` means the Set call went through,
    not that Revit kept it (opening_level_matches confirms that later)."""
    level_param, label = pick_writable_level_param(elem)
    if level_param is None:
        return False, "no level parameter on this family"

    if get_opening_level(elem, level_param) is None:
        return False, "current level could not be read"

    try:
        if level_param.IsReadOnly:
            return False, ("Level is read-only ({}) - a wall-hosted opening "
                           "follows its host wall".format(label))
    except:
        pass

    try:
        level_param.Set(new_level.Id)
    except Exception as set_error:
        return False, "Level could not be set: {}".format(str(set_error))

    return True, "set via {}".format(label)


def restore_opening_position(elem, z_before, tolerance=1e-6):
    """Put a Door/Window back to the elevation it had before its level
    changed, by shifting Sill Height by however far it actually moved.

    A no-op when the element did not move (a schedule-only level parameter)
    or when Sill Height cannot be written."""
    if z_before is None:
        return False
    z_after = get_opening_z(elem)
    if z_after is None:
        return False

    drift = z_before - z_after
    if abs(drift) <= tolerance:
        return False        # nothing moved - nothing to compensate

    sill_param = get_opening_sill_param(elem)
    if sill_param is None:
        return False
    try:
        if sill_param.IsReadOnly:
            return False
        sill_param.Set(sill_param.AsDouble() + drift)
        return True
    except:
        return False


def opening_level_matches(elem, new_level):
    """True when the opening really is on `new_level` now. Called after the
    document regenerates, because Revit re-derives host-driven parameters
    then - which is exactly when a silently-rejected change shows up."""
    level_param, _label = pick_writable_level_param(elem)
    level = get_opening_level(elem, level_param)
    if level is None:
        return False
    try:
        return level.Id == new_level.Id
    except:
        return False

def get_element_type_name(elem):
    """Get element type name safely"""
    try:
        elem_type = doc.GetElement(elem.GetTypeId())
        if elem_type:
            return Element.Name.GetValue(elem_type)
    except:
        pass
    return "Unknown"

def get_element_category_name(elem):
    """Get element category name"""
    try:
        if elem.Category:
            return elem.Category.Name
    except:
        pass
    return "Unknown"

def get_element_base_info(elem):
    """Get element's current base constraint and offset information
    Works for Wall, Floor, Column, Beam (Structural Framing), Door, Window"""
    try:
        base_constraint_param = None
        base_offset_param = None
        
        # Wall
        if isinstance(elem, Wall):
            base_constraint_param = elem.get_Parameter(BuiltInParameter.WALL_BASE_CONSTRAINT)
            base_offset_param = elem.get_Parameter(BuiltInParameter.WALL_BASE_OFFSET)
        
        # Floor
        elif isinstance(elem, Floor):
            base_constraint_param = elem.get_Parameter(BuiltInParameter.LEVEL_PARAM)
            base_offset_param = elem.get_Parameter(BuiltInParameter.FLOOR_HEIGHTABOVELEVEL_PARAM)
        
        # Column (Architectural or Structural)
        elif is_column(elem):
            base_constraint_param = elem.get_Parameter(BuiltInParameter.FAMILY_BASE_LEVEL_PARAM)
            base_offset_param = elem.get_Parameter(BuiltInParameter.FAMILY_BASE_LEVEL_OFFSET_PARAM)
        
        # Beam (Structural Framing)
        elif is_beam(elem):
            base_constraint_param = elem.get_Parameter(BuiltInParameter.INSTANCE_REFERENCE_LEVEL_PARAM)
            base_offset_param = elem.get_Parameter(BuiltInParameter.STRUCTURAL_BEAM_END0_ELEVATION)

        # Door / Window - handled separately: which parameter carries the
        # level varies by family, and a missing Sill Height means offset 0
        # rather than "unreadable", so the generic both-params-required
        # check below would wrongly skip most doors.
        elif is_door_or_window(elem):
            return get_opening_base_info(elem)

        else:
            return None, None, None
        
        if not base_constraint_param or not base_offset_param:
            return None, None, None
        
        base_level_id = base_constraint_param.AsElementId()
        if base_level_id == ElementId.InvalidElementId:
            return None, None, None
            
        base_level = doc.GetElement(base_level_id)
        base_offset = base_offset_param.AsDouble() if base_offset_param else 0.0
        
        # Calculate actual elevation (absolute position)
        base_elevation = get_level_elevation(base_level)
        actual_elevation = base_elevation + base_offset
        
        return base_level, base_offset, actual_elevation
    except Exception as e:
        print("Error getting element base info: {}".format(str(e)))
        return None, None, None

def adjust_element_base_constraint(elem, new_level):
    """Adjust element's base constraint and offset to maintain position.
    Works for Wall, Floor, Column, Beam (Structural Framing), Door, Window.

    Returns (ok, reason) - the reason is what the summary reports when an
    element could not be changed, so a silent no-op is never possible."""
    try:
        # Door / Window: hosted openings need their own handling (see the
        # section above) and report why they could not be changed.
        if is_door_or_window(elem):
            return adjust_opening_base(elem, new_level)

        # Get current base information
        current_level, current_offset, actual_elevation = get_element_base_info(elem)

        if current_level is None or actual_elevation is None:
            return False, "current level/offset could not be read"

        # Calculate new offset needed
        new_level_elevation = get_level_elevation(new_level)
        new_offset = actual_elevation - new_level_elevation

        # Beam (Structural Framing): only change the Reference Level. Keep the
        # Start/End Level Offset and z Offset Value unchanged, so the beam
        # "jumps" to follow the new level.
        if is_beam(elem):
            ref_param = elem.get_Parameter(BuiltInParameter.INSTANCE_REFERENCE_LEVEL_PARAM)
            if not ref_param or ref_param.IsReadOnly:
                return False, "beam Reference Level is not editable"
            ref_param.Set(new_level.Id)
            return True, "reference level set"

        # Get parameters based on element type
        base_constraint_param = None
        base_offset_param = None
        
        # Wall
        if isinstance(elem, Wall):
            base_constraint_param = elem.get_Parameter(BuiltInParameter.WALL_BASE_CONSTRAINT)
            base_offset_param = elem.get_Parameter(BuiltInParameter.WALL_BASE_OFFSET)
        
        # Floor
        elif isinstance(elem, Floor):
            base_constraint_param = elem.get_Parameter(BuiltInParameter.LEVEL_PARAM)
            base_offset_param = elem.get_Parameter(BuiltInParameter.FLOOR_HEIGHTABOVELEVEL_PARAM)
        
        # Column (Architectural or Structural)
        elif is_column(elem):
            base_constraint_param = elem.get_Parameter(BuiltInParameter.FAMILY_BASE_LEVEL_PARAM)
            base_offset_param = elem.get_Parameter(BuiltInParameter.FAMILY_BASE_LEVEL_OFFSET_PARAM)
        
        # Beam (Structural Framing)
        elif is_beam(elem):
            base_constraint_param = elem.get_Parameter(BuiltInParameter.INSTANCE_REFERENCE_LEVEL_PARAM)
            base_offset_param = elem.get_Parameter(BuiltInParameter.STRUCTURAL_BEAM_END0_ELEVATION)

        if not base_constraint_param or not base_offset_param:
            return False, "base constraint/offset parameters not available"

        # Update Base Constraint
        base_constraint_param.Set(new_level.Id)

        # Update Base Offset
        base_offset_param.Set(new_offset)

        return True, "base constraint and offset updated"
    except Exception as e:
        return False, str(e)

def main():
    """Main execution function"""
    
    try:
        # Get selected elements or prompt user to select
        selection = uidoc.Selection.GetElementIds()
        elements = []
        
        if selection.Count > 0:
            # Filter selected elements to get only walls, floors, columns, beams
            for elem_id in selection:
                elem = doc.GetElement(elem_id)
                if isinstance(elem, (Wall, Floor)) or is_column(elem) or is_beam(elem) or is_door_or_window(elem):
                    elements.append(elem)
        
        if not elements:
            # Prompt user to select elements
            try:
                from Autodesk.Revit.UI.Selection import ISelectionFilter
                
                class ElementSelectionFilter(ISelectionFilter):
                    def AllowElement(self, elem):
                        return isinstance(elem, (Wall, Floor)) or is_column(elem) or is_beam(elem) or is_door_or_window(elem)
                    
                    def AllowReference(self, reference, position):
                        return False
                
                refs = uidoc.Selection.PickObjects(
                    ObjectType.Element,
                    ElementSelectionFilter(),
                    "Select elements to adjust Base Constraint (Walls, Floors, Columns, Beams, Doors, Windows)"
                )
                
                elements = [doc.GetElement(ref.ElementId) for ref in refs]
                
            except Exception as e:
                forms.alert("No elements selected!", exitscript=True)
                return
        
        if not elements:
            forms.alert("No elements selected!", exitscript=True)
            return
        
        # Get all levels
        all_levels = get_all_levels()
        if not all_levels:
            forms.alert("No levels found in project!", exitscript=True)
            return
        
        # Create level selection options with display names
        class LevelOption:
            def __init__(self, level):
                self.level = level
                elevation_mm = level.Elevation * 304.8  # Convert to mm for display
                self.name = "{} (Elevation: {:.0f}mm)".format(level.Name, elevation_mm)
        
        level_options = [LevelOption(level) for level in all_levels]
        
        selected_option = forms.SelectFromList.show(
            level_options,
            title="Select New Base Constraint",
            button_name="Apply",
            name_attr='name',
            multiselect=False
        )
        
        if not selected_option:
            script.exit()
            return
        
        new_level = selected_option.level
        
        # Process elements in a transaction
        applied = []        # elements whose Set call went through
        failures = []       # (elem, reason)

        t = Transaction(doc, "Revise Base - Change Level & Offset")
        t.Start()

        try:
            # Openings are measured first so their position can be restored
            # only if changing the level actually moved them.
            z_before = {}
            for elem in elements:
                if is_door_or_window(elem):
                    z_before[elem.Id] = get_opening_z(elem)

            for elem in elements:
                try:
                    ok, reason = adjust_element_base_constraint(elem, new_level)
                    if ok:
                        applied.append(elem)
                    else:
                        failures.append((elem, reason))
                except Exception as elem_error:
                    failures.append((elem, str(elem_error)))

            # Revit re-derives host-driven parameters on regeneration, which
            # is where a silently-rejected level change shows up - and where
            # any real movement becomes measurable.
            doc.Regenerate()

            for elem in applied:
                if is_door_or_window(elem):
                    restore_opening_position(elem, z_before.get(elem.Id))

            doc.Regenerate()

            # Openings only count as done once the new level survives.
            for elem in list(applied):
                if is_door_or_window(elem) and not opening_level_matches(elem, new_level):
                    applied.remove(elem)
                    failures.append((elem, "Revit reset the Level back to the "
                                           "host wall's level"))

            t.Commit()

        except Exception as e:
            t.RollBack()
            forms.alert("Error occurred while adjusting elements!\n\n{}".format(str(e)))
            return

        report_results(applied, failures, new_level)

    except Exception as main_error:
        forms.alert("Error in main execution!\n\n{}".format(str(main_error)))


def report_results(applied, failures, new_level):
    """Tell the user what actually happened.

    The tool used to count successes and failures and then show nothing at
    all, so an element that could not be changed looked identical to one
    that was - which is exactly how a door/window silently not moving went
    unnoticed."""
    out = script.get_output()

    summary = "Revise Base -> {}\n\n{} element(s) changed".format(
        new_level.Name, len(applied))
    if failures:
        summary += "\n{} element(s) NOT changed".format(len(failures))

    # Group the reasons so a batch of identical problems reads as one line.
    if failures:
        by_reason = {}
        for elem, reason in failures:
            by_reason.setdefault(reason, []).append(elem)

        summary += "\n\nWhy:"
        for reason in sorted(by_reason.keys()):
            summary += "\n  - {} x {}".format(len(by_reason[reason]), reason)

        out.print_md("## Revise Base - elements not changed")
        for reason in sorted(by_reason.keys()):
            elems = by_reason[reason]
            out.print_md("**{}** ({} element(s))".format(reason, len(elems)))
            for elem in elems[:20]:
                out.print_md("- {} - {} (id {})".format(
                    get_element_category_name(elem),
                    get_element_type_name(elem), elem.Id))
            if len(elems) > 20:
                out.print_md("- ... and {} more".format(len(elems) - 20))

        # For openings, list what level parameters the family actually has -
        # without this the "read-only" reason is impossible to act on.
        openings = [e for e, _r in failures if is_door_or_window(e)]
        if openings:
            out.print_md("### Level parameters found on the first failing opening")
            out.print_md("```\n{}\n```".format(
                describe_opening_level_params(openings[0])))
            summary += ("\n\nA wall-hosted door/window follows its host wall's "
                        "level. To move them, change the level of the host "
                        "wall instead (select the walls and run this tool).")

    if failures:
        summary += "\n\nSee the pyRevit output window for the element list."
    forms.alert(summary, title="Revise Base")

if __name__ == "__main__":
    main()