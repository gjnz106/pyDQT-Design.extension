# -*- coding: utf-8 -*-
"""
Split MEP at Linked Wall v1.0 - DQT
Splits duct / pipe / cable tray / conduit runs in the active document
wherever they physically cross a wall in a selected linked model,
breaking each run into two elements at the wall's center (mid-thickness)
point along the run.

Workflow:
  1. Pick the linked model that holds the walls to split against.
  2. Tick which MEP categories to check (Ducts, Pipes, Cable Trays,
     Conduits).
  3. Run - every straight run of a ticked category that physically
     crosses a wall solid in the link gets broken into two elements at
     the point where the run's centerline crosses the wall's solid.

Copyright (c) 2026 Dang Quoc Truong (DQT)
All rights reserved.
"""

__title__ = "Split MEP\nat Wall"
__author__ = "Dang Quoc Truong (DQT)"
__doc__ = "Split duct/pipe/cable tray/conduit runs at the walls of a linked model."

import clr
clr.AddReference('RevitAPI')
clr.AddReference('RevitAPIUI')
clr.AddReference('PresentationFramework')
clr.AddReference('PresentationCore')
clr.AddReference('WindowsBase')

import os
import System
from System.IO import MemoryStream
from System.Text import Encoding
from System.Windows.Markup import XamlReader
from System.Windows import MessageBox, MessageBoxButton, MessageBoxImage
from System.Windows.Controls import ComboBoxItem
from System.Collections.Generic import List


def _open_help_page(html_filename):
    """Open this tool's page from the shared _Modify_Help folder in the
    default browser. Returns True on success, False if the caller should
    fall back to the in-app help text (e.g. the folder went missing).
    Three dirnames: script.py -> MEP_Split.pushbutton -> Split.pulldown
    -> 03 Modify.panel (the pulldown adds one extra nesting level)."""
    try:
        panel_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        path = os.path.join(panel_dir, "_Modify_Help", html_filename)
        if not os.path.isfile(path):
            return False
        os.startfile(path)
        return True
    except Exception:
        return False


import Autodesk.Revit.DB as DB
from Autodesk.Revit.DB import (
    FilteredElementCollector, BuiltInCategory, Transaction, SubTransaction,
    RevitLinkInstance, Line, XYZ, ElementId, Options,
    SolidCurveIntersectionOptions, LocationCurve, ElementMulticategoryFilter,
)
from Autodesk.Revit.DB.Plumbing import Pipe, PlumbingUtils
from Autodesk.Revit.DB.Mechanical import Duct, MechanicalUtils

doc   = __revit__.ActiveUIDocument.Document
uidoc = __revit__.ActiveUIDocument

# ── Categories offered (straight/curved MEP runs only - not fittings,
#    not flex duct/pipe, which don't have a simple Line centerline) ───────
CATEGORY_OPTIONS = [
    ("ChkDuct",      "Ducts",       BuiltInCategory.OST_DuctCurves),
    ("ChkPipe",      "Pipes",       BuiltInCategory.OST_PipeCurves),
    ("ChkCableTray", "Cable Trays", BuiltInCategory.OST_CableTray),
    ("ChkConduit",   "Conduits",    BuiltInCategory.OST_Conduit),
]

TOLERANCE_FT = 0.01  # ~3mm - de-duplicates break points that are essentially the same
END_CLEARANCE_FT = 0.166  # ~50mm - keep break points clear of the run's own ends;
                          # breaking a hair's-width from an element's end is a known
                          # way to crash Revit's native break-curve implementation


class UnsupportedMEPSplit(Exception):
    """Raised when the current Revit API surface has no way to break the
    given element's category (Cable Tray/Conduit require
    Document.BreakCurve, added in Revit 2022)."""
    pass


# ── Links ──────────────────────────────────────────────────────────────
def get_link_instances():
    """Return [(RevitLinkInstance, Document), ...] for every LOADED link."""
    links = []
    for link in FilteredElementCollector(doc).OfClass(RevitLinkInstance):
        try:
            link_doc = link.GetLinkDocument()
            if link_doc:
                links.append((link, link_doc))
        except Exception:
            continue
    return links


# ── Small geometry helpers ──────────────────────────────────────────────
def _eid_key(eid):
    """Hashable int key for an ElementId across Revit 2024-2027."""
    try:
        return eid.Value
    except AttributeError:
        return eid.IntegerValue


