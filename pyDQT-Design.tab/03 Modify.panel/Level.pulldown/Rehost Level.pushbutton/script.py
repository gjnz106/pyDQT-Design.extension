# -*- coding: utf-8 -*-
"""Rehost Level

Re-associates every element hosted on a SOURCE level onto a TARGET level
(at a different elevation) while keeping each element's exact position and
shape - so you can then delete the old level safely.

Typical use: you made a new level "Level 1!" at FFL 1150 and want everything
currently on "Level 1" (FFL 1000) to belong to the new one instead, without
anything moving. Afterwards the old level hosts nothing and can be deleted.

How it works (all in ONE transaction - undo with Ctrl+Z to revert):
  - For each element that has a level constraint pointing at the source
    level (wall base/top, column base/top, floor/ceiling level, level-based
    family instance, structural framing reference level, MEP reference
    level), that constraint is repointed to the target level.
  - Because the two levels sit at different elevations, the element would
    jump; the tool measures the real jump (via bounding-box Z) after Revit
    regenerates and subtracts it back out of the element's matching offset
    parameter - so the element ends up exactly where it started, now
    hosted on the target level. Geometry-driven, so it's correct regardless
    of how each category reacts to the level change.

The old level is NOT deleted (deleting a level also deletes its plan views);
the report tells you how many elements, if any, still reference it.

Copyright (c) 2026 Dang Quoc Truong (DQT)
All rights reserved.
"""

__title__ = "Rehost\nLevel"
__author__ = "Dang Quoc Truong (DQT)"
__doc__ = ("Move all elements from one level onto another (different "
           "elevation) without changing their position or shape.")

import os
import clr
clr.AddReference('RevitAPI')
clr.AddReference('RevitAPIUI')
clr.AddReference('PresentationCore')
clr.AddReference('PresentationFramework')
clr.AddReference('WindowsBase')
clr.AddReference('System')

from Autodesk.Revit.DB import (
    Transaction, FilteredElementCollector, Level, ElementId, CategoryType,
    StorageType, BuiltInParameter, ElementTransformUtils, XYZ
)
from pyrevit import forms, script


def _open_help_page(html_filename):
    """Open this tool's page from the shared _Modify_Help folder in the
    default browser. Returns True on success, False if the caller should
    fall back to the in-app help text (e.g. the folder went missing)."""
    try:
        panel_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        path = os.path.join(panel_dir, "_Modify_Help", html_filename)
        if not os.path.isfile(path):
            return False
        os.startfile(path)
        return True
    except Exception:
        return False

import System
from System.Collections.Generic import List
from System.Windows import (
    Window, WindowStartupLocation, Thickness, HorizontalAlignment,
    TextWrapping, FontWeights, CornerRadius, SizeToContent, ResizeMode
)
from System.Windows.Controls import (
    StackPanel, Button, TextBlock, RadioButton, Border, ComboBox,
    ComboBoxItem, Orientation
)
from System.Windows.Media import SolidColorBrush, Color, Colors, FontFamily

doc = __revit__.ActiveUIDocument.Document
uidoc = __revit__.ActiveUIDocument

DQT_PRIMARY   = Color.FromRgb(0xF0, 0xCC, 0x88)
DQT_ACCENT    = Color.FromRgb(0xC8, 0x96, 0x50)
DQT_BG        = Color.FromRgb(0xFE, 0xF8, 0xE7)
DQT_DARK      = Color.FromRgb(0x3C, 0x3C, 0x3C)
DQT_WHITE     = Colors.White
DQT_BORDER    = Color.FromRgb(0xDD, 0xDD, 0xDD)
DQT_TEXT_DARK = Color.FromRgb(0x33, 0x33, 0x33)

MM_PER_FT = 304.8


def B(color):
    return SolidColorBrush(color)


def _eid_int(eid):
    try:
        return eid.Value
    except:
        return eid.IntegerValue


def _bip(name):
    return getattr(BuiltInParameter, name, None)


