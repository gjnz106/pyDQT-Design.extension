# -*- coding: utf-8 -*-
"""
Wall Dimensioning v1.0 - DQT
Simple and safe auto-dimensioning for walls.
Select walls -> creates dimension chain between wall faces.

Copyright (c) 2026 Dang Quoc Truong (DQT)
All rights reserved.
"""
try: __import__("dqt_usage").log_use(__file__)  # pyDQT usage log
except Exception: pass

__title__ = "Wall\nDimension"
__author__ = "Dang Quoc Truong (DQT)"

import clr
clr.AddReference('RevitAPI')
clr.AddReference('RevitAPIUI')

import Autodesk.Revit.DB as DB
from Autodesk.Revit.DB import *
from Autodesk.Revit.UI import TaskDialog
from Autodesk.Revit.UI.Selection import ObjectType, ISelectionFilter
from pyrevit import revit, forms, script

doc = __revit__.ActiveUIDocument.Document
uidoc = __revit__.ActiveUIDocument
view = doc.ActiveView
output = script.get_output()


def _eid(eid):
    """Get integer value from ElementId - compatible with Revit 2024-2026"""
    try:
        return eid.IntegerValue
    except:
        try:
            return eid.Value
        except:
            return str(eid)



class WallFilter(ISelectionFilter):
    def AllowElement(self, elem):
        return isinstance(elem, Wall)
    def AllowReference(self, ref, pt):
        return False


class LinkedWallFilter(ISelectionFilter):
    """Filter for picking walls in linked files"""
    def AllowElement(self, elem):
        # Allow RevitLinkInstance (the link container)
        return isinstance(elem, RevitLinkInstance)
    def AllowReference(self, ref, pt):
        # Allow references to walls inside links
        try:
            # ref.LinkedElementId gives the element in the linked doc
            if ref.LinkedElementId and ref.LinkedElementId != ElementId.InvalidElementId:
                return True
        except:
            pass
        return True


def get_wall_direction(wall):
    """Get wall direction - None if not a straight wall"""
    try:
        loc = wall.Location
        if isinstance(loc, LocationCurve) and isinstance(loc.Curve, Line):
            return loc.Curve.Direction.Normalize()
    except:
        pass
    return None


def get_wall_midpoint(wall):
    """Get wall midpoint"""
    try:
        loc = wall.Location
        if isinstance(loc, LocationCurve):
            return loc.Curve.Evaluate(0.5, True)
    except:
        pass
    return None


def get_safe_face_refs(wall):
    """
    Get wall exterior/interior face references using HostObjectUtils.
    Returns list of References.
    """
    refs = []
    try:
        ext_faces = HostObjectUtils.GetSideFaces(wall, ShellLayerType.Exterior)
        int_faces = HostObjectUtils.GetSideFaces(wall, ShellLayerType.Interior)
        
        for face_ref in ext_faces:
            if face_ref:
                refs.append(face_ref)
        
        for face_ref in int_faces:
            if face_ref:
                refs.append(face_ref)
                
    except Exception as e:
        output.print_md("**GetSideFaces failed for wall {}: {}**".format(
            _eid(wall.Id), str(e)))
    
    return refs




def get_all_grids():
    """Get all grids in project"""
    grids = []
    try:
        collector = FilteredElementCollector(doc).OfClass(Grid).WhereElementIsNotElementType()
        for g in collector:
            try:
                if g.Curve and isinstance(g.Curve, Line):
                    grids.append(g)
            except:
                continue
    except:
        pass
    return grids


