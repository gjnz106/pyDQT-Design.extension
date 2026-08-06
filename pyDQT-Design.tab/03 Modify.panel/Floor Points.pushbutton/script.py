# -*- coding: utf-8 -*-
"""
Floor Points v1.0 - DQT
Bulk-adjust the slab shape points ("Modify Sub Elements") of one or more floors:
offset every point by a delta, or flatten them onto a target elevation.

Copyright (c) 2026 Dang Quoc Truong (DQT)
All rights reserved.
"""

__title__ = "Floor\nPoints"
__author__ = "Dang Quoc Truong (DQT)"
__doc__ = ("Move the shape-edited points of selected floors all at once - "
           "offset them by a value (e.g. -2000 mm) or set them to one elevation.")

# ==============================================================================
# IMPORTS - aliased Revit DB import so WPF's Grid is not overwritten
# ==============================================================================
import clr

clr.AddReference('RevitAPI')
clr.AddReference('RevitAPIUI')
clr.AddReference('PresentationCore')
clr.AddReference('PresentationFramework')
clr.AddReference('WindowsBase')

from System.IO import MemoryStream
from System.Text import Encoding
from System.Windows.Markup import XamlReader

import Autodesk.Revit.DB as DB
from Autodesk.Revit.DB import (
    Transaction, TransactionGroup, BuiltInParameter, UnitUtils
)
from Autodesk.Revit.UI import TaskDialog
from Autodesk.Revit.UI.Selection import ObjectType, ISelectionFilter

doc = __revit__.ActiveUIDocument.Document
uidoc = __revit__.ActiveUIDocument

# ==============================================================================
# CONSTANTS
# ==============================================================================
MODE_OFFSET = "offset"
MODE_SET = "set"

PTS_ALL = "all"
PTS_INTERIOR = "interior"
PTS_BOUNDARY = "boundary"

MM_PER_FT = 304.8
XY_TOL = 1e-4      # ~0.03 mm - vertex XY identity
Z_TOL = 1e-6       # ~0.0003 mm - "did this point actually move"

try:
    from Autodesk.Revit.DB import UnitTypeId
    HAS_UNIT_TYPE_ID = True
except Exception:
    HAS_UNIT_TYPE_ID = False


# ==============================================================================
# UNITS
# ==============================================================================
def ft_to_mm(value):
    """Revit internal feet -> millimetres."""
    try:
        if HAS_UNIT_TYPE_ID:
            return UnitUtils.ConvertFromInternalUnits(value, DB.UnitTypeId.Millimeters)
        from Autodesk.Revit.DB import DisplayUnitType
        return UnitUtils.ConvertFromInternalUnits(value, DisplayUnitType.DUT_MILLIMETERS)
    except Exception:
        return value * MM_PER_FT


def mm_to_ft(value):
    """Millimetres -> Revit internal feet."""
    try:
        if HAS_UNIT_TYPE_ID:
            return UnitUtils.ConvertToInternalUnits(value, DB.UnitTypeId.Millimeters)
        from Autodesk.Revit.DB import DisplayUnitType
        return UnitUtils.ConvertToInternalUnits(value, DisplayUnitType.DUT_MILLIMETERS)
    except Exception:
        return value / MM_PER_FT


def mm_text(value_ft):
    """Format an internal-units length as a millimetre string for the UI."""
    return str(int(round(ft_to_mm(value_ft))))


# ==============================================================================
# SLAB SHAPE ACCESS
# ==============================================================================
def get_slab_shape_editor(floor):
    """Floor.SlabShapeEditor was a property up to Revit 2023; from 2024 it is
    the method GetSlabShapeEditor(). Try the method first, fall back to the
    property so the tool works across versions."""
    try:
        sse = floor.GetSlabShapeEditor()
        if sse is not None:
            return sse
    except Exception:
        pass
    try:
        return floor.SlabShapeEditor
    except Exception:
        return None


def vertex_kind(vertex):
    """'Interior' for points added inside the slab, 'Boundary' for the corner
    and edge points that sit on the floor's sketch outline."""
    try:
        name = str(vertex.VertexType)
    except Exception:
        return PTS_BOUNDARY
    if name and "Interior" in name:
        return PTS_INTERIOR
    return PTS_BOUNDARY