# Level-constraint parameter -> candidate offset parameter(s) -> which Z edge
# the constraint drives ('base' = bbox.Min.Z, 'top' = bbox.Max.Z). Only pairs
# whose parameters actually exist on a given element are used. Missing enums
# (across Revit versions) resolve to None via _bip and are skipped.
_RAW_SPECS = [
    ("WALL_BASE_CONSTRAINT", ["WALL_BASE_OFFSET"], "base"),
    ("WALL_HEIGHT_TYPE", ["WALL_TOP_OFFSET"], "top"),
    ("FAMILY_BASE_LEVEL_PARAM", ["FAMILY_BASE_LEVEL_OFFSET_PARAM"], "base"),
    ("FAMILY_TOP_LEVEL_PARAM", ["FAMILY_TOP_LEVEL_OFFSET_PARAM"], "top"),
    ("FAMILY_LEVEL_PARAM", ["INSTANCE_ELEVATION_PARAM"], "base"),
    ("LEVEL_PARAM", ["FLOOR_HEIGHTABOVELEVEL_PARAM",
                     "CEILING_HEIGHTABOVELEVEL_PARAM"], "base"),
    ("INSTANCE_REFERENCE_LEVEL_PARAM", [], "base"),
    ("RBS_START_LEVEL_PARAM", ["RBS_START_OFFSET_PARAM", "RBS_OFFSET_PARAM"],
     "base"),
]

LEVEL_SPECS = []
for lp_name, off_names, kind in _RAW_SPECS:
    lp = _bip(lp_name)
    if lp is None:
        continue
    offs = [_bip(n) for n in off_names]
    offs = [o for o in offs if o is not None]
    LEVEL_SPECS.append((lp, offs, kind))


def get_levels():
    lvls = list(FilteredElementCollector(doc).OfClass(Level)
                .WhereElementIsNotElementType())
    lvls.sort(key=lambda l: l.Elevation)
    return lvls


def find_associations(elem, old_id):
    """[(level_param_bip, offset_param_bip_or_None, kind), ...] for every level
    constraint on elem that currently points at old_id."""
    res = []
    for (lp_bip, off_bips, kind) in LEVEL_SPECS:
        p = elem.get_Parameter(lp_bip)
        if p is None:
            continue
        try:
            if p.StorageType != StorageType.ElementId:
                continue
            val = p.AsElementId()
        except:
            continue
        if val is None or _eid_int(val) != _eid_int(old_id):
            continue
        op_bip = None
        for ob in off_bips:
            if elem.get_Parameter(ob) is not None:
                op_bip = ob
                break
        res.append((lp_bip, op_bip, kind))
    return res


def collect_candidates(old_id, scope_ids):
    """[(elem, associations)] for elements hosted on the source level."""
    if scope_ids:
        source = [doc.GetElement(eid) for eid in scope_ids]
    else:
        source = list(FilteredElementCollector(doc)
                      .WhereElementIsNotElementType())
    out = []
    for el in source:
        if el is None:
            continue
        cat = el.Category
        if cat is None or cat.CategoryType != CategoryType.Model:
            continue
        if isinstance(el, Level):
            continue
        try:
            assoc = find_associations(el, old_id)
        except:
            assoc = []
        if assoc:
            out.append((el, assoc))
    return out


def _z_edge(bbox, kind):
    return bbox.Min.Z if kind == "base" else bbox.Max.Z


def _elem_bbox(elem):
    try:
        return elem.get_BoundingBox(None)
    except:
        return None