def get_grid_ref(grid):
    """Get grid curve reference safely - try multiple approaches"""
    # Approach 1: Direct Reference constructor (simplest, works in many cases)
    try:
        ref = Reference(grid)
        if ref:
            return ref
    except:
        pass
    
    # Approach 2: Geometry iteration with view
    try:
        opts = Options()
        opts.ComputeReferences = True
        opts.IncludeNonVisibleObjects = True
        opts.View = view
        geom = grid.get_Geometry(opts)
        if geom:
            for obj in geom:
                try:
                    if hasattr(obj, 'Reference') and obj.Reference:
                        return obj.Reference
                except:
                    continue
    except:
        pass
    
    # Approach 3: Without view filter
    try:
        opts2 = Options()
        opts2.ComputeReferences = True
        opts2.IncludeNonVisibleObjects = True
        geom2 = grid.get_Geometry(opts2)
        if geom2:
            for obj in geom2:
                try:
                    if hasattr(obj, 'Reference') and obj.Reference:
                        return obj.Reference
                except:
                    continue
    except:
        pass
    
    return None


def find_nearest_parallel_grid(wall, grids):
    """Find the nearest grid that is parallel to a wall.
    Returns (grid, distance) or (None, None)"""
    wd = get_wall_direction(wall)
    wm = get_wall_midpoint(wall)
    if not wd or not wm:
        return None, None
    
    best_grid = None
    best_dist = float('inf')
    
    for grid in grids:
        try:
            gd = grid.Curve.Direction.Normalize()
            # Grid must be parallel to wall (dot product ~ 1)
            if abs(gd.DotProduct(wd)) < 0.7:
                continue
            
            # Distance from wall midpoint to grid line
            gs = grid.Curve.GetEndPoint(0)
            to_wall = DB.XYZ(wm.X - gs.X, wm.Y - gs.Y, 0)
            gp = DB.XYZ(-gd.Y, gd.X, 0).Normalize()  # grid perpendicular
            dist = abs(to_wall.DotProduct(gp))
            
            if dist < best_dist:
                best_dist = dist
                best_grid = grid
        except:
            continue
    
    return best_grid, best_dist


def get_wall_center_ref(wall):
    """Get wall center line reference for dimensioning"""
    try:
        opts = Options()
        opts.ComputeReferences = True
        opts.IncludeNonVisibleObjects = True
        opts.View = view
        geom = wall.get_Geometry(opts)
        if geom:
            for obj in geom:
                try:
                    if isinstance(obj, Line) and obj.Reference:
                        return obj.Reference
                except:
                    continue
    except:
        pass
    return None



# ============================================================================
# LINKED FILE SUPPORT
# ============================================================================

def get_link_instances():
    """Get all RevitLinkInstance visible in current view"""
    links = []
    try:
        collector = FilteredElementCollector(doc, view.Id).OfClass(RevitLinkInstance)
        for link in collector:
            try:
                link_doc = link.GetLinkDocument()
                if link_doc:
                    links.append(link)
            except:
                continue
    except:
        pass
    return links


def get_linked_walls(link_instance, filter_by_view=True):
    """Get walls from a linked document, filtered to current view's level"""
    walls = []
    try:
        link_doc = link_instance.GetLinkDocument()
        if not link_doc:
            return walls
        
        transform = link_instance.GetTotalTransform()
        
        # Get view's level elevation for filtering
        view_level_elev = None
        view_crop_bb = None
        if filter_by_view and isinstance(view, ViewPlan):
            try:
                gen_level = view.GenLevel
                if gen_level:
                    view_level_elev = gen_level.Elevation
            except:
                pass
            # Get view crop box for XY filtering
            try:
                if view.CropBoxActive:
                    view_crop_bb = view.CropBox
            except:
                pass
        
        collector = FilteredElementCollector(link_doc).OfClass(Wall).WhereElementIsNotElementType()
        for w in collector:
            try:
                loc = w.Location
                if not loc or not isinstance(loc, LocationCurve) or not isinstance(loc.Curve, Line):
                    continue
                
                # Filter: wall must be at similar elevation as view level
                if view_level_elev is not None:
                    try:
                        base_offset_param = w.get_Parameter(BuiltInParameter.WALL_BASE_OFFSET)
                        base_level_param = w.get_Parameter(BuiltInParameter.WALL_BASE_CONSTRAINT)
                        
                        if base_level_param:
                            base_level_id = base_level_param.AsElementId()
                            base_level = link_doc.GetElement(base_level_id)
                            if base_level:
                                wall_base_elev = base_level.Elevation
                                if base_offset_param:
                                    wall_base_elev += base_offset_param.AsDouble()
                                
                                # Transform to host elevation
                                wall_pt = transform.OfPoint(DB.XYZ(0, 0, wall_base_elev))
                                host_elev = wall_pt.Z
                                
                                # Wall must be within reasonable range of view level
                                # (within one story height ~5m)
                                if abs(host_elev - view_level_elev) > 16.4:  # ~5000mm
                                    continue
                    except:
                        pass
                
                walls.append(w)
            except:
                continue
    except:
        pass
    return walls