def read_points(floor):
    """Snapshot of a floor's shape points, read-only (no transaction needed).

    Returns a list of {'x', 'y', 'z', 'kind'} in vertex order, where z is the
    absolute project elevation of the point. A floor that has never been shape
    edited has a disabled editor and returns an empty list."""
    sse = get_slab_shape_editor(floor)
    if sse is None:
        return []
    try:
        if not sse.IsEnabled:
            return []
    except Exception:
        pass

    points = []
    try:
        for vertex in sse.SlabShapeVertices:
            position = vertex.Position
            points.append({
                'x': position.X,
                'y': position.Y,
                'z': position.Z,
                'kind': vertex_kind(vertex),
            })
    except Exception:
        return []
    return points


def point_selected(point, point_filter):
    if point_filter == PTS_ALL:
        return True
    return point['kind'] == point_filter


# ==============================================================================
# ELEVATION REFERENCE
# ==============================================================================
def probe_zero_reference(floor, sse, points):
    """Absolute Z that ModifySubElement() treats as elevation 0.

    The second argument of ModifySubElement is measured from a reference plane
    Revit derives from the floor, and which plane that is has not been stated
    consistently across API versions (level elevation, with or without the
    floor's height offset). Rather than guess, park the first point at 0,
    regenerate, and read back where it landed - that Z *is* the reference, by
    definition. The probe only changes the point's Z, so it stays findable by
    XY on the way back.

    The probed point is left displaced; the caller must write an explicit
    elevation back to it. Returns (ref_z, sse, matched_vertices, disturbed)
    where a None ref_z means the probe could not be completed - and if
    `disturbed` is also True the floor has been left modified and the caller
    must roll it back."""
    disturbed = False
    try:
        live = match_vertices(points, list(sse.SlabShapeVertices))
        if live is None or live[0] is None:
            return None, sse, None, False

        sse.ModifySubElement(live[0], 0.0)
        disturbed = True
        doc.Regenerate()

        live_sse = get_slab_shape_editor(floor)
        if live_sse is None:
            return None, sse, None, disturbed

        live = match_vertices(points, list(live_sse.SlabShapeVertices))
        if live is None or live[0] is None:
            return None, live_sse, None, disturbed

        return live[0].Position.Z, live_sse, live, disturbed
    except Exception:
        return None, sse, None, disturbed


def assumed_reference(floor):
    """Fallback reference plane: the floor's level plus its height offset.
    Only used when the probe could not run at all."""
    reference = 0.0
    try:
        level = doc.GetElement(floor.LevelId)
        if level is not None:
            reference = level.ProjectElevation
    except Exception:
        pass
    try:
        param = floor.get_Parameter(BuiltInParameter.FLOOR_HEIGHTABOVELEVEL_PARAM)
        if param is not None:
            reference += param.AsDouble()
    except Exception:
        pass
    return reference


def match_vertices(points, vertices):
    """Pair each snapshot point with its live vertex.

    Regenerating does not change the slab's topology, so the order is expected
    to hold - verify that index-for-index before trusting it, and fall back to
    a nearest-XY search if it does not. The probed vertex only moved in Z, so
    XY matching stays valid either way."""
    if vertices is None or len(vertices) != len(points):
        return None

    aligned = True
    for point, vertex in zip(points, vertices):
        position = vertex.Position
        if abs(position.X - point['x']) > XY_TOL or abs(position.Y - point['y']) > XY_TOL:
            aligned = False
            break
    if aligned:
        return list(vertices)

    matched = []
    for point in points:
        best = None
        best_distance = 1e-3
        for vertex in vertices:
            position = vertex.Position
            distance = ((position.X - point['x']) ** 2 +
                        (position.Y - point['y']) ** 2) ** 0.5
            if distance < best_distance:
                best_distance = distance
                best = vertex
        matched.append(best)
    return matched


# ==============================================================================
# PREVIEW / APPLY MATHS  (absolute elevations, so no reference plane involved)
# ==============================================================================
def target_elevation(point, mode, value_ft):
    """New absolute elevation for a point that the filter selected."""
    if mode == MODE_OFFSET:
        return point['z'] + value_ft
    return value_ft