def _bbox_in_host(bb, link_transform):
    """(lo, hi) coordinate tuples of a linked element's bounding box,
    expressed in host coordinates."""
    total = link_transform.Multiply(bb.Transform)
    mn, mx = bb.Min, bb.Max
    xs, ys, zs = [], [], []
    for x in (mn.X, mx.X):
        for y in (mn.Y, mx.Y):
            for z in (mn.Z, mx.Z):
                p = total.OfPoint(XYZ(x, y, z))
                xs.append(p.X)
                ys.append(p.Y)
                zs.append(p.Z)
    return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))


def _bbox_overlap(a_lo, a_hi, b_lo, b_hi, pad=0.05):
    for i in range(3):
        if a_lo[i] - pad > b_hi[i] or a_hi[i] + pad < b_lo[i]:
            return False
    return True


# ── Wall geometry from the link ─────────────────────────────────────────
def collect_wall_entries(link_doc, link_transform):
    """[(wall_id, bbox_lo, bbox_hi), ...] for every wall in the link, in
    host coordinates. Only the cheap bounding box is read here - solid
    geometry is pulled later and only for walls a run actually reaches,
    so a big link doesn't mean extracting every wall's geometry."""
    entries = []
    for wid in FilteredElementCollector(link_doc).OfClass(DB.Wall) \
            .WhereElementIsNotElementType().ToElementIds():
        try:
            wall = link_doc.GetElement(wid)
            if wall is None:
                continue
            bb = wall.get_BoundingBox(None)
            if bb is None:
                continue
            lo, hi = _bbox_in_host(bb, link_transform)
        except Exception:
            continue
        entries.append((wid, lo, hi))
    return entries


def get_wall_solids(link_doc, link_transform, wall_id, cache):
    """Solid geometry of one linked wall in host coordinates, cached -
    the same wall is normally crossed by several runs."""
    key = _eid_key(wall_id)
    if key in cache:
        return cache[key]
    solids = []
    try:
        wall = link_doc.GetElement(wall_id)
        if wall is not None:
            opts = Options()
            opts.ComputeReferences = False
            opts.DetailLevel = DB.ViewDetailLevel.Coarse
            geom = wall.get_Geometry(opts)
            if geom is not None:
                for g in geom:
                    if isinstance(g, DB.Solid) and g.Volume > 1e-6:
                        try:
                            solids.append(DB.SolidUtils.CreateTransformed(g, link_transform))
                        except Exception:
                            continue
    except Exception:
        solids = []
    cache[key] = solids
    return solids


# ── Break a single MEP curve element at a point ──────────────────────────
def break_mep_curve(document, element, point):
    """Split an MEP curve-based element (duct, pipe, cable tray, conduit)
    at `point`. Returns the ElementId of the new (far side) element.
    Raises UnsupportedMEPSplit if this Revit API surface can't break this
    element's category."""
    try:
        return document.BreakCurve(element.Id, point)
    except AttributeError:
        pass  # Document.BreakCurve not present on this Revit API surface
    if isinstance(element, Pipe):
        return PlumbingUtils.BreakCurve(document, element.Id, point)
    if isinstance(element, Duct):
        return MechanicalUtils.BreakCurve(document, element.Id, point)
    raise UnsupportedMEPSplit(
        "Splitting Cable Tray/Conduit needs Document.BreakCurve "
        "(Revit 2022+ API).")


# ── Find where a curve crosses any of the given wall solids ─────────────
def find_break_points(curve, solids):
    """Return the points, ordered along `curve`, where it physically
    crosses one of `solids` - the midpoint of each solid/curve
    intersection segment, projected exactly onto `curve` (the transformed
    wall solid carries tiny floating-point noise the raw midpoint doesn't
    cancel out) and kept clear of the run's own endpoints."""
    p_start = curve.GetEndPoint(0)
    p_end = curve.GetEndPoint(1)

    candidates = []
    curve_opts = SolidCurveIntersectionOptions()
    for solid in solids:
        try:
            inter = solid.IntersectWithCurve(curve, curve_opts)
        except Exception:
            continue
        if inter is None:
            continue
        for i in range(inter.SegmentCount):
            seg = inter.GetCurveSegment(i)
            try:
                mid = seg.Evaluate(0.5, True)
            except Exception:
                p0, p1 = seg.GetEndPoint(0), seg.GetEndPoint(1)
                mid = XYZ((p0.X + p1.X) / 2.0, (p0.Y + p1.Y) / 2.0, (p0.Z + p1.Z) / 2.0)
            try:
                proj = curve.Project(mid)
                pt = proj.XYZPoint
                param = proj.Parameter
            except Exception:
                continue
            # A break right at (or a hair's-width from) the run's own
            # end is a known way to crash Revit's native break-curve
            # implementation instead of raising a catchable error.
            if pt.DistanceTo(p_start) < END_CLEARANCE_FT or pt.DistanceTo(p_end) < END_CLEARANCE_FT:
                continue
            candidates.append((param, pt))

    candidates.sort(key=lambda c: c[0])
    ordered = []
    last_param = None
    for param, pt in candidates:
        if last_param is not None and abs(param - last_param) < TOLERANCE_FT:
            continue
        ordered.append(pt)
        last_param = param
    return ordered