def get_linked_wall_face_refs(wall, link_instance):
    """
    Get face references for a wall in a linked file.
    References must be converted to linked references for dimensioning.
    """
    refs = []
    try:
        link_doc = link_instance.GetLinkDocument()
        if not link_doc:
            return refs
        
        # Get face references from the linked wall
        ext_faces = HostObjectUtils.GetSideFaces(wall, ShellLayerType.Exterior)
        int_faces = HostObjectUtils.GetSideFaces(wall, ShellLayerType.Interior)
        
        for face_ref in ext_faces:
            if face_ref:
                # Convert to linked reference
                linked_ref = face_ref.CreateLinkReference(link_instance)
                if linked_ref:
                    refs.append(linked_ref)
        
        for face_ref in int_faces:
            if face_ref:
                linked_ref = face_ref.CreateLinkReference(link_instance)
                if linked_ref:
                    refs.append(linked_ref)
    except Exception as e:
        output.print_md("  - Linked face ref error: {}".format(str(e)))
    
    return refs


def get_linked_wall_direction(wall, link_instance):
    """Get wall direction in host coordinate system"""
    try:
        transform = link_instance.GetTotalTransform()
        loc = wall.Location
        if isinstance(loc, LocationCurve) and isinstance(loc.Curve, Line):
            # Transform direction from link to host coordinates
            link_dir = loc.Curve.Direction.Normalize()
            host_dir = transform.OfVector(link_dir).Normalize()
            return host_dir
    except:
        pass
    return None


def get_linked_wall_midpoint(wall, link_instance):
    """Get wall midpoint in host coordinate system"""
    try:
        transform = link_instance.GetTotalTransform()
        loc = wall.Location
        if isinstance(loc, LocationCurve):
            link_mid = loc.Curve.Evaluate(0.5, True)
            host_mid = transform.OfPoint(link_mid)
            return host_mid
    except:
        pass
    return None


def find_nearest_grid_for_linked_wall(wall, link_instance, grids):
    """Find nearest parallel grid for a linked wall"""
    wd = get_linked_wall_direction(wall, link_instance)
    wm = get_linked_wall_midpoint(wall, link_instance)
    if not wd or not wm:
        return None, None
    
    best_grid = None
    best_dist = float('inf')
    
    for grid in grids:
        try:
            gd = grid.Curve.Direction.Normalize()
            if abs(gd.DotProduct(wd)) < 0.7:
                continue
            gs = grid.Curve.GetEndPoint(0)
            to_wall = DB.XYZ(wm.X - gs.X, wm.Y - gs.Y, 0)
            gp = DB.XYZ(-gd.Y, gd.X, 0).Normalize()
            dist = abs(to_wall.DotProduct(gp))
            if dist < best_dist:
                best_dist = dist
                best_grid = grid
        except:
            continue
    
    return best_grid, best_dist