def preview_result(points, mode, value_ft, point_filter):
    """Exact before/after summary, computed from the snapshot alone.

    Returns (selected_count, moved_count, new_min_ft, new_max_ft) over every
    point of the floor - selected points at their new elevation, the rest
    unchanged. Returns None when there is nothing to report."""
    if not points:
        return None

    selected = 0
    moved = 0
    lowest = None
    highest = None

    for point in points:
        if point_selected(point, point_filter):
            selected += 1
            elevation = target_elevation(point, mode, value_ft)
            if abs(elevation - point['z']) > Z_TOL:
                moved += 1
        else:
            elevation = point['z']

        if lowest is None or elevation < lowest:
            lowest = elevation
        if highest is None or elevation > highest:
            highest = elevation

    return selected, moved, lowest, highest


def apply_to_floor(floor, points, mode, value_ft, point_filter):
    """Write the new elevations onto one floor. Must run inside a transaction.

    Raises on any condition that would leave the floor in a state we cannot
    account for, so the caller can roll that floor back untouched."""
    sse = get_slab_shape_editor(floor)
    if sse is None:
        raise Exception("no slab shape editor on this floor")

    ref_z, sse, live, disturbed = probe_zero_reference(floor, sse, points)

    if ref_z is None:
        if disturbed:
            # A point was parked at elevation 0 and we cannot work out where
            # that is - the only safe move is to let the caller roll back.
            raise Exception("could not read back the elevation reference")
        ref_z = assumed_reference(floor)
        sse = get_slab_shape_editor(floor)
        try:
            live = match_vertices(points, list(sse.SlabShapeVertices))
        except Exception:
            raise Exception("could not read the floor's points")

    if live is None:
        raise Exception("the floor's points changed while reading them")

    moved = 0
    for index, point in enumerate(points):
        vertex = live[index]
        if vertex is None:
            continue

        if point_selected(point, point_filter):
            elevation = target_elevation(point, mode, value_ft)
        elif disturbed and index == 0:
            elevation = point['z']          # undo the calibration probe
        else:
            continue

        sse.ModifySubElement(vertex, elevation - ref_z)
        if abs(elevation - point['z']) > Z_TOL:
            moved += 1

    return moved


# ==============================================================================
# SELECTION
# ==============================================================================
class FloorSelectionFilter(ISelectionFilter):
    def AllowElement(self, element):
        try:
            return isinstance(element, DB.Floor)
        except Exception:
            return False

    def AllowReference(self, reference, position):
        return False


def get_target_floors():
    """Pre-selected floors if there are any, otherwise ask the user to pick."""
    floors = []
    try:
        for element_id in uidoc.Selection.GetElementIds():
            element = doc.GetElement(element_id)
            if isinstance(element, DB.Floor):
                floors.append(element)
    except Exception:
        pass

    if floors:
        return floors

    try:
        references = uidoc.Selection.PickObjects(
            ObjectType.Element, FloorSelectionFilter(),
            "Select the floors whose points you want to move, then Finish")
        for reference in references:
            element = doc.GetElement(reference.ElementId)
            if isinstance(element, DB.Floor):
                floors.append(element)
    except Exception:
        return []       # user pressed Escape
    return floors


def floor_label(floor):
    try:
        floor_type = doc.GetElement(floor.GetTypeId())
        name = floor_type.get_Parameter(
            BuiltInParameter.SYMBOL_NAME_PARAM).AsString()
        if name:
            return name
    except Exception:
        pass
    return "Floor"