def rehost(old_level, new_level, candidates):
    """Repoint level constraints old->new and keep every element in place.
    Returns a result dict. All inside one committed transaction."""
    tol = 0.3 / MM_PER_FT

    before = {}
    for elem, _assoc in candidates:
        bb = _elem_bbox(elem)
        if bb is not None:
            before[_eid_int(elem.Id)] = (bb.Min.Z, bb.Max.Z)

    level_readonly = set()          # could NOT change level parameter
    assoc_by_eid = {}               # iid -> [(op_bip, kind), ...] (rehosted)

    t = Transaction(doc, "DQT - Rehost {} -> {}".format(
        old_level.Name, new_level.Name))
    t.Start()
    try:
        # Pass 1: repoint every matching level constraint to the new level.
        for elem, assoc in candidates:
            iid = _eid_int(elem.Id)
            for (lp_bip, op_bip, kind) in assoc:
                p = elem.get_Parameter(lp_bip)
                if p is None:
                    continue
                if p.IsReadOnly:
                    level_readonly.add(iid)
                    continue
                try:
                    p.Set(new_level.Id)
                    assoc_by_eid.setdefault(iid, []).append((op_bip, kind))
                except:
                    level_readonly.add(iid)

        doc.Regenerate()

        # Pass 2: subtract the real vertical shift back out of each matching
        # offset parameter, so the element returns to its exact elevation.
        for iid, ops in assoc_by_eid.items():
            elem = doc.GetElement(ElementId(iid))
            bz = before.get(iid)
            bb = _elem_bbox(elem)
            if elem is None or bz is None or bb is None:
                continue
            for (op_bip, kind) in ops:
                resid = _z_edge(bb, kind) - (bz[0] if kind == "base" else bz[1])
                if abs(resid) <= tol:
                    continue
                op = elem.get_Parameter(op_bip) if op_bip else None
                if op is None or op.IsReadOnly:
                    continue
                try:
                    op.Set(op.AsDouble() - resid)
                except:
                    pass

        doc.Regenerate()

        # Pass 3: rescue anything still off by physically moving it back, but
        # ONLY when the whole element shifted uniformly (min and max moved the
        # same amount) - that means it's safe to translate. Partial elements
        # (base moved, top pinned to another level) are left for the offset
        # pass and never blindly moved.
        for iid in assoc_by_eid.keys():
            elem = doc.GetElement(ElementId(iid))
            bz = before.get(iid)
            bb = _elem_bbox(elem)
            if elem is None or bz is None or bb is None:
                continue
            dmin = bb.Min.Z - bz[0]
            dmax = bb.Max.Z - bz[1]
            if abs(dmin) <= tol and abs(dmax) <= tol:
                continue
            if abs(dmin - dmax) <= tol:      # uniform shift -> safe to move
                try:
                    ElementTransformUtils.MoveElement(
                        doc, ElementId(iid), XYZ(0, 0, -dmin))
                except:
                    pass

        doc.Regenerate()

        # Pass 4: final verdict per element.
        rehosted = set(assoc_by_eid.keys())
        moved = set()
        for iid, ops in assoc_by_eid.items():
            elem = doc.GetElement(ElementId(iid))
            bz = before.get(iid)
            bb = _elem_bbox(elem)
            if elem is None or bz is None or bb is None:
                continue
            for (op_bip, kind) in ops:
                resid = _z_edge(bb, kind) - (bz[0] if kind == "base" else bz[1])
                if abs(resid) > tol * 3:
                    moved.add(iid)
                    break

        t.Commit()
    except Exception:
        t.RollBack()
        raise

    # Which elements still reference the old level (blocks safe deletion)?
    remaining = set()
    for el in FilteredElementCollector(doc).WhereElementIsNotElementType():
        try:
            if el.Category and el.Category.CategoryType == CategoryType.Model \
                    and not isinstance(el, Level) \
                    and find_associations(el, old_level.Id):
                remaining.add(_eid_int(el.Id))
        except:
            pass

    return {
        'rehosted': rehosted,
        'level_readonly': level_readonly - rehosted,
        'moved': moved,
        'remaining': remaining,
    }


def category_tally(iids):
    """Sorted [(category_name, count), ...] for a set of element ids."""
    counts = {}
    for iid in iids:
        el = doc.GetElement(ElementId(iid))
        name = el.Category.Name if (el and el.Category) else "?"
        counts[name] = counts.get(name, 0) + 1
    return sorted(counts.items(), key=lambda kv: kv[1], reverse=True)


# =====================================================================
#  DIALOG
# =====================================================================

