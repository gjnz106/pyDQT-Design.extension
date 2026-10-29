# -*- coding: utf-8 -*-
"""
Split Ceiling Tool
Splits a ceiling with multiple disconnected boundaries into separate individual ceilings.
Dang Quoc Truong - DQT (c) 2026
"""
try: __import__("dqt_usage").log_use(__file__)  # pyDQT usage log
except Exception: pass

__title__ = "Split\nCeiling"
__author__ = "DQT"

from Autodesk.Revit.DB import *
from Autodesk.Revit.UI.Selection import ObjectType
from pyrevit import revit, DB, UI, forms
import math
import clr
clr.AddReference('System.Core')
from System.Collections.Generic import List


def _eid_int(eid):
    """Get integer value of an ElementId across Revit 2024-2027.
    .IntegerValue is deprecated in 2024+ and removed in 2026+; use .Value."""
    try:
        return eid.Value
    except:
        return eid.IntegerValue


doc = revit.doc
uidoc = revit.uidoc


def get_curve_loops_from_ceiling(ceiling):
    """Extract all curve loops from a ceiling's sketch"""
    curve_loops = []

    sketch_filter = DB.ElementClassFilter(DB.Sketch)
    dependent_elements = ceiling.GetDependentElements(sketch_filter)

    if dependent_elements.Count > 0:
        sketch_id = dependent_elements[0]
        sketch_obj = doc.GetElement(sketch_id)

        if sketch_obj:
            profile = sketch_obj.Profile

            for curve_array in profile:
                curve_loop = CurveLoop()
                for curve in curve_array:
                    curve_loop.Append(curve)
                curve_loops.append(curve_loop)

    return curve_loops


def check_if_loop_is_inside(inner_loop, outer_loop):
    """Check if inner_loop is inside outer_loop"""
    test_point = None
    for curve in inner_loop:
        test_point = curve.GetEndPoint(0)
        break

    if not test_point:
        return False

    ray_end = XYZ(test_point.X + 10000, test_point.Y, test_point.Z)
    ray = Line.CreateBound(test_point, ray_end)

    intersection_count = 0
    for curve in outer_loop:
        try:
            result = curve.Intersect(ray)
            if result == DB.SetComparisonResult.Overlap:
                intersection_count += 1
        except:
            pass

    return intersection_count % 2 == 1


def get_ceiling_level(ceiling):
    """Level a ceiling is hosted on - .LevelId first, BuiltInParameter fallback."""
    try:
        lid = ceiling.LevelId
        if lid and lid != ElementId.InvalidElementId:
            return doc.GetElement(lid)
    except:
        pass
    try:
        p = ceiling.get_Parameter(DB.BuiltInParameter.LEVEL_PARAM)
        if p:
            lvl = doc.GetElement(p.AsElementId())
            if lvl:
                return lvl
    except:
        pass
    return None


def create_ceiling_from_curves(ceiling_type, level, outer_loop, holes=None):
    """Create a new ceiling from curve loops"""
    try:
        type_name = ceiling_type.get_Parameter(DB.BuiltInParameter.SYMBOL_NAME_PARAM).AsString()
    except:
        type_name = "Unknown Type"

    try:
        level_name = level.Name
    except:
        level_name = "Unknown Level"

    print("  Ceiling type: {} (ID: {})".format(type_name, _eid_int(ceiling_type.Id)))
    print("  Level: {} (ID: {})".format(level_name, _eid_int(level.Id)))

    curve_count = sum(1 for _ in outer_loop)
    print("  Added {} curves to outer boundary".format(curve_count))

    print("  Calling Ceiling.Create...")

    curve_loops = List[CurveLoop]()
    curve_loops.Add(outer_loop)

    if holes and len(holes) > 0:
        print("  Adding {} holes to CurveLoop list".format(len(holes)))
        for hole in holes:
            curve_loops.Add(hole)

    new_ceiling = DB.Ceiling.Create(doc, curve_loops, ceiling_type.Id, level.Id)
    print("  Ceiling created successfully: {}".format(_eid_int(new_ceiling.Id)))

    return new_ceiling