# ==============================================================================
# XAML UI
# ==============================================================================
XAML_MAIN = """
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="Floor Points v1.0 - DQT"
        Width="520" Height="640"
        WindowStartupLocation="CenterScreen"
        ResizeMode="NoResize"
        Background="#FFFFFF">

  <Window.Resources>
    <Style x:Key="DqtButton" TargetType="Button">
      <Setter Property="Background" Value="#F0CC88"/>
      <Setter Property="Foreground" Value="#5D4E37"/>
      <Setter Property="BorderBrush" Value="#D4B87A"/>
      <Setter Property="BorderThickness" Value="1"/>
      <Setter Property="Padding" Value="18,7"/>
      <Setter Property="Margin" Value="4,0,0,0"/>
      <Setter Property="FontWeight" Value="SemiBold"/>
      <Setter Property="Cursor" Value="Hand"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="Button">
            <Border x:Name="bd" CornerRadius="4"
                    Background="{TemplateBinding Background}"
                    BorderBrush="{TemplateBinding BorderBrush}"
                    BorderThickness="{TemplateBinding BorderThickness}">
              <ContentPresenter HorizontalAlignment="Center"
                                VerticalAlignment="Center"/>
            </Border>
            <ControlTemplate.Triggers>
              <Trigger Property="IsMouseOver" Value="True">
                <Setter TargetName="bd" Property="Background" Value="#E4D2A8"/>
              </Trigger>
              <Trigger Property="IsPressed" Value="True">
                <Setter TargetName="bd" Property="Background" Value="#D4B87A"/>
              </Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>

    <Style x:Key="SectionLabel" TargetType="TextBlock">
      <Setter Property="Foreground" Value="#5D4E37"/>
      <Setter Property="FontWeight" Value="SemiBold"/>
      <Setter Property="Margin" Value="0,14,0,4"/>
    </Style>

    <Style x:Key="DqtRadio" TargetType="RadioButton">
      <Setter Property="Foreground" Value="#333333"/>
      <Setter Property="Margin" Value="2,3,0,3"/>
      <Setter Property="Cursor" Value="Hand"/>
    </Style>
  </Window.Resources>

  <Grid>
    <Grid.RowDefinitions>
      <RowDefinition Height="Auto"/>
      <RowDefinition Height="*"/>
      <RowDefinition Height="Auto"/>
    </Grid.RowDefinitions>

    <!-- Header -->
    <Border Grid.Row="0" Background="#F0CC88"
            BorderBrush="#D4B87A" BorderThickness="0,0,0,2" Padding="16,12">
      <StackPanel>
        <TextBlock Text="Floor Points" Foreground="#5D4E37"
                   FontSize="18" FontWeight="Bold"/>
        <TextBlock Text="Move shape-edited floor points all at once"
                   Foreground="#5D4E37" FontSize="11" Margin="0,2,0,0"/>
      </StackPanel>
    </Border>

    <!-- Body -->
    <Border Grid.Row="1" Padding="16,10,16,10">
      <StackPanel>

        <Border Background="#FAF3E0" BorderBrush="#D4B87A" BorderThickness="1"
                CornerRadius="4" Padding="10,8">
          <StackPanel>
            <TextBlock x:Name="SummaryText" Foreground="#5D4E37"
                       FontWeight="SemiBold" TextWrapping="Wrap"/>
            <TextBlock x:Name="RangeText" Foreground="#333333" FontSize="11"
                       Margin="0,3,0,0" TextWrapping="Wrap"/>
          </StackPanel>
        </Border>

        <TextBlock Text="Points to move" Style="{StaticResource SectionLabel}"/>
        <StackPanel>
          <RadioButton x:Name="RbAll" GroupName="Pts" IsChecked="True"
                       Style="{StaticResource DqtRadio}"
                       Content="All points"/>
          <RadioButton x:Name="RbInterior" GroupName="Pts"
                       Style="{StaticResource DqtRadio}"
                       Content="Interior points only (added inside the slab)"/>
          <RadioButton x:Name="RbBoundary" GroupName="Pts"
                       Style="{StaticResource DqtRadio}"
                       Content="Boundary points only (on the sketch outline)"/>
        </StackPanel>

        <TextBlock Text="What to do" Style="{StaticResource SectionLabel}"/>
        <StackPanel>
          <RadioButton x:Name="RbOffset" GroupName="Mode" IsChecked="True"
                       Style="{StaticResource DqtRadio}"
                       Content="Offset - move each point by a value"/>
          <RadioButton x:Name="RbSet" GroupName="Mode"
                       Style="{StaticResource DqtRadio}"
                       Content="Set - put every point on one elevation"/>
        </StackPanel>

        <TextBlock Text="Value (mm)" Style="{StaticResource SectionLabel}"/>
        <TextBox x:Name="TxtValue" Text="-2000" Height="28" FontSize="13"
                 Padding="6,4" VerticalContentAlignment="Center"
                 Foreground="#333333" BorderBrush="#D4B87A" BorderThickness="1"/>
        <TextBlock x:Name="HintText" Foreground="#777777" FontSize="11"
                   Margin="0,4,0,0" TextWrapping="Wrap"/>

        <Border x:Name="PreviewBorder" Background="#FFFFFF"
                BorderBrush="#E0E0E0" BorderThickness="1"
                CornerRadius="4" Padding="10,8" Margin="0,14,0,0">
          <StackPanel>
            <TextBlock Text="Result" Foreground="#5D4E37"
                       FontWeight="SemiBold" FontSize="11"/>
            <TextBlock x:Name="PreviewText" Foreground="#333333" FontSize="11"
                       Margin="0,3,0,0" TextWrapping="Wrap"/>
          </StackPanel>
        </Border>

        <StackPanel Orientation="Horizontal" HorizontalAlignment="Right"
                    Margin="0,16,0,0">
          <Button x:Name="BtnCancel" Content="Cancel"
                  Style="{StaticResource DqtButton}"/>
          <Button x:Name="BtnApply" Content="Apply"
                  Style="{StaticResource DqtButton}"/>
        </StackPanel>

      </StackPanel>
    </Border>

    <!-- Footer -->
    <Border Grid.Row="2" Background="#FFFFFF"
            BorderBrush="#E0E0E0" BorderThickness="0,1,0,0" Padding="12,6">
      <TextBlock Text="Dang Quoc Truong - DQT (c) 2026"
                 Foreground="#5D4E37" FontSize="11"
                 HorizontalAlignment="Right"/>
    </Border>
  </Grid>
</Window>
"""