def validated_break_point(element, point):
    """Re-check a planned point against the element as it stands right
    now, immediately before the break. It must still lie on this
    element's own curve and stay clear of both ends - after an earlier
    break the element is shorter, so a later planned point may no longer
    belong to it. Handing Revit's native break API a point that is off
    the curve or on its endpoint is what takes the application down."""
    try:
        loc = element.Location
        if not isinstance(loc, LocationCurve):
            return None
        curve = loc.Curve
        if not isinstance(curve, Line):
            return None
        proj = curve.Project(point)
        if proj is None:
            return None
        pt = proj.XYZPoint
        # Project() clamps to the bounded curve, so a point that now sits
        # beyond this piece comes back displaced - that's the rejection.
        if pt.DistanceTo(point) > TOLERANCE_FT:
            return None
        if pt.DistanceTo(curve.GetEndPoint(0)) < END_CLEARANCE_FT:
            return None
        if pt.DistanceTo(curve.GetEndPoint(1)) < END_CLEARANCE_FT:
            return None
        return pt
    except Exception:
        return None


# ── Collect target elements ──────────────────────────────────────────────
def collect_target_element_ids(categories, view_only):
    """ElementIds, never Element objects: the ids stay valid across the
    document edits below, whereas an Element reference captured up front
    goes stale the moment the document is modified and regenerated."""
    bics = List[BuiltInCategory](categories)
    if view_only:
        collector = FilteredElementCollector(doc, doc.ActiveView.Id)
    else:
        collector = FilteredElementCollector(doc)
    cat_filter = ElementMulticategoryFilter(bics)
    return list(collector.WherePasses(cat_filter).WhereElementIsNotElementType().ToElementIds())


# ── Phase 1: work out every break, touching nothing ─────────────────────
def plan_breaks(element_ids, wall_entries, link_doc, link_transform, report):
    """Read-only pass. Returns [(element_id, [XYZ, ...]), ...] for the
    runs that cross a linked wall. The document is not modified here, so
    nothing can go stale mid-analysis, and each run is only intersected
    against walls whose bounding box it actually reaches."""
    solid_cache = {}
    plan = []
    for eid in element_ids:
        try:
            elem = doc.GetElement(eid)
            if elem is None:
                continue
            loc = elem.Location
            if not isinstance(loc, LocationCurve):
                report['no_location'] += 1
                continue
            curve = loc.Curve
            if not isinstance(curve, Line):
                report['curved'] += 1
                continue

            p0 = curve.GetEndPoint(0)
            p1 = curve.GetEndPoint(1)
            run_lo = (min(p0.X, p1.X), min(p0.Y, p1.Y), min(p0.Z, p1.Z))
            run_hi = (max(p0.X, p1.X), max(p0.Y, p1.Y), max(p0.Z, p1.Z))

            near_solids = []
            for wid, wlo, whi in wall_entries:
                if _bbox_overlap(run_lo, run_hi, wlo, whi):
                    near_solids.extend(
                        get_wall_solids(link_doc, link_transform, wid, solid_cache))

            points = find_break_points(curve, near_solids) if near_solids else []
            if not points:
                report['no_crossing'] += 1
                continue
            plan.append((eid, points))
        except Exception:
            report['errors'] += 1
    return plan