def main():
    # Validate view
    if not isinstance(view, (ViewPlan, ViewSection)):
        TaskDialog.Show("Wall Dimensioning", 
                       "Please open a Floor Plan or Section view.")
        return
    
    # Ask user what to dimension FIRST (to know if linked mode)
    options = [
        "Wall Faces (between parallel walls)",
        "Wall Thickness (each wall individually)",
        "Wall Thickness + Nearest Grid",
        "Wall Center to Nearest Grid",
        "Linked Wall Thickness + Nearest Grid"
    ]
    
    selected = forms.SelectFromList.show(
        options,
        title="Wall Dimensioning - Select Mode",
        multiselect=True,
        button_name="Create Dimensions"
    )
    
    if not selected:
        return
    
    do_faces = options[0] in selected
    do_thickness = options[1] in selected
    do_thick_grid = options[2] in selected
    do_center_grid = options[3] in selected
    do_linked_grid = options[4] in selected
    
    needs_host_walls = do_faces or do_thickness or do_thick_grid or do_center_grid
    
    # Get HOST walls (for non-linked modes)
    walls = []
    if needs_host_walls:
        sel_ids = uidoc.Selection.GetElementIds()
        
        if sel_ids.Count > 0:
            for eid in sel_ids:
                try:
                    elem = doc.GetElement(eid)
                    if isinstance(elem, Wall):
                        loc = elem.Location
                        if loc and isinstance(loc, LocationCurve) and isinstance(loc.Curve, Line):
                            walls.append(elem)
                except:
                    continue
        
        if not walls:
            try:
                picked = uidoc.Selection.PickObjects(
                    ObjectType.Element, WallFilter(),
                    "Select walls to dimension (Finish with Escape)")
                for p in picked:
                    try:
                        elem = doc.GetElement(p.ElementId)
                        if isinstance(elem, Wall):
                            loc = elem.Location
                            if loc and isinstance(loc, LocationCurve) and isinstance(loc.Curve, Line):
                                walls.append(elem)
                    except:
                        continue
            except:
                if not do_linked_grid:
                    return
        
        if not walls and not do_linked_grid:
            TaskDialog.Show("Wall Dimensioning", "No valid walls selected.")
            return
    
    # Ask offset
    offset_mm = 1000
    try:
        offset_input = forms.ask_for_string(
            default="1000",
            prompt="Offset distance in mm:",
            title="Dimension Offset"
        )
        if offset_input:
            offset_mm = float(offset_input)
    except:
        pass
    
    offset_ft = offset_mm / 304.8
    
    # Group walls by direction
    horizontal = []  # walls along X
    vertical = []    # walls along Y
    
    for wall in walls:
        d = get_wall_direction(wall)
        if not d:
            continue
        if abs(d.X) > 0.7:
            horizontal.append(wall)
        elif abs(d.Y) > 0.7:
            vertical.append(wall)
    
    output.print_md("## Wall Dimensioning")
    output.print_md("Selected: **{} walls** ({} horizontal, {} vertical)".format(
        len(walls), len(horizontal), len(vertical)))
    
    total = 0
    errors = 0
    
    t = Transaction(doc, "DQT - Wall Dimensioning")
    t.Start()
    
    try:
        # === WALL FACES (between parallel walls) ===
        if do_faces:
            # Horizontal walls - dimension perpendicular (along Y)
            if len(horizontal) >= 2:
                horizontal.sort(key=lambda w: get_wall_midpoint(w).Y if get_wall_midpoint(w) else 0)
                
                ref_array = ReferenceArray()
                ref_count = 0
                
                for wall in horizontal:
                    face_refs = get_safe_face_refs(wall)
                    for r in face_refs:
                        try:
                            ref_array.Append(r)
                            ref_count += 1
                        except:
                            pass
                
                if ref_count >= 2:
                    first = get_wall_midpoint(horizontal[0])
                    last = get_wall_midpoint(horizontal[-1])
                    if first and last:
                        try:
                            miny = min(first.Y, last.Y) - 3.0
                            maxy = max(first.Y, last.Y) + 3.0
                            x = first.X - offset_ft
                            
                            dim_line = Line.CreateBound(
                                DB.XYZ(x, miny, 0),
                                DB.XYZ(x, maxy, 0)
                            )
                            dim = doc.Create.NewDimension(view, dim_line, ref_array)
                            if dim:
                                total += 1
                                output.print_md("- Created face dimension for {} horizontal walls".format(len(horizontal)))
                        except Exception as e:
                            errors += 1
                            output.print_md("- **Error** horizontal face dim: {}".format(str(e)))
                else:
                    output.print_md("- Skipped horizontal: only {} valid refs".format(ref_count))
            
            # Vertical walls - dimension perpendicular (along X)
            if len(vertical) >= 2:
                vertical.sort(key=lambda w: get_wall_midpoint(w).X if get_wall_midpoint(w) else 0)
                
                ref_array = ReferenceArray()
                ref_count = 0
                
                for wall in vertical:
                    face_refs = get_safe_face_refs(wall)
                    for r in face_refs:
                        try:
                            ref_array.Append(r)
                            ref_count += 1
                        except:
                            pass
                
                if ref_count >= 2:
                    first = get_wall_midpoint(vertical[0])
                    last = get_wall_midpoint(vertical[-1])
                    if first and last:
                        try:
                            minx = min(first.X, last.X) - 3.0
                            maxx = max(first.X, last.X) + 3.0
                            y = first.Y - offset_ft
                            
                            dim_line = Line.CreateBound(
                                DB.XYZ(minx, y, 0),
                                DB.XYZ(maxx, y, 0)
                            )
                            dim = doc.Create.NewDimension(view, dim_line, ref_array)
                            if dim:
                                total += 1
                                output.print_md("- Created face dimension for {} vertical walls".format(len(vertical)))
                        except Exception as e:
                            errors += 1
                            output.print_md("- **Error** vertical face dim: {}".format(str(e)))
                else:
                    output.print_md("- Skipped vertical: only {} valid refs".format(ref_count))
        
        # === WALL THICKNESS (individual) ===
        if do_thickness:
            for wall in walls:
                try:
                    face_refs = get_safe_face_refs(wall)
                    if len(face_refs) < 2:
                        continue
                    
                    ref_array = ReferenceArray()
                    ref_array.Append(face_refs[0])
                    ref_array.Append(face_refs[1])
                    
                    wd = get_wall_direction(wall)
                    if not wd:
                        continue
                    
                    loc = wall.Location
                    curve = loc.Curve
                    ep = curve.GetEndPoint(1)
                    wn = DB.XYZ(-wd.Y, wd.X, 0)
                    
                    pt = ep + wd * offset_ft
                    p1 = pt + wn * (-3.0)
                    p2 = pt + wn * 3.0
                    
                    dim_line = Line.CreateBound(p1, p2)
                    dim = doc.Create.NewDimension(view, dim_line, ref_array)
                    if dim:
                        total += 1
                except Exception as e:
                    errors += 1
                    output.print_md("- **Error** thickness dim wall {}: {}".format(
                        _eid(wall.Id), str(e)))
            
            if do_thickness:
                output.print_md("- Wall thickness: {} created".format(
                    total if not do_faces else total - (1 if len(horizontal)>=2 else 0) - (1 if len(vertical)>=2 else 0)))
        
        # === WALL THICKNESS + NEAREST GRID ===
        if do_thick_grid:
            grids = get_all_grids()
            if not grids:
                output.print_md("- **No grids found** in project")
            else:
                output.print_md("- Found {} grids in project".format(len(grids)))
                tg_count = 0
                
                for wall in walls:
                    try:
                        wd = get_wall_direction(wall)
                        wm = get_wall_midpoint(wall)
                        if not wd or not wm:
                            continue
                        
                        # Get wall face refs (ext + int)
                        face_refs = get_safe_face_refs(wall)
                        if len(face_refs) < 2:
                            continue
                        
                        # Find nearest parallel grid
                        nearest, dist = find_nearest_parallel_grid(wall, grids)
                        if not nearest:
                            continue
                        
                        grid_ref = get_grid_ref(nearest)
                        if not grid_ref:
                            output.print_md("  - No ref for grid {}".format(nearest.Name))
                            continue
                        
                        # Build: grid + ext face + int face
                        # This gives: [grid --- ext face | int face] showing thickness + distance to grid
                        ref_array = ReferenceArray()
                        ref_array.Append(grid_ref)
                        ref_array.Append(face_refs[0])  # exterior
                        ref_array.Append(face_refs[1])  # interior
                        
                        # Dim line MUST be perpendicular to wall (along wall normal)
                        # Pick a point along wall and draw line perpendicular through it
                        wn = DB.XYZ(-wd.Y, wd.X, 0).Normalize()
                        
                        # Use wall midpoint, offset along wall direction for placement
                        loc = wall.Location
                        curve = loc.Curve
                        mid_pt = curve.Evaluate(0.5, True)
                        
                        # Dim line along wall normal, long enough to cover grid + wall
                        p1 = DB.XYZ(mid_pt.X, mid_pt.Y, 0) + wn * (dist + 5.0)
                        p2 = DB.XYZ(mid_pt.X, mid_pt.Y, 0) - wn * (dist + 5.0)
                        
                        if p1.DistanceTo(p2) < 0.01:
                            continue
                        
                        dim_line = Line.CreateBound(p1, p2)
                        dim = doc.Create.NewDimension(view, dim_line, ref_array)
                        if dim:
                            tg_count += 1
                            total += 1
                    except Exception as e:
                        errors += 1
                        output.print_md("  - **Error** thick+grid wall {}: {}".format(
                            _eid(wall.Id), str(e)))
                
                output.print_md("- Thickness + Grid: {} created".format(tg_count))
        
        # === WALL CENTER TO NEAREST GRID ===
        if do_center_grid:
            if not do_thick_grid:
                grids = get_all_grids()
            if not grids:
                output.print_md("- **No grids found** in project")
            else:
                if not do_thick_grid:
                    output.print_md("- Found {} grids in project".format(len(grids)))
                cg_count = 0
                
                for wall in walls:
                    try:
                        wd = get_wall_direction(wall)
                        wm = get_wall_midpoint(wall)
                        if not wd or not wm:
                            continue
                        
                        # Get wall center line reference
                        center_ref = get_wall_center_ref(wall)
                        if not center_ref:
                            output.print_md("  - No center ref for wall {}".format(_eid(wall.Id)))
                            continue
                        
                        # Find nearest parallel grid
                        nearest, dist = find_nearest_parallel_grid(wall, grids)
                        if not nearest:
                            continue
                        
                        grid_ref = get_grid_ref(nearest)
                        if not grid_ref:
                            output.print_md("  - No ref for grid {}".format(nearest.Name))
                            continue
                        
                        # Build: grid + wall center
                        ref_array = ReferenceArray()
                        ref_array.Append(grid_ref)
                        ref_array.Append(center_ref)
                        
                        # Dim line MUST be perpendicular to wall (along wall normal)
                        wn = DB.XYZ(-wd.Y, wd.X, 0).Normalize()
                        
                        # Line through wall midpoint, along wall normal
                        p1 = DB.XYZ(wm.X, wm.Y, 0) + wn * (dist + 5.0)
                        p2 = DB.XYZ(wm.X, wm.Y, 0) - wn * (dist + 5.0)
                        
                        if p1.DistanceTo(p2) < 0.01:
                            continue
                        
                        dim_line = Line.CreateBound(p1, p2)
                        dim = doc.Create.NewDimension(view, dim_line, ref_array)
                        if dim:
                            cg_count += 1
                            total += 1
                    except Exception as e:
                        errors += 1
                        output.print_md("  - **Error** center+grid wall {}: {}".format(
                            _eid(wall.Id), str(e)))
                
                output.print_md("- Center to Grid: {} created".format(cg_count))
        

        # === LINKED WALL THICKNESS + NEAREST GRID ===
        if do_linked_grid:
            grids = get_all_grids()
            if not grids:
                output.print_md("- **No grids found** in project")
            else:
                link_instances = get_link_instances()
                if not link_instances:
                    output.print_md("- **No linked files found** in current view")
                else:
                    output.print_md("- Found {} link(s), {} grids".format(
                        len(link_instances), len(grids)))
                    output.print_md("- Pick linked walls to dimension (Escape to finish)...")
                    
                    # Must commit transaction before picking
                    t.Commit()
                    
                    picked_data = []
                    try:
                        while True:
                            try:
                                picked_ref = uidoc.Selection.PickObject(
                                    ObjectType.LinkedElement,
                                    "Pick a wall in linked file (Escape to finish)")
                                
                                link_elem = doc.GetElement(picked_ref.ElementId)
                                if not isinstance(link_elem, RevitLinkInstance):
                                    output.print_md("  - Not a link element, skipped")
                                    continue
                                
                                link_doc = link_elem.GetLinkDocument()
                                if not link_doc:
                                    continue
                                
                                linked_id = picked_ref.LinkedElementId
                                linked_elem = link_doc.GetElement(linked_id)
                                
                                if isinstance(linked_elem, Wall):
                                    loc = linked_elem.Location
                                    if loc and isinstance(loc, LocationCurve) and isinstance(loc.Curve, Line):
                                        picked_data.append((linked_elem, link_elem))
                                        output.print_md("  - Picked wall {} from '{}'".format(
                                            _eid(linked_elem.Id), link_doc.Title))
                                    else:
                                        output.print_md("  - Not a straight wall, skipped")
                                else:
                                    output.print_md("  - Not a wall ({}), skipped".format(
                                        type(linked_elem).__name__))
                            except:
                                break  # Escape pressed
                    except:
                        pass
                    
                    # Restart transaction for dimensioning
                    t = Transaction(doc, "DQT - Wall Dimensioning (Linked)")
                    t.Start()
                    
                    linked_count = 0
                    for lw, link_inst in picked_data:
                        try:
                            wd = get_linked_wall_direction(lw, link_inst)
                            wm = get_linked_wall_midpoint(lw, link_inst)
                            if not wd or not wm:
                                continue
                            
                            face_refs = get_linked_wall_face_refs(lw, link_inst)
                            if len(face_refs) < 2:
                                output.print_md("  - No face refs for wall {}".format(_eid(lw.Id)))
                                continue
                            
                            nearest, dist = find_nearest_grid_for_linked_wall(lw, link_inst, grids)
                            if not nearest:
                                output.print_md("  - No parallel grid for wall {}".format(_eid(lw.Id)))
                                continue
                            
                            grid_ref = get_grid_ref(nearest)
                            if not grid_ref:
                                continue
                            
                            ref_array = ReferenceArray()
                            ref_array.Append(grid_ref)
                            ref_array.Append(face_refs[0])
                            ref_array.Append(face_refs[1])
                            
                            wn = DB.XYZ(-wd.Y, wd.X, 0).Normalize()
                            p1 = DB.XYZ(wm.X, wm.Y, 0) + wn * (dist + 5.0)
                            p2 = DB.XYZ(wm.X, wm.Y, 0) - wn * (dist + 5.0)
                            
                            if p1.DistanceTo(p2) < 0.01:
                                continue
                            
                            dim_line = Line.CreateBound(p1, p2)
                            dim = doc.Create.NewDimension(view, dim_line, ref_array)
                            if dim:
                                linked_count += 1
                                total += 1
                        except Exception as e:
                            errors += 1
                            output.print_md("  - **Error** wall {}: {}".format(_eid(lw.Id), str(e)))
                    
                    output.print_md("- Linked Wall + Grid: {} created".format(linked_count))

        t.Commit()
        
        output.print_md("---")
        output.print_md("**Done!** {} dimension(s) created. {} error(s).".format(total, errors))
        
        if total == 0 and errors == 0:
            output.print_md("**Tips:**")
            output.print_md("- Face mode needs 2+ parallel walls (horizontal or vertical)")
            output.print_md("- Grid mode needs grids parallel to walls in the project")
            output.print_md("- Only straight walls are supported")
            output.print_md("- Make sure walls are visible in current view")
    
    except Exception as e:
        try:
            t.RollBack()
        except:
            pass
        output.print_md("**FATAL ERROR:** {}".format(str(e)))
        TaskDialog.Show("Error", str(e))


if __name__ == '__main__':
    main()