class FloorPointsDialog(object):
    """Themed settings window. No element picking happens while it is open -
    the floors are collected before it is shown."""

    def __init__(self, floor_data):
        self.floor_data = floor_data        # [(floor, points), ...]
        self.confirmed = False
        self.mode = MODE_OFFSET
        self.point_filter = PTS_ALL
        self.value_ft = mm_to_ft(-2000.0)

        stream = MemoryStream(Encoding.UTF8.GetBytes(XAML_MAIN))
        self.window = XamlReader.Load(stream)

        self.summary_text = self.window.FindName("SummaryText")
        self.range_text = self.window.FindName("RangeText")
        self.rb_all = self.window.FindName("RbAll")
        self.rb_interior = self.window.FindName("RbInterior")
        self.rb_boundary = self.window.FindName("RbBoundary")
        self.rb_offset = self.window.FindName("RbOffset")
        self.rb_set = self.window.FindName("RbSet")
        self.txt_value = self.window.FindName("TxtValue")
        self.hint_text = self.window.FindName("HintText")
        self.preview_text = self.window.FindName("PreviewText")
        self.btn_apply = self.window.FindName("BtnApply")
        self.btn_cancel = self.window.FindName("BtnCancel")

        self._fill_summary()

        self.rb_all.Checked += self._on_options_changed
        self.rb_interior.Checked += self._on_options_changed
        self.rb_boundary.Checked += self._on_options_changed
        self.rb_offset.Checked += self._on_options_changed
        self.rb_set.Checked += self._on_options_changed
        self.txt_value.TextChanged += self._on_options_changed
        self.btn_apply.Click += self._on_apply
        self.btn_cancel.Click += self._on_cancel

        self._refresh()

    # -- helpers ------------------------------------------------------------
    def _all_points(self):
        points = []
        for _, floor_points in self.floor_data:
            points.extend(floor_points)
        return points

    def _fill_summary(self):
        points = self._all_points()
        interior = sum(1 for p in points if p['kind'] == PTS_INTERIOR)
        self.summary_text.Text = "{0} floor(s), {1} point(s) - {2} interior, {3} boundary".format(
            len(self.floor_data), len(points), interior, len(points) - interior)

        if points:
            lowest = min(p['z'] for p in points)
            highest = max(p['z'] for p in points)
            self.range_text.Text = "Current elevation: {0} mm to {1} mm".format(
                mm_text(lowest), mm_text(highest))
        else:
            self.range_text.Text = ""

    def _read_options(self):
        """Pull the current UI state. Returns False if the value box does not
        hold a number."""
        if self.rb_interior.IsChecked:
            self.point_filter = PTS_INTERIOR
        elif self.rb_boundary.IsChecked:
            self.point_filter = PTS_BOUNDARY
        else:
            self.point_filter = PTS_ALL

        self.mode = MODE_SET if self.rb_set.IsChecked else MODE_OFFSET

        try:
            self.value_ft = mm_to_ft(float(self.txt_value.Text.strip()))
            return True
        except Exception:
            return False

    def _refresh(self):
        valid = self._read_options()

        if self.mode == MODE_OFFSET:
            self.hint_text.Text = ("How far each point moves. Negative goes down - "
                                   "e.g. -2000 lowers everything by 2 m.")
        else:
            self.hint_text.Text = ("The project elevation every selected point is "
                                   "moved to, flattening them.")

        if not valid:
            self.preview_text.Text = "Enter a number in millimetres."
            self.btn_apply.IsEnabled = False
            return

        result = preview_result(self._all_points(), self.mode,
                                self.value_ft, self.point_filter)
        if result is None:
            self.preview_text.Text = "Nothing to move."
            self.btn_apply.IsEnabled = False
            return

        selected, moved, lowest, highest = result
        self.preview_text.Text = (
            "{0} point(s) selected, {1} will actually move.\n"
            "Elevation afterwards: {2} mm to {3} mm.".format(
                selected, moved, mm_text(lowest), mm_text(highest)))
        self.btn_apply.IsEnabled = moved > 0

    # -- events -------------------------------------------------------------
    def _on_options_changed(self, sender, args):
        try:
            self._refresh()
        except Exception:
            pass

    def _on_apply(self, sender, args):
        if not self._read_options():
            return
        self.confirmed = True
        self.window.Close()

    def _on_cancel(self, sender, args):
        self.confirmed = False
        self.window.Close()

    def show(self):
        self.window.ShowDialog()
        return self.confirmed


