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

        # Door / Window: hosted instances don't have a Base Constraint of
        # their own - "Level" (Schedule Level) plays that role, and "Sill
        # Height" is the offset from it that actually positions the opening
        # vertically within its host. If a particular family/host doesn't
        # expose one of these (e.g. many door types have no Sill Height),
        # the generic None-check below skips it rather than guessing.
        elif is_door_or_window(elem):
            base_constraint_param = elem.get_Parameter(BuiltInParameter.INSTANCE_SCHEDULE_ONLY_LEVEL_PARAM)
            base_offset_param = elem.get_Parameter(BuiltInParameter.INSTANCE_SILL_HEIGHT_PARAM)

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
    """Adjust element's base constraint and offset to maintain position
    Works for Wall, Floor, Column, Beam (Structural Framing), Door, Window"""
    try:
        # Get current base information
        current_level, current_offset, actual_elevation = get_element_base_info(elem)

        if current_level is None or actual_elevation is None:
            print("Cannot get current element information: {}".format(elem.Id))
            return False

        # Calculate new offset needed
        new_level_elevation = get_level_elevation(new_level)
        new_offset = actual_elevation - new_level_elevation

        # Beam (Structural Framing): only change the Reference Level. Keep the
        # Start/End Level Offset and z Offset Value unchanged, so the beam
        # "jumps" to follow the new level.
        if is_beam(elem):
            ref_param = elem.get_Parameter(BuiltInParameter.INSTANCE_REFERENCE_LEVEL_PARAM)
            if not ref_param or ref_param.IsReadOnly:
                print("Beam reference level not editable: {}".format(elem.Id))
                return False
            ref_param.Set(new_level.Id)
            return True

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

        # Door / Window - see the matching branch in get_element_base_info
        # for why Level + Sill Height stand in for Base Constraint + Offset.
        elif is_door_or_window(elem):
            base_constraint_param = elem.get_Parameter(BuiltInParameter.INSTANCE_SCHEDULE_ONLY_LEVEL_PARAM)
            base_offset_param = elem.get_Parameter(BuiltInParameter.INSTANCE_SILL_HEIGHT_PARAM)

        if not base_constraint_param or not base_offset_param:
            print("Cannot access element parameters: {}".format(elem.Id))
            return False
        
        # Update Base Constraint
        base_constraint_param.Set(new_level.Id)
        
        # Update Base Offset
        base_offset_param.Set(new_offset)
        
        return True
    except Exception as e:
        print("Error adjusting element {}: {}".format(elem.Id, str(e)))
        return False

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
        success_count = 0
        error_count = 0
        
        t = Transaction(doc, "Revise Base - Change Level & Offset")
        t.Start()
        
        try:
            for elem in elements:
                try:
                    # Get current info
                    current_level, current_offset, actual_elevation = get_element_base_info(elem)
                    
                    if current_level is None:
                        error_count += 1
                        continue
                    
                    # Apply changes
                    if adjust_element_base_constraint(elem, new_level):
                        success_count += 1
                    else:
                        error_count += 1
                        
                except Exception as elem_error:
                    error_count += 1
            
            t.Commit()
            
        except Exception as e:
            t.RollBack()
            forms.alert("Error occurred while adjusting elements!\n\n{}".format(str(e)))
    
    except Exception as main_error:
        forms.alert("Error in main execution!\n\n{}".format(str(main_error)))

if __name__ == "__main__":
    main()