# ── Phase 2: apply the planned breaks ───────────────────────────────────
def apply_breaks(plan, report):
    """Write pass. Every element is re-fetched by id right before it is
    touched, and each run's own sequence of breaks is atomic."""
    for eid, points in plan:
        sub = SubTransaction(doc)
        sub.Start()
        cuts = 0
        unsupported = False
        try:
            current_id = eid
            for pt in points:
                elem = doc.GetElement(current_id)
                if elem is None:
                    break
                safe_pt = validated_break_point(elem, pt)
                if safe_pt is None:
                    continue
                try:
                    new_id = break_mep_curve(doc, elem, safe_pt)
                except UnsupportedMEPSplit:
                    unsupported = True
                    break
                if new_id and new_id != ElementId.InvalidElementId:
                    current_id = new_id
                    cuts += 1
                    doc.Regenerate()
            sub.Commit()
        except Exception:
            sub.RollBack()
            report['errors'] += 1
            continue

        if cuts > 0:
            report['split_elems'] += 1
            report['split_cuts'] += cuts
        elif unsupported:
            report['unsupported'] += 1


# ═══════════════════════════════════════════════════════════════════════════
# XAML UI
# ═══════════════════════════════════════════════════════════════════════════
XAML_TEMPLATE = """
<Window
    xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
    xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
    Title="Split MEP at Linked Wall"
    Width="460"
    SizeToContent="Height"
    MinHeight="560" MaxHeight="820"
    WindowStartupLocation="CenterScreen"
    ResizeMode="NoResize"
    FontFamily="Segoe UI">
  <Window.Resources>
    <Style x:Key="PrimaryBtn" TargetType="Button">
      <Setter Property="Background" Value="%%BTN_BG%%"/>
      <Setter Property="Foreground" Value="White"/>
      <Setter Property="FontSize" Value="13"/>
      <Setter Property="FontWeight" Value="SemiBold"/>
      <Setter Property="Height" Value="38"/>
      <Setter Property="BorderThickness" Value="0"/>
      <Setter Property="Cursor" Value="Hand"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="Button">
            <Border Background="{TemplateBinding Background}"
                    CornerRadius="5"
                    Padding="12,0">
              <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center"/>
            </Border>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>
    <Style x:Key="SecondaryBtn" TargetType="Button" BasedOn="{StaticResource PrimaryBtn}">
      <Setter Property="Background" Value="#9E9E9E"/>
    </Style>
  </Window.Resources>

  <Border Background="White">
    <Grid>
      <Grid.RowDefinitions>
        <RowDefinition Height="Auto"/>
        <RowDefinition Height="*"/>
        <RowDefinition Height="Auto"/>
      </Grid.RowDefinitions>

      <!-- HEADER -->
      <Border Grid.Row="0" Background="%%HEADER_BG%%" CornerRadius="0">
        <Grid Margin="16,14,16,14">
          <Grid.ColumnDefinitions>
            <ColumnDefinition Width="*"/>
            <ColumnDefinition Width="Auto"/>
          </Grid.ColumnDefinitions>
          <StackPanel Grid.Column="0" VerticalAlignment="Center">
            <TextBlock Text="Split MEP at Linked Wall" FontSize="16"
                       FontWeight="Bold" Foreground="%%DARK_ACCENT%%"/>
            <TextBlock Text="Break duct/pipe/tray/conduit runs where they cross a linked wall"
                       FontSize="11" Foreground="%%DARK_ACCENT%%" Opacity="0.75" Margin="0,2,0,0"/>
          </StackPanel>
          <Border Grid.Column="1" Background="%%ACCENT%%" CornerRadius="4"
                  Padding="10,4" VerticalAlignment="Center">
            <TextBlock x:Name="WallCountLabel" Text="0 walls"
                       FontSize="11" FontWeight="SemiBold" Foreground="%%DARK_ACCENT%%"/>
          </Border>
        </Grid>
      </Border>

      <!-- BODY -->
      <StackPanel Grid.Row="1" Margin="16,16,16,12">

        <!-- Step 1 -->
        <Border Background="#F5F5F5" CornerRadius="4" Padding="10,6" Margin="0,0,0,8">
          <TextBlock FontSize="12" Foreground="#555555">
            <Run FontWeight="Bold" Foreground="%%DARK_ACCENT%%">Step 1 — </Run>
            <Run>Pick the linked model that holds the walls</Run>
          </TextBlock>
        </Border>
        <ComboBox x:Name="LinkCombo" Height="32" FontSize="12"
                  VerticalContentAlignment="Center" Margin="0,0,0,14"/>

        <!-- Step 2 -->
        <Border Background="#F5F5F5" CornerRadius="4" Padding="10,6" Margin="0,0,0,8">
          <TextBlock FontSize="12" Foreground="#555555">
            <Run FontWeight="Bold" Foreground="%%DARK_ACCENT%%">Step 2 — </Run>
            <Run>Tick the categories to split</Run>
          </TextBlock>
        </Border>
        <Border BorderBrush="#E0E0E0" BorderThickness="1" CornerRadius="4" Padding="12,10" Margin="0,0,0,14">
          <StackPanel>
            <CheckBox x:Name="ChkDuct" Content="Ducts" FontSize="12" IsChecked="True" Margin="0,0,0,8"/>
            <CheckBox x:Name="ChkPipe" Content="Pipes" FontSize="12" IsChecked="True" Margin="0,0,0,8"/>
            <CheckBox x:Name="ChkCableTray" Content="Cable Trays" FontSize="12" IsChecked="True" Margin="0,0,0,8"/>
            <CheckBox x:Name="ChkConduit" Content="Conduits" FontSize="12" IsChecked="True"/>
          </StackPanel>
        </Border>

        <!-- Step 3 -->
        <Border Background="#F5F5F5" CornerRadius="4" Padding="10,6" Margin="0,0,0,8">
          <TextBlock FontSize="12" Foreground="#555555">
            <Run FontWeight="Bold" Foreground="%%DARK_ACCENT%%">Step 3 — </Run>
            <Run>Choose scope and run</Run>
          </TextBlock>
        </Border>
        <CheckBox x:Name="ChkViewOnly" Content="Active view only (unticked = whole document)"
                  FontSize="12" IsChecked="False" Margin="0,0,0,8"/>
        <CheckBox x:Name="ChkPreview" Content="Preview only — count crossings, cut nothing"
                  FontSize="12" IsChecked="False" Margin="0,0,0,4"/>

        <!-- Status result — always visible after run -->
        <Border x:Name="StatusBorder" CornerRadius="5"
                Padding="12,10" Margin="0,10,0,4" Visibility="Collapsed">
          <StackPanel>
            <TextBlock x:Name="StatusTitle" FontSize="13" FontWeight="Bold"
                       Margin="0,0,0,4"/>
            <TextBlock x:Name="StatusText" FontSize="11"
                       Foreground="#333333" TextWrapping="Wrap" LineHeight="18"/>
          </StackPanel>
        </Border>

      </StackPanel>

      <!-- FOOTER -->
      <Border Grid.Row="2" BorderBrush="#E0E0E0" BorderThickness="0,1,0,0" Padding="16,10">
        <Grid>
          <Grid.ColumnDefinitions>
            <ColumnDefinition Width="*"/>
            <ColumnDefinition Width="Auto"/>
            <ColumnDefinition Width="8"/>
            <ColumnDefinition Width="Auto"/>
            <ColumnDefinition Width="8"/>
            <ColumnDefinition Width="Auto"/>
          </Grid.ColumnDefinitions>
          <TextBlock Grid.Column="0" Text="Dang Quoc Truong - DQT (c) 2026"
                     FontSize="10" Foreground="#AAAAAA" VerticalAlignment="Center"/>
          <Button x:Name="HelpButton" Grid.Column="1"
                  Content="? Help" Width="70"
                  Style="{StaticResource SecondaryBtn}"/>
          <Button x:Name="RunButton" Grid.Column="3"
                  Content="✂  Split at Wall" Width="150"
                  Style="{StaticResource PrimaryBtn}"/>
          <Button x:Name="CloseButton" Grid.Column="5"
                  Content="Close" Width="80"
                  Style="{StaticResource SecondaryBtn}"/>
        </Grid>
      </Border>

    </Grid>
  </Border>
</Window>
""".replace("%%HEADER_BG%%", "#F0CC88") \
   .replace("%%ACCENT%%",    "#D4B87A") \
   .replace("%%DARK_ACCENT%%","#5D4E37") \
   .replace("%%BTN_BG%%",    "#5D4E37")