# ==============================================================================
# MAIN
# ==============================================================================
def run():
    floors = get_target_floors()
    if not floors:
        return

    floor_data = []
    flat = 0
    for floor in floors:
        points = read_points(floor)
        if points:
            floor_data.append((floor, points))
        else:
            flat += 1

    if not floor_data:
        TaskDialog.Show(
            "Floor Points",
            "None of the {0} selected floor(s) have shape points.\n\n"
            "Add points with Modify > Shape Editing > Add Point first, or use "
            "the floor's Height Offset From Level to move a flat slab.".format(len(floors)))
        return

    dialog = FloorPointsDialog(floor_data)
    if not dialog.show():
        return

    mode = dialog.mode
    value_ft = dialog.value_ft
    point_filter = dialog.point_filter

    moved_total = 0
    ok = 0
    failures = []

    group = TransactionGroup(doc, "DQT - Floor Point Elevation")
    group.Start()
    try:
        for floor, points in floor_data:
            transaction = Transaction(doc, "DQT - Floor Point Elevation")
            transaction.Start()
            try:
                moved = apply_to_floor(floor, points, mode, value_ft, point_filter)
                transaction.Commit()
                moved_total += moved
                ok += 1
            except Exception as error:
                transaction.RollBack()
                failures.append("{0} (id {1}): {2}".format(
                    floor_label(floor), floor.Id, str(error)))
        group.Assimilate()
    except Exception as error:
        group.RollBack()
        TaskDialog.Show("Floor Points", "Error: {0}".format(str(error)))
        return

    message = "{0} point(s) moved on {1} floor(s).".format(moved_total, ok)
    if flat:
        message += "\n\n{0} selected floor(s) had no shape points and were skipped.".format(flat)
    if failures:
        message += "\n\nNot changed ({0}):\n".format(len(failures)) + "\n".join(failures[:10])
        if len(failures) > 10:
            message += "\n..."
    TaskDialog.Show("Floor Points", message)


run()
