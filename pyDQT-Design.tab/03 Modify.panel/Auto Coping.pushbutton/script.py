# -*- coding: utf-8 -*-
"""Auto Coping - DQT
Batch apply / remove coping on steel beams.

Apply  : pick the element(s) to be CUT (coped), then the CUTTING element(s),
         like Revit's own Apply Coping - just in batch.
Remove : pick any related elements; coping is removed in BOTH directions
         (whether the picked element is the cut beam or the cutter), so you
         never have to guess which side holds the coping.

Copyright (c) 2026 by Dang Quoc Truong (DQT)
"""
try: __import__("dqt_usage").log_use(__file__)  # pyDQT usage log
except Exception: pass

__title__ = "Auto\nCoping"
__author__ = "Dang Quoc Truong (DQT)"
__doc__ = ("Batch coping for steel: Apply (pick cut beams then cutters) or "
           "Remove (pick anything, removed both ways).\n"
           "Copyright (c) 2026 by Dang Quoc Truong (DQT)")

from Autodesk.Revit.DB import (
    FilteredElementCollector, FamilyInstance, BuiltInCategory, Transaction,
    ElementId, Outline, BoundingBoxIntersectsFilter, XYZ
)
from Autodesk.Revit.UI.Selection import ObjectType, ISelectionFilter
from pyrevit import forms, script

doc = __revit__.ActiveUIDocument.Document
uidoc = __revit__.ActiveUIDocument
output = script.get_output()

MODE_APPLY = "Apply coping"
MODE_REMOVE = "Remove coping"
BBOX_TOL = 0.05   # ft, ~15mm padding for overlap test
NEAR_TOL = 0.33   # ft, ~100mm padding when scanning neighbours


def _eid_int(eid):
    try:
        return eid.Value
    except:
        try:
            return eid.IntegerValue
        except:
            return -1


def _bic(elem):
    try:
        return elem.Category.BuiltInCategory
    except:
        return None


def is_framing(elem):
    return isinstance(elem, FamilyInstance) and \
        _bic(elem) == BuiltInCategory.OST_StructuralFraming


def is_cutter(elem):
    return isinstance(elem, FamilyInstance) and _bic(elem) in (
        BuiltInCategory.OST_StructuralFraming,
        BuiltInCategory.OST_StructuralColumns)


def bbox_overlap(a, b):
    try:
        ba = a.get_BoundingBox(None)
        bb = b.get_BoundingBox(None)
        if ba is None or bb is None:
            return True
        t = BBOX_TOL
        return not (ba.Max.X + t < bb.Min.X or ba.Min.X - t > bb.Max.X or
                    ba.Max.Y + t < bb.Min.Y or ba.Min.Y - t > bb.Max.Y or
                    ba.Max.Z + t < bb.Min.Z or ba.Min.Z - t > bb.Max.Z)
    except:
        return True


def framing_near(elem):
    """Structural framing whose bounding box overlaps this element."""
    try:
        bb = elem.get_BoundingBox(None)
        if bb is None:
            return []
        t = NEAR_TOL
        o = Outline(XYZ(bb.Min.X - t, bb.Min.Y - t, bb.Min.Z - t),
                    XYZ(bb.Max.X + t, bb.Max.Y + t, bb.Max.Z + t))
        col = (FilteredElementCollector(doc).OfClass(FamilyInstance)
               .WhereElementIsNotElementType()
               .WherePasses(BoundingBoxIntersectsFilter(o)))
        return [e for e in col if is_framing(e)]
    except:
        return []


def coping_ids(beam):
    try:
        return set(_eid_int(x) for x in beam.GetCopingList())
    except:
        return set()


class _FramingFilter(ISelectionFilter):
    def AllowElement(self, e):
        return is_framing(e)

    def AllowReference(self, r, p):
        return False


class _CutterFilter(ISelectionFilter):
    def AllowElement(self, e):
        return is_cutter(e)

    def AllowReference(self, r, p):
        return False