def split_ceiling(ceiling):
    """Split a ceiling with multiple boundaries into separate ceilings"""
    ceiling_type_id = ceiling.GetTypeId()
    ceiling_type = doc.GetElement(ceiling_type_id)
    level = get_ceiling_level(ceiling)
    if level is None:
        print("  ERROR: could not find the ceiling's level - skipping")
        return None

    curve_loops = get_curve_loops_from_ceiling(ceiling)

    if len(curve_loops) <= 1:
        print("  Ceiling has only one boundary - skipping")
        return None

    print("\nFound {} curve loops in ceiling".format(len(curve_loops)))

    loop_data = []
    for i, loop in enumerate(curve_loops):
        curve_count = sum(1 for _ in loop)

        points = []
        for curve in loop:
            points.append(curve.GetEndPoint(0))
            points.append(curve.GetEndPoint(1))

        if points:
            min_x = min(p.X for p in points)
            max_x = max(p.X for p in points)
            min_y = min(p.Y for p in points)
            max_y = max(p.Y for p in points)
            bbox_area = (max_x - min_x) * (max_y - min_y)
        else:
            bbox_area = 0

        loop_data.append({
            'index': i,
            'loop': loop,
            'curve_count': curve_count,
            'area': bbox_area,
        })

        print("Loop {}: {} curves, area = {:.2f}".format(i, curve_count, bbox_area))

    loop_data.sort(key=lambda x: x['area'], reverse=True)

    print("\nChecking for inside/outside relationships...")

    is_hole = [False] * len(loop_data)
    parent_of = [-1] * len(loop_data)

    for i in range(len(loop_data)):
        for j in range(len(loop_data)):
            if i == j:
                continue
            if check_if_loop_is_inside(loop_data[i]['loop'], loop_data[j]['loop']):
                if loop_data[j]['area'] > loop_data[i]['area']:
                    is_hole[i] = True
                    parent_of[i] = j
                    print("  Loop {} is inside Loop {} (hole of parent)".format(
                        loop_data[i]['index'], loop_data[j]['index']))
                    break

    main_boundaries = []
    for i, data in enumerate(loop_data):
        if not is_hole[i]:
            holes = []
            for j in range(len(loop_data)):
                if parent_of[j] == i:
                    holes.append(loop_data[j]['loop'])

            main_boundaries.append({
                'loop': data['loop'],
                'holes': holes,
                'index': data['index'],
                'area': data['area'],
                'curve_count': data['curve_count']
            })

    print("\nAnalysis:")
    print("  Total loops: {}".format(len(loop_data)))
    print("  Main boundaries: {}".format(len(main_boundaries)))
    print("  Holes: {}".format(sum(is_hole)))

    if len(main_boundaries) <= 1:
        print("\nOnly one main boundary found - this is a single ceiling with holes - skipping split")
        return None

    print("\nCreating {} separate ceilings".format(len(main_boundaries)))

    t = Transaction(doc, "DQT - Split Ceiling {} into {} Ceilings".format(
        _eid_int(ceiling.Id), len(main_boundaries)))
    t.Start()

    try:
        new_ceilings = []

        for idx, data in enumerate(main_boundaries):
            print("\nCreating ceiling {} (area: {:.2f}, {} curves, {} holes)".format(
                idx + 1, data['area'], data['curve_count'], len(data['holes'])))
            try:
                new_ceiling = create_ceiling_from_curves(
                    ceiling_type,
                    level,
                    data['loop'],
                    data['holes'] if len(data['holes']) > 0 else None
                )
                new_ceilings.append(new_ceiling)
            except Exception as e:
                print("  WARNING: Failed to create ceiling {}: {}".format(idx + 1, str(e)))

        doc.Delete(ceiling.Id)

        t.Commit()

        return new_ceilings

    except Exception as e:
        t.RollBack()
        import traceback
        print("\n=== ERROR IN SPLIT_CEILING ===")
        print(traceback.format_exc())
        raise e


def main():
    """Main function"""
    try:
        result = forms.alert(
            "Select multiple ceilings with disconnected boundaries to split.\n\n"
            "Click OK to start selecting ceilings.\n"
            "Press ESC or Finish when done.",
            title="Split Ceiling Tool",
            ok=True,
            cancel=True
        )

        if not result:
            return

        selected_ceilings = []
        try:
            references = uidoc.Selection.PickObjects(
                ObjectType.Element,
                "Select ceilings to split (Press ESC or Finish when done)"
            )

            for ref in references:
                element = doc.GetElement(ref.ElementId)
                if isinstance(element, Ceiling):
                    selected_ceilings.append(element)
                else:
                    print("Skipping non-ceiling element: {} (ID: {})".format(
                        element.Category.Name if element.Category else "Unknown",
                        element.Id
                    ))
        except:
            return

        if not selected_ceilings:
            forms.alert("No ceilings selected.", exitscript=True)

        print("\n" + "=" * 60)
        print("SPLIT CEILING TOOL - Processing {} ceiling(s)".format(len(selected_ceilings)))
        print("=" * 60)

        total_created = 0
        successful_splits = 0
        failed_splits = 0

        for idx, ceiling in enumerate(selected_ceilings):
            print("\n" + "-" * 60)
            print("Processing Ceiling {}/{} (ID: {})".format(
                idx + 1, len(selected_ceilings), _eid_int(ceiling.Id)))
            print("-" * 60)

            try:
                new_ceilings = split_ceiling(ceiling)
                if new_ceilings:
                    total_created += len(new_ceilings)
                    successful_splits += 1
                    print("SUCCESS: Created {} ceilings from this split".format(len(new_ceilings)))
            except Exception as e:
                failed_splits += 1
                print("FAILED: {}".format(str(e)))
                continue

        print("\n" + "=" * 60)
        print("SUMMARY")
        print("=" * 60)
        print("Ceilings processed: {}".format(len(selected_ceilings)))
        print("Successful splits: {}".format(successful_splits))
        print("Failed splits: {}".format(failed_splits))
        print("Total new ceilings created: {}".format(total_created))
        print("=" * 60)

        summary_message = (
            "Split Ceiling Complete!\n\n"
            "Processed: {} ceiling(s)\n"
            "Successful: {}\n"
            "Failed: {}\n"
            "Total new ceilings created: {}"
        ).format(len(selected_ceilings), successful_splits, failed_splits, total_created)

        forms.alert(summary_message, title="Split Ceiling Summary")

    except Exception as e:
        import traceback
        print("\n=== MAIN ERROR ===")
        print(traceback.format_exc())
        forms.alert("Error: {}".format(str(e)), exitscript=True)


if __name__ == "__main__":
    main()