class RehostDialog(Window):
    def __init__(self, levels, selection_count):
        self.levels = levels
        self.selection_count = selection_count
        self.result = None
        self._build()

    def _st(self, t):
        b = TextBlock(); b.Text = t; b.FontSize = 12
        b.FontWeight = FontWeights.SemiBold; b.Foreground = B(DQT_ACCENT)
        b.Margin = Thickness(0, 0, 0, 6); return b

    def _nt(self, t):
        b = TextBlock(); b.Text = t; b.FontSize = 10
        b.Foreground = B(Color.FromRgb(0x99, 0x99, 0x99))
        b.TextWrapping = TextWrapping.Wrap
        b.Margin = Thickness(2, 2, 0, 2); return b

    def _rb(self, t, checked, group, enabled=True):
        r = RadioButton(); r.Content = t; r.GroupName = group
        r.IsChecked = System.Nullable[System.Boolean](checked)
        r.IsEnabled = enabled
        r.Margin = Thickness(4, 3, 0, 3); r.FontSize = 11.5
        r.Foreground = B(DQT_TEXT_DARK); return r

    def _cd(self, ch):
        b = Border(); b.Background = B(DQT_WHITE)
        b.BorderBrush = B(DQT_BORDER); b.BorderThickness = Thickness(1)
        b.CornerRadius = CornerRadius(4)
        b.Margin = Thickness(16, 10, 16, 0)
        b.Padding = Thickness(12, 10, 12, 10)
        p = StackPanel()
        for c in ch:
            p.Children.Add(c)
        b.Child = p; return b

    def _mk_level_combo(self):
        cmb = ComboBox(); cmb.FontSize = 11.5; cmb.Height = 28
        for lvl in self.levels:
            item = ComboBoxItem()
            item.Content = "{}   ({:.0f} mm)".format(
                lvl.Name, lvl.Elevation * MM_PER_FT)
            item.Tag = lvl.Id
            cmb.Items.Add(item)
        return cmb

    def _build(self):
        self.Title = "Rehost Level - DQT"
        self.Width = 460; self.SizeToContent = SizeToContent.Height
        self.WindowStartupLocation = WindowStartupLocation.CenterScreen
        self.ResizeMode = ResizeMode.NoResize; self.Background = B(DQT_BG)

        m = StackPanel()

        hd = Border(); hd.Background = B(DQT_DARK)
        hd.Padding = Thickness(16, 12, 16, 12)
        hp = StackPanel()
        t = TextBlock(); t.Text = "Rehost Level"
        t.FontSize = 16; t.FontWeight = FontWeights.Bold
        t.Foreground = B(DQT_PRIMARY); t.FontFamily = FontFamily("Segoe UI")
        hp.Children.Add(t)
        s = TextBlock(); s.Text = "Dang Quoc Truong (DQT)"
        s.FontSize = 10; s.Foreground = B(DQT_ACCENT)
        s.Margin = Thickness(0, 2, 0, 0); hp.Children.Add(s)
        hd.Child = hp; m.Children.Add(hd)

        self.cmb_src = self._mk_level_combo()
        self.cmb_dst = self._mk_level_combo()
        if len(self.levels) >= 1:
            self.cmb_src.SelectedIndex = 0
        if len(self.levels) >= 2:
            self.cmb_dst.SelectedIndex = 1
        m.Children.Add(self._cd([
            self._st("Move elements FROM (source level)"), self.cmb_src]))
        m.Children.Add(self._cd([
            self._st("ONTO (target level)"), self.cmb_dst,
            self._nt("Elements keep their exact elevation - only their host "
                     "level changes.")]))

        has_sel = self.selection_count > 0
        self.rb_all = self._rb("All elements on the source level",
                               checked=(not has_sel), group="Scope")
        self.rb_sel = self._rb(
            "Current selection ({} element(s))".format(self.selection_count),
            checked=has_sel, group="Scope", enabled=has_sel)
        m.Children.Add(self._cd([
            self._st("Scope"), self.rb_all, self.rb_sel,
            self._nt("Runs in one transaction - press Ctrl+Z to undo "
                     "everything. The old level is NOT deleted (that also "
                     "deletes its plan views); delete it yourself once the "
                     "report shows 0 elements remaining on it.")]))

        bp = StackPanel(); bp.Orientation = Orientation.Horizontal
        bp.HorizontalAlignment = HorizontalAlignment.Right
        bp.Margin = Thickness(16, 14, 16, 14)
        bh = Button(); bh.Content = "? Help"; bh.Width = 70; bh.Height = 32
        bh.FontSize = 12; bh.Margin = Thickness(0, 0, 8, 0)
        bh.Background = B(DQT_WHITE); bh.Foreground = B(DQT_TEXT_DARK)
        bh.Click += self._on_help; bp.Children.Add(bh)
        bc = Button(); bc.Content = "Cancel"; bc.Width = 90; bc.Height = 32
        bc.FontSize = 12; bc.Margin = Thickness(0, 0, 8, 0)
        bc.Background = B(DQT_WHITE); bc.Foreground = B(DQT_TEXT_DARK)
        bc.Click += self._cancel; bp.Children.Add(bc)
        br = Button(); br.Content = "Rehost"
        br.Width = 120; br.Height = 32; br.FontSize = 12
        br.FontWeight = FontWeights.SemiBold
        br.Background = B(DQT_ACCENT); br.Foreground = B(DQT_WHITE)
        br.Click += self._run; bp.Children.Add(br)
        m.Children.Add(bp)

        f = Border(); f.Background = B(DQT_DARK)
        f.Padding = Thickness(16, 6, 16, 6)
        ft = TextBlock()
        ft.Text = "Copyright (c) 2026 Dang Quoc Truong (DQT)"
        ft.FontSize = 9; ft.Foreground = B(DQT_ACCENT)
        ft.HorizontalAlignment = HorizontalAlignment.Center
        f.Child = ft; m.Children.Add(f)
        self.Content = m

    def _lvl(self, cmb):
        item = cmb.SelectedItem
        return doc.GetElement(item.Tag) if item else None

    def _cancel(self, s, e):
        self.result = None; self.Close()

    def _on_help(self, s, e):
        if _open_help_page("rehost_level.html"):
            return
        forms.alert(
            "Re-associates every element hosted on a SOURCE level onto a "
            "TARGET level (at a different elevation) while keeping each "
            "element's exact position and shape - so the old level can "
            "then be deleted safely.\n\n"
            "- Pick the Source level (what's being emptied) and the Target "
            "level (where elements move to).\n"
            "- Scope: check the entire model, or just the current selection.\n"
            "- Runs in one transaction - Ctrl+Z undoes everything.\n"
            "- The old level is NOT deleted automatically (deleting a level "
            "also deletes its plan views) - the report tells you how many "
            "elements, if any, still reference it.",
            title="Rehost Level")

    def _run(self, s, e):
        src = self._lvl(self.cmb_src)
        dst = self._lvl(self.cmb_dst)
        if src is None or dst is None:
            forms.alert("Select both a source and a target level.", title=__title__)
            return
        if _eid_int(src.Id) == _eid_int(dst.Id):
            forms.alert("Source and target levels must be different.",
                        title=__title__)
            return
        self.result = {
            'src_id': src.Id,
            'dst_id': dst.Id,
            'use_selection': self.rb_sel.IsChecked == True,
        }
        self.Close()