# ═══════════════════════════════════════════════════════════════════════════
# Main dialog class
# ═══════════════════════════════════════════════════════════════════════════
class MEPSplitDialog(object):

    def __init__(self):
        self.link_options = get_link_instances()
        self.selected_link = None
        self.selected_link_doc = None

        stream = MemoryStream(Encoding.UTF8.GetBytes(XAML_TEMPLATE))
        self.window = XamlReader.Load(stream)

        # Controls
        self._wall_count_lbl = self.window.FindName("WallCountLabel")
        self._link_combo     = self.window.FindName("LinkCombo")
        self._chk_view_only  = self.window.FindName("ChkViewOnly")
        self._chk_preview    = self.window.FindName("ChkPreview")
        self._status_bdr     = self.window.FindName("StatusBorder")
        self._status_title   = self.window.FindName("StatusTitle")
        self._status_txt     = self.window.FindName("StatusText")
        self._run_btn        = self.window.FindName("RunButton")
        self._close_btn      = self.window.FindName("CloseButton")
        self._help_btn       = self.window.FindName("HelpButton")

        self._cat_checks = [
            (self.window.FindName(ctrl_name), display, bic)
            for ctrl_name, display, bic in CATEGORY_OPTIONS
        ]

        # Init
        self._populate_links()
        if not self.link_options:
            self._show_status(
                "⚠  No linked models loaded",
                "This document has no loaded Revit links. Load the link "
                "that contains the walls first, then reopen this tool.",
                success=False)
            self._run_btn.IsEnabled = False

        # Events
        self._link_combo.SelectionChanged += self._on_link_changed
        self._run_btn.Click   += self._on_run
        self._close_btn.Click += self._on_close
        self._help_btn.Click  += self._on_help

        # _populate_links() set SelectedIndex before the handler above was
        # wired, so sync the cached selection now.
        self._on_link_changed(None, None)

    # ── Link combo helpers ────────────────────────────────────────────────
    def _populate_links(self):
        self._link_combo.Items.Clear()
        for link, link_doc in self.link_options:
            item = ComboBoxItem()
            try:
                item.Content = link.Name
            except Exception:
                item.Content = "Link {0}".format(link.Id)
            self._link_combo.Items.Add(item)
        if self._link_combo.Items.Count > 0:
            self._link_combo.SelectedIndex = 0

    def _on_link_changed(self, sender, e):
        idx = self._link_combo.SelectedIndex
        if idx < 0 or idx >= len(self.link_options):
            self.selected_link = None
            self.selected_link_doc = None
            self._wall_count_lbl.Text = "0 walls"
            return
        self.selected_link, self.selected_link_doc = self.link_options[idx]
        try:
            wall_count = FilteredElementCollector(self.selected_link_doc) \
                .OfClass(DB.Wall).WhereElementIsNotElementType().GetElementCount()
        except Exception:
            wall_count = 0
        self._wall_count_lbl.Text = "{0} wall(s)".format(wall_count)

    # ── Status helper ─────────────────────────────────────────────────────
    def _show_status(self, title, detail, success=True):
        conv = System.Windows.Media.BrushConverter()
        if success:
            self._status_bdr.Background = conv.ConvertFromString("#E8F5E9")
            self._status_bdr.SetValue(
                System.Windows.Controls.Border.BorderBrushProperty,
                conv.ConvertFromString("#A5D6A7"))
            self._status_bdr.SetValue(
                System.Windows.Controls.Border.BorderThicknessProperty,
                System.Windows.Thickness(1))
            self._status_title.Foreground = conv.ConvertFromString("#1B5E20")
        else:
            self._status_bdr.Background = conv.ConvertFromString("#FFF3E0")
            self._status_bdr.SetValue(
                System.Windows.Controls.Border.BorderBrushProperty,
                conv.ConvertFromString("#FFCC80"))
            self._status_bdr.SetValue(
                System.Windows.Controls.Border.BorderThicknessProperty,
                System.Windows.Thickness(1))
            self._status_title.Foreground = conv.ConvertFromString("#E65100")

        self._status_title.Text = title
        self._status_txt.Text   = detail
        self._status_bdr.Visibility = System.Windows.Visibility.Visible
        self.window.SizeToContent = System.Windows.SizeToContent.Height

    # ── Run logic ─────────────────────────────────────────────────────────
    def _on_run(self, sender, e):
        if self.selected_link_doc is None:
            self._show_status(
                "⚠  No linked model selected",
                "Pick a linked model from the list above first.",
                success=False)
            return

        categories = [bic for chk, name, bic in self._cat_checks if chk.IsChecked]
        if not categories:
            self._show_status(
                "⚠  No categories ticked",
                "Tick at least one MEP category (Ducts, Pipes, Cable "
                "Trays, Conduits) to split.",
                success=False)
            return

        view_only = bool(self._chk_view_only.IsChecked)
        try:
            element_ids = collect_target_element_ids(categories, view_only)
        except Exception:
            self._show_status(
                "⚠  Active view does not support this scope",
                "Switch to a plan/section/3D view, or untick 'Active "
                "view only'.",
                success=False)
            return

        if not element_ids:
            self._show_status(
                "⚠  No elements found",
                "No elements of the ticked categories were found in scope.",
                success=False)
            return

        link_transform = self.selected_link.GetTotalTransform()
        try:
            wall_entries = collect_wall_entries(self.selected_link_doc, link_transform)
        except Exception:
            wall_entries = []

        if not wall_entries:
            self._show_status(
                "⚠  No walls found",
                "The selected linked model has no walls to split against.",
                success=False)
            return

        report = {
            'split_elems': 0, 'split_cuts': 0, 'no_location': 0,
            'curved': 0, 'no_crossing': 0, 'unsupported': 0, 'errors': 0,
        }

        # Phase 1 is read-only, phase 2 does every edit. Keeping them apart
        # means no geometry is ever queried from a document that is midway
        # through being modified, and no element reference outlives an edit.
        plan = plan_breaks(element_ids, wall_entries,
                           self.selected_link_doc, link_transform, report)

        if bool(self._chk_preview.IsChecked):
            crossings = sum(len(points) for _, points in plan)
            detail = ["• {0} run(s) scanned".format(len(element_ids)),
                      "• {0} wall(s) in the link".format(len(wall_entries)),
                      "• {0} run(s) would be cut, at {1} crossing(s)".format(len(plan), crossings)]
            if report['curved'] > 0:
                detail.append("• {0} run(s) skipped — curved (non-straight) section".format(report['curved']))
            if report['errors'] > 0:
                detail.append("• {0} error(s) during the scan".format(report['errors']))
            self._show_status("Preview — nothing was changed",
                              "\n".join(detail), success=True)
            return

        if plan:
            with Transaction(doc, "DQT - Split MEP at Linked Wall") as t:
                t.Start()
                apply_breaks(plan, report)
                t.Commit()

        success = report['split_elems'] > 0
        if success:
            title = "✅  Completed — {0} run(s) split into {1} cut(s)".format(
                report['split_elems'], report['split_cuts'])
        else:
            title = "⚠  Nothing was split"

        detail_lines = []
        if report['no_crossing'] > 0:
            detail_lines.append("• {0} run(s) don't cross any wall in the link".format(report['no_crossing']))
        if report['no_location'] > 0:
            detail_lines.append("• {0} element(s) skipped — no straight-line location curve".format(report['no_location']))
        if report['curved'] > 0:
            detail_lines.append("• {0} run(s) skipped — curved (non-straight) section".format(report['curved']))
        if report['unsupported'] > 0:
            detail_lines.append("• {0} run(s) skipped — this Revit version can't split their category".format(report['unsupported']))
        if report['errors'] > 0:
            detail_lines.append("• {0} error(s) encountered while splitting".format(report['errors']))
        if not detail_lines:
            detail_lines.append("All crossing runs were split successfully.")

        self._show_status(title, "\n".join(detail_lines), success=success)

    def _on_close(self, sender, e):
        self.window.Close()

    def _on_help(self, sender, e):
        if _open_help_page("mep_split.html"):
            return
        MessageBox.Show(
            "Splits duct/pipe/cable tray/conduit runs in this document "
            "wherever they physically cross a wall in a selected linked "
            "model - each run is broken into two elements at the point "
            "where its centerline crosses the wall's solid geometry.\n\n"
            "- Pick the linked model that holds the walls.\n"
            "- Tick the categories to check: Ducts, Pipes, Cable Trays, "
            "Conduits.\n"
            "- Split at Wall scans every run of the ticked categories "
            "(whole document, or the active view only) and breaks each "
            "one at every wall it crosses.\n"
            "- The status panel reports how many runs were split, and "
            "why any were skipped (no crossing, curved section, or an "
            "unsupported category on this Revit version).",
            "Split MEP at Linked Wall - DQT", MessageBoxButton.OK, MessageBoxImage.Information)

    def show(self):
        self.window.ShowDialog()


# ═══════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    dlg = MEPSplitDialog()
    dlg.show()