def get_selection_or_pick(check, msg):
    sel = [doc.GetElement(i) for i in uidoc.Selection.GetElementIds()]
    picked = [e for e in sel if check(e)]
    if picked:
        return picked
    flt = _FramingFilter() if check is is_framing else _CutterFilter()
    try:
        refs = uidoc.Selection.PickObjects(ObjectType.Element, flt, msg)
    except:
        return []
    return [doc.GetElement(r.ElementId) for r in refs]


def pick(check, msg):
    flt = _FramingFilter() if check is is_framing else _CutterFilter()
    try:
        refs = uidoc.Selection.PickObjects(ObjectType.Element, flt, msg)
    except:
        return []
    return [doc.GetElement(r.ElementId) for r in refs]


def do_apply():
    cut_targets = get_selection_or_pick(
        is_framing, "STEP 1: select the beams to be CUT (coped), then Finish")
    if not cut_targets:
        forms.alert("No beams to cut were selected.", title="Auto Coping")
        return
    cutters = pick(
        is_cutter,
        "STEP 2: select the CUTTING elements (girders/columns), then Finish")
    if not cutters:
        forms.alert("No cutting elements selected.", title="Auto Coping")
        return

    applied = 0
    t = Transaction(doc, "DQT - Auto Coping (apply)")
    t.Start()
    try:
        for beam in cut_targets:
            if not is_framing(beam):
                continue
            beam_id = _eid_int(beam.Id)
            existing = coping_ids(beam)
            for cutter in cutters:
                cid = _eid_int(cutter.Id)
                if cid == beam_id or cid in existing:
                    continue
                if not bbox_overlap(beam, cutter):
                    continue
                try:
                    beam.AddCoping(cutter)
                    applied += 1
                    existing.add(cid)
                except:
                    pass
        t.Commit()
    except Exception as ex:
        t.RollBack()
        forms.alert("Auto Coping failed:\n{}".format(ex), title="Auto Coping")
        return

    msg = "Applied coping to {} connection(s) on {} beam(s).".format(
        applied, len(cut_targets))
    output.print_md("**Auto Coping** - {}".format(msg))
    forms.alert(msg, title="Auto Coping")


def do_remove():
    targets = get_selection_or_pick(
        is_cutter,
        "Select the connections to UN-COPE (beams and/or cutters), then Finish")
    if not targets:
        forms.alert("Nothing selected.", title="Auto Coping")
        return

    removed = 0
    t = Transaction(doc, "DQT - Auto Coping (remove)")
    t.Start()
    try:
        for elem in targets:
            elem_id = _eid_int(elem.Id)

            # Direction 1: the selected element is the CUT beam.
            if is_framing(elem):
                try:
                    for ceid in list(elem.GetCopingList()):
                        ce = doc.GetElement(ceid)
                        if ce is not None:
                            try:
                                elem.RemoveCoping(ce)
                                removed += 1
                            except:
                                pass
                except:
                    pass

            # Direction 2: the selected element is the CUTTER; remove coping
            # from neighbouring beams that are coped by it.
            for nb in framing_near(elem):
                if _eid_int(nb.Id) == elem_id:
                    continue
                if elem_id in coping_ids(nb):
                    try:
                        nb.RemoveCoping(elem)
                        removed += 1
                    except:
                        pass
        t.Commit()
    except Exception as ex:
        t.RollBack()
        forms.alert("Auto Coping failed:\n{}".format(ex), title="Auto Coping")
        return

    msg = "Removed {} coping(s) involving {} selected element(s).".format(
        removed, len(targets))
    output.print_md("**Auto Coping** - {}".format(msg))
    forms.alert(msg, title="Auto Coping")


def main():
    mode = forms.SelectFromList.show(
        [MODE_APPLY, MODE_REMOVE], title="Auto Coping - mode",
        multiselect=False, button_name="Next")
    if not mode:
        return
    if mode == MODE_APPLY:
        do_apply()
    else:
        do_remove()


if __name__ == "__main__":
    main()