# =====================================================================
#  MAIN
# =====================================================================

def main():
    levels = get_levels()
    if len(levels) < 2:
        forms.alert("Need at least 2 levels (a source and a target).",
                    title=__title__, exitscript=True)

    pre_sel = [eid for eid in uidoc.Selection.GetElementIds()]

    dlg = RehostDialog(levels, len(pre_sel))
    dlg.ShowDialog()
    if dlg.result is None:
        return
    opts = dlg.result

    src = doc.GetElement(opts['src_id'])
    dst = doc.GetElement(opts['dst_id'])
    scope_ids = pre_sel if opts['use_selection'] else None

    candidates = collect_candidates(src.Id, scope_ids)
    if not candidates:
        forms.alert("No elements are hosted on '{}' in the chosen scope."
                    .format(src.Name), title=__title__, exitscript=True)

    delta_mm = (dst.Elevation - src.Elevation) * MM_PER_FT
    if not forms.alert(
            "Rehost {} element(s) from '{}' onto '{}'.\n\n"
            "Elevation difference: {:+.0f} mm (offsets are compensated so "
            "nothing moves).\n\nContinue?".format(
                len(candidates), src.Name, dst.Name, delta_mm),
            title=__title__, ok=True, cancel=True):
        return

    res = rehost(src, dst, candidates)

    kept = res['rehosted'] - res['moved']

    output = script.get_output()
    output.print_md("# Rehost Level: {} -> {}".format(src.Name, dst.Name))
    output.print_md(
        "Elevation difference **{:+.0f} mm** &nbsp;|&nbsp; "
        "Candidates **{}**".format(delta_mm, len(candidates)))
    output.print_md(
        "- Rehosted & kept in place: **{}**\n"
        "- Rehosted but still off elevation: **{}**\n"
        "- Could NOT rehost (level parameter read-only): **{}**\n"
        "- Elements still referencing '{}': **{}**".format(
            len(kept), len(res['moved']), len(res['level_readonly']),
            src.Name, len(res['remaining'])))

    def _dump(title, iids):
        if not iids:
            return
        output.print_md("### {} - by category".format(title))
        data = [[n, c] for (n, c) in category_tally(iids)]
        output.print_table(table_data=data, columns=["Category", "Count"])

    _dump("Could NOT rehost (level read-only)", res['level_readonly'])
    _dump("Rehosted but still off elevation", res['moved'])

    if not res['remaining']:
        output.print_md("### '{}' hosts nothing now - safe to delete "
                        "(this also removes its plan views).".format(src.Name))
    else:
        output.print_md(
            "### '{}' still hosts {} element(s) - deleting the level now would "
            "delete them. See the category breakdown above.".format(
                src.Name, len(res['remaining'])))

    # Select the elements that need attention (read-only + still-off) so they
    # are easy to inspect; if there are none, select the kept ones.
    focus = res['level_readonly'] | res['moved']
    if not focus:
        focus = kept
    sel = List[ElementId]()
    for iid in focus:
        sel.Add(ElementId(iid))
    try:
        uidoc.Selection.SetElementIds(sel)
    except:
        pass

    summary = (
        "Rehost complete: {} -> {}\n\n"
        "Rehosted & kept in place: {}\n"
        "Rehosted but still off elevation: {}\n"
        "Could not rehost (level read-only): {}\n\n"
        "Elements still on '{}': {}\n\n"
        "A per-category breakdown of the failures is in the output window "
        "(the elements needing attention are now selected).\n"
        "Undo with Ctrl+Z if needed."
    ).format(src.Name, dst.Name, len(kept), len(res['moved']),
             len(res['level_readonly']), src.Name, len(res['remaining']))
    forms.alert(summary, title=__title__)


if __name__ == "__main__":
    main()
