# -*- coding: utf-8 -*-
"""
Property Line from Excel v1.3 - DQT
Draws a closed loop of Model Lines - a boundary - from Easting/Northing
(or plain X/Y) coordinate tables found in an .xlsx file. A header shared
by several boundaries listed back-to-back with no separating row (e.g.
WL1..WL8 immediately followed by LB1..LB30) is split one boundary per
label prefix, rather than looped through as a single bogus shape.

Revit's own PropertyLine element cannot be created through the public
API (Document.Create has no NewPropertyLine method, and PropertyLine has
no Create method either, on any Revit version this suite supports) - the
only officially supported way to get a real Property Line is Revit's own
Property Lines > Edit Table dialog. This tool draws the boundary as plain
Model Lines instead, which is real, supported API and is enough to see
and check the shape; promoting it into a true Property Line still needs
that native dialog.

Copyright (c) 2026 Dang Quoc Truong (DQT)
All rights reserved.
"""

__title__ = "Property\nLine"
__author__ = "Dang Quoc Truong (DQT)"
__doc__ = ("Draw a closed Model Line boundary from Easting/Northing points in "
           "an Excel file - pick the .xlsx, tick the coordinate table(s) "
           "found, and draw.")

# ==============================================================================
# IMPORTS - aliased Revit DB import so WPF's Grid is not overwritten
# ==============================================================================
import re
import zipfile
import xml.etree.ElementTree as ET

import clr

clr.AddReference('RevitAPI')
clr.AddReference('RevitAPIUI')
clr.AddReference('PresentationCore')
clr.AddReference('PresentationFramework')
clr.AddReference('WindowsBase')
clr.AddReference('System.Windows.Forms')

from System.IO import MemoryStream
from System.Text import Encoding
from System.Windows.Markup import XamlReader
from System.Windows.Forms import OpenFileDialog, DialogResult
from System.Windows.Controls import CheckBox, ComboBoxItem

import Autodesk.Revit.DB as DB
from Autodesk.Revit.DB import Transaction, TransactionGroup, UnitUtils
from Autodesk.Revit.UI import TaskDialog

doc = __revit__.ActiveUIDocument.Document
uidoc = __revit__.ActiveUIDocument

try:
    from Autodesk.Revit.DB import UnitTypeId
    HAS_UNIT_TYPE_ID = True
except Exception:
    HAS_UNIT_TYPE_ID = False

# ==============================================================================
# CONSTANTS
# ==============================================================================
MM_PER_FT = 304.8
M_PER_FT = 0.3048

UNIT_M = "m"
UNIT_MM = "mm"
UNIT_FT = "ft"

COORD_SHARED = "shared"
COORD_INTERNAL = "internal"

DUP_TOL_MM = 1.0            # points closer than this are treated as one point
TITLE_LOOKUP_ROWS = 4        # how many rows above a header to search for a title
MIN_TITLE_LEN = 6


# ==============================================================================
# UNITS
# ==============================================================================
def to_internal(value, unit):
    """A raw Excel number, in `unit`, to Revit internal feet."""
    try:
        if HAS_UNIT_TYPE_ID:
            uid = {UNIT_M: DB.UnitTypeId.Meters,
                   UNIT_MM: DB.UnitTypeId.Millimeters,
                   UNIT_FT: DB.UnitTypeId.Feet}[unit]
            return UnitUtils.ConvertToInternalUnits(value, uid)
    except Exception:
        pass
    factor = {UNIT_M: 1.0 / M_PER_FT, UNIT_MM: 1.0 / MM_PER_FT, UNIT_FT: 1.0}[unit]
    return value * factor


def ft_to_mm(value):
    try:
        if HAS_UNIT_TYPE_ID:
            return UnitUtils.ConvertFromInternalUnits(value, DB.UnitTypeId.Millimeters)
    except Exception:
        pass
    return value * MM_PER_FT


# ==============================================================================
# XLSX READING - plain zipfile + ElementTree, no CPython-only dependency
# (openpyxl cannot be imported under IronPython 2.7 - see PurgeFamilies /
# ScheduleExportImportPro in this suite for the same constraint)
# ==============================================================================
NS_MAIN = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
NS_REL = 'http://schemas.openxmlformats.org/package/2006/relationships'
NS_REL_DOC = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'


def _col_num(col_letters):
    n = 0
    for c in col_letters:
        n = n * 26 + (ord(c.upper()) - ord('A') + 1)
    return n


def _cell_ref_parts(ref):
    m = re.match(r'([A-Za-z]+)(\d+)', ref or "")
    if not m:
        return None, None
    return _col_num(m.group(1)), int(m.group(2))


def _read_sheet_grid(xml_bytes, shared_strings):
    """{row_number: {col_number: text_value}}, 1-based, blanks omitted."""
    root = ET.fromstring(xml_bytes)
    grid = {}
    for row_el in root.findall('.//{%s}row' % NS_MAIN):
        for cell_el in row_el.findall('{%s}c' % NS_MAIN):
            col, row = _cell_ref_parts(cell_el.get('r'))
            if col is None:
                continue
            cell_type = cell_el.get('t', '')
            value = ""
            if cell_type == 's':
                v_el = cell_el.find('{%s}v' % NS_MAIN)
                if v_el is not None and v_el.text:
                    idx = int(v_el.text)
                    if 0 <= idx < len(shared_strings):
                        value = shared_strings[idx]
            elif cell_type == 'inlineStr':
                is_el = cell_el.find('{%s}is' % NS_MAIN)
                if is_el is not None:
                    value = "".join(t.text or "" for t in
                                     is_el.findall('.//{%s}t' % NS_MAIN))
            else:
                v_el = cell_el.find('{%s}v' % NS_MAIN)
                if v_el is not None and v_el.text:
                    value = v_el.text
            if value != "":
                grid.setdefault(row, {})[col] = value
    return grid


def read_workbook(filepath):
    """[(sheet_name, grid), ...] for every sheet in the .xlsx."""
    sheets = []
    with zipfile.ZipFile(filepath, 'r') as zf:
        names = set(zf.namelist())

        shared_strings = []
        if 'xl/sharedStrings.xml' in names:
            root = ET.fromstring(zf.read('xl/sharedStrings.xml'))
            for si in root.findall('{%s}si' % NS_MAIN):
                shared_strings.append(
                    "".join(t.text or "" for t in si.findall('.//{%s}t' % NS_MAIN)))

        rels = {}
        if 'xl/_rels/workbook.xml.rels' in names:
            root = ET.fromstring(zf.read('xl/_rels/workbook.xml.rels'))
            for rel in root.findall('{%s}Relationship' % NS_REL):
                rels[rel.get('Id')] = rel.get('Target')

        wb_root = ET.fromstring(zf.read('xl/workbook.xml'))
        sheet_defs = []
        for sh in wb_root.findall('.//{%s}sheet' % NS_MAIN):
            sheet_defs.append((sh.get('name'), sh.get('{%s}id' % NS_REL_DOC)))

        for idx, (name, rid) in enumerate(sheet_defs):
            target = rels.get(rid)
            path = ('xl/' + target.lstrip('/')) if target else None
            if not path or path not in names:
                path = 'xl/worksheets/sheet{0}.xml'.format(idx + 1)
            if path not in names:
                continue
            sheets.append((name, _read_sheet_grid(zf.read(path), shared_strings)))
    return sheets


# ==============================================================================
# TABLE DETECTION
# ==============================================================================
def _num(text):
    if text is None:
        return None
    t = text.strip().replace(',', '')
    if not t:
        return None
    try:
        return float(t)
    except Exception:
        return None


def _is_easting_header(text):
    if not text:
        return False
    u = text.upper()
    return ('EAST' in u) or (re.search(r'(?:^|[^A-Z])X(?:[^A-Z]|$)', u) is not None)


def _is_northing_header(text):
    if not text:
        return False
    u = text.upper()
    return ('NORTH' in u) or (re.search(r'(?:^|[^A-Z])Y(?:[^A-Z]|$)', u) is not None)


def split_label(label):
    """'WL12' -> ('WL', 12); a label with no trailing-number suffix -> (None, None)."""
    m = re.match(r'^([A-Za-z][A-Za-z_\-]*?)(\d+)$', label or "")
    if not m:
        return None, None
    return m.group(1), int(m.group(2))


def split_into_boundaries(points):
    """A single header is sometimes shared by more than one boundary stacked
    back-to-back with no separating row - e.g. WL1..WL8 (the working limit)
    immediately followed by LB1..LB30 (the lease boundary), both listed
    under one "Indicative Coordinates" title. Looping straight through such
    a list draws one bogus loop through two unrelated boundaries.

    Split wherever the point label's letter prefix changes (WL -> LB, LHJ ->
    PRJ, ...) so each run of same-prefix labels becomes its own boundary.
    Falls back to a single group - the previous behaviour - the moment any
    label does not end in a plain letters+number pattern, since the split
    signal does not apply to freeform names."""
    groups = []
    current = []
    current_prefix = None
    for label, x, y in points:
        prefix, _num = split_label(label)
        if prefix is None:
            return [points]
        if current and prefix != current_prefix:
            groups.append(current)
            current = []
        current.append((label, x, y))
        current_prefix = prefix
    if current:
        groups.append(current)
    return groups


def find_tables(sheet_name, grid):
    """Every coordinate table in one sheet - one entry per boundary, which is
    not always one entry per header (see split_into_boundaries).

    A table is triggered by a row holding both an Easting-like and a
    Northing-like header cell. The Point/Name column is assumed to sit
    immediately to the left of whichever of the two is further left (works
    for a plain one-row header, and for the common layout where the point
    label header sits one row above a merged coordinate-system heading -
    see the sample sheet this tool was built against). A title is any lone
    text cell found in the few rows above the header."""
    tables = []
    for r in sorted(grid.keys()):
        row_cells = grid.get(r, {})
        easting_col = None
        northing_col = None
        for c in sorted(row_cells.keys()):
            text = row_cells[c]
            if easting_col is None and _is_easting_header(text):
                easting_col = c
            elif northing_col is None and _is_northing_header(text):
                northing_col = c
        if easting_col is None or northing_col is None or easting_col == northing_col:
            continue

        label_col = min(easting_col, northing_col) - 1
        if label_col < 1:
            label_col = None

        title = None
        for up in range(1, TITLE_LOOKUP_ROWS + 1):
            trow = r - up
            if trow < 1:
                break
            texts = [v.strip() for v in grid.get(trow, {}).values() if v and v.strip()]
            if len(texts) == 1 and len(texts[0]) >= MIN_TITLE_LEN:
                title = texts[0]
                break
        if not title:
            title = "Sheet '{0}' (row {1})".format(sheet_name, r)

        points = []
        row = r + 1
        while True:
            cells = grid.get(row)
            if not cells:
                break
            x_val = _num(cells.get(easting_col))
            y_val = _num(cells.get(northing_col))
            if x_val is None or y_val is None:
                break
            label = cells.get(label_col) if label_col is not None else None
            if not label or not label.strip():
                label = "P{0}".format(len(points) + 1)
            points.append((label.strip(), x_val, y_val))
            row += 1

        groups = split_into_boundaries(points)
        for group in groups:
            if len(group) < 2:
                continue
            group_title = title
            if len(groups) > 1:
                prefix, _prefix_num = split_label(group[0][0])
                group_title = "{0} [{1}]".format(title, prefix)
            tables.append({
                'sheet': sheet_name,
                'title': group_title,
                'header_row': r,
                'points': group,
            })
    return tables


# ==============================================================================
# COORDINATE TRANSFORM
#
# The internal -> shared map is MEASURED from the model rather than read off a
# transform object. ProjectLocation.GetProjectPosition(p) answers "what does
# Revit's own Report Shared Coordinates say about internal point p", which is
# exactly the number the survey schedule is quoted in. Probing it at three
# points pins the whole map down without assuming which way any API-supplied
# transform runs, or which way ProjectPosition.Angle is measured - the two
# things that are easy to get backwards and that put the boundary in the wrong
# place once the project has acquired coordinates.
#
# The maths stays in plain-float helpers so it can be unit-tested without a
# live Revit document.
# ==============================================================================
def sample_project_position(x_ft, y_ft):
    """Shared (EastWest, NorthSouth) of one internal point, in feet."""
    position = doc.ActiveProjectLocation.GetProjectPosition(DB.XYZ(x_ft, y_ft, 0.0))
    return position.EastWest, position.NorthSouth


def build_shared_to_internal(samples):
    """Invert the internal -> shared map, given its value at three points.

    `samples` is the shared (E, N) of internal (0,0), (1,0) and (0,1). Revit
    maps internal to shared with a rotation about Z plus a translation, so

        shared = origin + x * U + y * V

    where U and V are the shared-space images of the internal unit axes -
    read straight off the second and third probes. Inverting that 2x2 system
    gives the map this tool needs.

    Returns (ew0, ns0, a, b, c, d) with

        x = a * (E - ew0) + b * (N - ns0)
        y = c * (E - ew0) + d * (N - ns0)

    or None if the probes came back degenerate (which should not happen for a
    rotation, and would mean the answer cannot be trusted)."""
    (ew0, ns0), (ew_x, ns_x), (ew_y, ns_y) = samples
    ux, uy = ew_x - ew0, ns_x - ns0
    vx, vy = ew_y - ew0, ns_y - ns0
    det = ux * vy - vx * uy
    if abs(det) < 1e-9:
        return None
    return (ew0, ns0, vy / det, -vx / det, -uy / det, ux / det)


def shared_to_internal(mapping, ew, ns):
    """One shared (Easting, Northing) pair to internal (x, y), in feet."""
    ew0, ns0, a, b, c, d = mapping
    de, dn = ew - ew0, ns - ns0
    return (a * de + b * dn, c * de + d * dn)


def is_identity_mapping(mapping, tol=1e-6):
    """True when shared and internal coordinates are the same thing, i.e. the
    project has not been positioned yet."""
    ew0, ns0, a, b, c, d = mapping
    return (abs(ew0) < tol and abs(ns0) < tol and
            abs(a - 1) < tol and abs(b) < tol and
            abs(c) < tol and abs(d - 1) < tol)


def get_shared_to_internal():
    """None if the project location could not be probed."""
    try:
        samples = (sample_project_position(0.0, 0.0),
                   sample_project_position(1.0, 0.0),
                   sample_project_position(0.0, 1.0))
    except Exception:
        return None
    return build_shared_to_internal(samples)


def roundtrip_error_ft(mapping, ew, ns):
    """Convert a shared point to internal, then ask Revit what the shared
    coordinates of that internal point are, and return how far off it came
    back. Zero means the conversion agrees with Revit's own Report Shared
    Coordinates - the check that would have caught the transform running the
    wrong way. None if it could not be run."""
    try:
        x, y = shared_to_internal(mapping, ew, ns)
        back_ew, back_ns = sample_project_position(x, y)
        return ((back_ew - ew) ** 2 + (back_ns - ns) ** 2) ** 0.5
    except Exception:
        return None


def build_points_ft(table, unit, coord_mode, elevation_ft, mapping):
    """[(label, x_ft, y_ft, z_ft), ...] in Revit internal units, flattened
    onto elevation_ft. Pure math - no DB.XYZ construction here.

    The file's first column is an Easting and the second a Northing, so in
    shared mode they go into the map in that order, not as raw x/y."""
    out = []
    for label, easting_raw, northing_raw in table['points']:
        x_ft = to_internal(easting_raw, unit)
        y_ft = to_internal(northing_raw, unit)
        if coord_mode == COORD_SHARED and mapping is not None:
            x_ft, y_ft = shared_to_internal(mapping, x_ft, y_ft)
        out.append((label, x_ft, y_ft, elevation_ft))
    return out


def dedupe_consecutive(points_ft, closed):
    """Drop a point that lands on top of the previous one (within DUP_TOL_MM),
    and - if closing the loop - drop a last point that lands on the first.
    Returns (kept_points, dropped_count)."""
    tol = to_internal(DUP_TOL_MM, UNIT_MM)
    cleaned = []
    for p in points_ft:
        if cleaned:
            px, py = cleaned[-1][1], cleaned[-1][2]
            if ((p[1] - px) ** 2 + (p[2] - py) ** 2) ** 0.5 < tol:
                continue
        cleaned.append(p)
    if closed and len(cleaned) > 1:
        fx, fy = cleaned[0][1], cleaned[0][2]
        lx, ly = cleaned[-1][1], cleaned[-1][2]
        if ((lx - fx) ** 2 + (ly - fy) ** 2) ** 0.5 < tol:
            cleaned.pop()
    return cleaned, len(points_ft) - len(cleaned)


def segment_pairs(points_ft, closed):
    """[(p1, p2), ...] consecutive pairs, wrapping to the first point if closed."""
    n = len(points_ft)
    count = n if closed else n - 1
    return [(points_ft[i], points_ft[(i + 1) % n]) for i in range(count)]


# ==============================================================================
# GEOMETRY CREATION (must run inside a Transaction)
# ==============================================================================
def make_sketch_plane(elevation_ft):
    origin = DB.XYZ(0.0, 0.0, elevation_ft)
    plane = DB.Plane.CreateByNormalAndOrigin(DB.XYZ.BasisZ, origin)
    return DB.SketchPlane.Create(doc, plane)


def create_model_lines(points_ft, closed, elevation_ft):
    sketch_plane = make_sketch_plane(elevation_ft)
    created = 0
    for p1, p2 in segment_pairs(points_ft, closed):
        xyz1 = DB.XYZ(p1[1], p1[2], p1[3])
        xyz2 = DB.XYZ(p2[1], p2[2], p2[3])
        doc.Create.NewModelCurve(DB.Line.CreateBound(xyz1, xyz2), sketch_plane)
        created += 1
    return created


# ==============================================================================
# FILE PICKING
# ==============================================================================
def pick_excel_file():
    try:
        dialog = OpenFileDialog()
        dialog.Filter = "Excel Files (*.xlsx)|*.xlsx"
        dialog.Title = "Select the coordinate Excel file"
        if dialog.ShowDialog() != DialogResult.OK:
            return None
        return dialog.FileName
    except Exception:
        return None


def level_label(level):
    try:
        return "{0}  (elev {1} mm)".format(level.Name, int(round(ft_to_mm(level.Elevation))))
    except Exception:
        return str(getattr(level, "Name", "Level"))


# ==============================================================================
# XAML UI
# ==============================================================================
XAML_MAIN = """
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="Property Line from Excel - DQT"
        Width="560" Height="720"
        WindowStartupLocation="CenterScreen"
        ResizeMode="NoResize"
        Background="#FFFFFF">

  <Window.Resources>
    <Style x:Key="DqtButton" TargetType="Button">
      <Setter Property="Background" Value="#F0CC88"/>
      <Setter Property="Foreground" Value="#5D4E37"/>
      <Setter Property="BorderBrush" Value="#D4B87A"/>
      <Setter Property="BorderThickness" Value="1"/>
      <Setter Property="Padding" Value="27,11"/>
      <Setter Property="Margin" Value="6,0,0,0"/>
      <Setter Property="FontSize" Value="18"/>
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
      <Setter Property="Margin" Value="0,12,0,4"/>
    </Style>

    <Style x:Key="DqtRadio" TargetType="RadioButton">
      <Setter Property="Foreground" Value="#333333"/>
      <Setter Property="Margin" Value="2,3,0,3"/>
      <Setter Property="Cursor" Value="Hand"/>
    </Style>

    <Style x:Key="DqtCheck" TargetType="CheckBox">
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
        <TextBlock Text="Property Line from Excel" Foreground="#5D4E37"
                   FontSize="18" FontWeight="Bold"/>
        <TextBlock x:Name="FileText" Foreground="#5D4E37" FontSize="11"
                   Margin="0,2,0,0" TextWrapping="Wrap"/>
      </StackPanel>
    </Border>

    <!-- Body -->
    <ScrollViewer Grid.Row="1" VerticalScrollBarVisibility="Auto">
    <Border Padding="16,10,16,10">
      <StackPanel>

        <TextBlock Text="Coordinate table(s) found" Style="{StaticResource SectionLabel}"/>
        <Border Background="#FFFFFF" BorderBrush="#E0E0E0" BorderThickness="1"
                CornerRadius="4" Padding="8" MaxHeight="150">
          <ScrollViewer VerticalScrollBarVisibility="Auto">
            <StackPanel x:Name="TablesPanel"/>
          </ScrollViewer>
        </Border>

        <Border Background="#FDEEEE" BorderBrush="#E0B4B4" BorderThickness="1"
                CornerRadius="4" Padding="8,6" Margin="0,10,0,0">
          <TextBlock Foreground="#8A3B3B" FontSize="11" TextWrapping="Wrap"
                     Text="Drawn as Model Lines (a closed boundary loop), one per ticked table. Revit's own Property Line object cannot be created through its public API, so promoting this into a true Property Line still needs Revit's native Property Lines &gt; Edit Table dialog."/>
        </Border>

        <TextBlock Text="The X/Y values in the file are" Style="{StaticResource SectionLabel}"/>
        <StackPanel>
          <RadioButton x:Name="RbShared" GroupName="Coord" IsChecked="True"
                       Style="{StaticResource DqtRadio}"
                       Content="Shared / Survey coordinates (Easting/Northing - converted through the project's Survey Point)"/>
          <RadioButton x:Name="RbInternal" GroupName="Coord"
                       Style="{StaticResource DqtRadio}"
                       Content="Internal / Project coordinates (used as-is)"/>
        </StackPanel>
        <TextBlock x:Name="TransformWarning" Foreground="#B03A2E" FontSize="11"
                   Margin="0,4,0,0" TextWrapping="Wrap"/>

        <Grid Margin="0,12,0,0">
          <Grid.ColumnDefinitions>
            <ColumnDefinition Width="*"/>
            <ColumnDefinition Width="12"/>
            <ColumnDefinition Width="*"/>
          </Grid.ColumnDefinitions>

          <StackPanel Grid.Column="0">
            <TextBlock Text="Unit in the file" Style="{StaticResource SectionLabel}"/>
            <StackPanel>
              <RadioButton x:Name="RbUnitM" GroupName="Unit" IsChecked="True"
                           Style="{StaticResource DqtRadio}" Content="Meters"/>
              <RadioButton x:Name="RbUnitMm" GroupName="Unit"
                           Style="{StaticResource DqtRadio}" Content="Millimeters"/>
              <RadioButton x:Name="RbUnitFt" GroupName="Unit"
                           Style="{StaticResource DqtRadio}" Content="Feet"/>
            </StackPanel>
          </StackPanel>

          <StackPanel Grid.Column="2">
            <TextBlock Text="Flatten onto Level" Style="{StaticResource SectionLabel}"/>
            <ComboBox x:Name="CmbLevel" Height="26" FontSize="12"/>
            <CheckBox x:Name="ChkClose" Style="{StaticResource DqtCheck}"
                      IsChecked="True" Margin="2,14,0,3"
                      Content="Close loop back to first point"/>
          </StackPanel>
        </Grid>

        <Border x:Name="PreviewBorder" Background="#FAF3E0"
                BorderBrush="#D4B87A" BorderThickness="1"
                CornerRadius="4" Padding="10,8" Margin="0,14,0,0">
          <TextBlock x:Name="PreviewText" Foreground="#5D4E37" FontSize="11"
                     TextWrapping="Wrap"/>
        </Border>

        <StackPanel Orientation="Horizontal" HorizontalAlignment="Right"
                    Margin="0,16,0,0">
          <Button x:Name="BtnCancel" Content="Cancel"
                  Style="{StaticResource DqtButton}"/>
          <Button x:Name="BtnApply" Content="Create"
                  Style="{StaticResource DqtButton}"/>
        </StackPanel>

      </StackPanel>
    </Border>
    </ScrollViewer>

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


class PropertyLineDialog(object):
    """Themed settings window. All Excel parsing happened before this is
    built - nothing here touches the document until Create is pressed and
    the window has closed."""

    def __init__(self, filepath, tables, levels, mapping):
        self.tables = tables
        self.levels = levels
        self.mapping = mapping
        self.confirmed = False

        self.coord_mode = COORD_SHARED
        self.unit = UNIT_M
        self.close_loop = True
        self.selected_indices = []
        self.selected_level = levels[0] if levels else None

        stream = MemoryStream(Encoding.UTF8.GetBytes(XAML_MAIN))
        self.window = XamlReader.Load(stream)

        self.file_text = self.window.FindName("FileText")
        self.tables_panel = self.window.FindName("TablesPanel")
        self.rb_shared = self.window.FindName("RbShared")
        self.rb_internal = self.window.FindName("RbInternal")
        self.transform_warning = self.window.FindName("TransformWarning")
        self.rb_unit_m = self.window.FindName("RbUnitM")
        self.rb_unit_mm = self.window.FindName("RbUnitMm")
        self.rb_unit_ft = self.window.FindName("RbUnitFt")
        self.cmb_level = self.window.FindName("CmbLevel")
        self.chk_close = self.window.FindName("ChkClose")
        self.preview_text = self.window.FindName("PreviewText")
        self.btn_apply = self.window.FindName("BtnApply")
        self.btn_cancel = self.window.FindName("BtnCancel")

        self.file_text.Text = filepath

        self.checkboxes = []
        for index, table in enumerate(tables):
            box = CheckBox()
            box.Content = "{0}  -  {1} point(s)".format(table['title'], len(table['points']))
            box.Tag = index
            box.IsChecked = (index == 0)
            box.Checked += self._on_options_changed
            box.Unchecked += self._on_options_changed
            self.tables_panel.Children.Add(box)
            self.checkboxes.append(box)

        for level in levels:
            item = ComboBoxItem()
            item.Content = level_label(level)
            item.Tag = level
            self.cmb_level.Items.Add(item)
        if levels:
            self.cmb_level.SelectedIndex = 0

        if mapping is None:
            self.transform_warning.Text = (
                "Could not read this project's shared-coordinate position - "
                "Shared coordinates will be used as Internal instead.")
        elif is_identity_mapping(mapping):
            self.transform_warning.Text = (
                "This project has not acquired or published shared coordinates "
                "yet (shared and internal coordinates are the same) - Shared "
                "points will land far from the origin. Check Manage > "
                "Coordinates first, or use Internal coordinates.")
        else:
            self.transform_warning.Text = ""

        self.rb_shared.Checked += self._on_options_changed
        self.rb_internal.Checked += self._on_options_changed
        self.rb_unit_m.Checked += self._on_options_changed
        self.rb_unit_mm.Checked += self._on_options_changed
        self.rb_unit_ft.Checked += self._on_options_changed
        self.cmb_level.SelectionChanged += self._on_options_changed
        self.chk_close.Checked += self._on_options_changed
        self.chk_close.Unchecked += self._on_options_changed
        self.btn_apply.Click += self._on_apply
        self.btn_cancel.Click += self._on_cancel

        self._refresh()

    # -- helpers --------------------------------------------------------------
    def _checked_indices(self):
        return [box.Tag for box in self.checkboxes if box.IsChecked]

    def _on_options_changed(self, sender, args):
        try:
            self._refresh()
        except Exception:
            pass

    def _refresh(self):
        self.coord_mode = COORD_SHARED if self.rb_shared.IsChecked else COORD_INTERNAL
        if self.rb_unit_mm.IsChecked:
            self.unit = UNIT_MM
        elif self.rb_unit_ft.IsChecked:
            self.unit = UNIT_FT
        else:
            self.unit = UNIT_M
        self.close_loop = bool(self.chk_close.IsChecked)
        self.selected_indices = self._checked_indices()

        item = self.cmb_level.SelectedItem
        self.selected_level = item.Tag if item is not None else None

        if not self.levels:
            self.preview_text.Text = "This project has no Levels - add one first."
            self.btn_apply.IsEnabled = False
            return
        if not self.selected_indices:
            self.preview_text.Text = "Tick at least one table."
            self.btn_apply.IsEnabled = False
            return

        total_points = sum(len(self.tables[i]['points']) for i in self.selected_indices)
        min_needed = 3 if self.close_loop else 2
        too_small = [self.tables[i]['title'] for i in self.selected_indices
                     if len(self.tables[i]['points']) < min_needed]

        lines = ["{0} table(s), {1} point(s) total.".format(
            len(self.selected_indices), total_points)]
        lines.extend(self._coordinate_check_lines())
        if too_small:
            lines.append("Needs at least {0} points per table - too few in: {1}".format(
                min_needed, ", ".join(too_small)))
        self.preview_text.Text = "\n".join(lines)
        self.btn_apply.IsEnabled = not too_small

    def _coordinate_check_lines(self):
        """Prove the shared-coordinate conversion against Revit itself.

        Take the first point that is about to be drawn, convert it, and ask
        Revit to report the shared coordinates of where it landed. If they do
        not come back as the number in the spreadsheet, the boundary is going
        in the wrong place and the user gets told before anything is drawn -
        rather than after, by looking at it."""
        if self.coord_mode != COORD_SHARED:
            return ["Using the file's numbers as internal coordinates, "
                    "unconverted."]
        if self.mapping is None:
            return []

        table = self.tables[self.selected_indices[0]]
        label, easting_raw, northing_raw = table['points'][0]
        easting_ft = to_internal(easting_raw, self.unit)
        northing_ft = to_internal(northing_raw, self.unit)

        error_ft = roundtrip_error_ft(self.mapping, easting_ft, northing_ft)
        if error_ft is None:
            return ["Could not verify the conversion against Revit."]
        if error_ft > to_internal(DUP_TOL_MM, UNIT_MM):
            return ["WARNING: {0} would land {1} mm away from its stated "
                    "coordinates. Do not use this result.".format(
                        label, int(round(ft_to_mm(error_ft))))]
        return ["Checked: {0} lands where Revit reports E {1}, N {2} - "
                "matching the file.".format(label, easting_raw, northing_raw)]

    # -- events -----------------------------------------------------------------
    def _on_apply(self, sender, args):
        self._refresh()
        if not self.btn_apply.IsEnabled:
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
    filepath = pick_excel_file()
    if not filepath:
        return

    try:
        sheets = read_workbook(filepath)
    except Exception as error:
        TaskDialog.Show("Property Line from Excel",
                        "Could not read this file:\n{0}".format(str(error)))
        return

    tables = []
    for name, grid in sheets:
        tables.extend(find_tables(name, grid))

    if not tables:
        TaskDialog.Show(
            "Property Line from Excel",
            "No coordinate table was found in this file.\n\n"
            "Expected a header row with Easting/X and Northing/Y columns, "
            "and a Point/Name column just to their left.")
        return

    levels = list(DB.FilteredElementCollector(doc).OfClass(DB.Level).ToElements())
    levels.sort(key=lambda level: level.Elevation)

    mapping = get_shared_to_internal()

    dialog = PropertyLineDialog(filepath, tables, levels, mapping)
    if not dialog.show():
        return

    coord_mode = dialog.coord_mode
    unit = dialog.unit
    close_loop = dialog.close_loop
    elevation_ft = dialog.selected_level.Elevation
    selected_tables = [tables[i] for i in dialog.selected_indices]

    results = []
    ok_any = False
    group = TransactionGroup(doc, "DQT - Property Line from Excel")
    group.Start()
    try:
        for table in selected_tables:
            transaction = Transaction(doc, "DQT - Property Line from Excel")
            transaction.Start()
            try:
                points_ft = build_points_ft(table, unit, coord_mode, elevation_ft,
                                            dialog.mapping)
                cleaned, dropped = dedupe_consecutive(points_ft, close_loop)
                min_needed = 3 if close_loop else 2
                if len(cleaned) < min_needed:
                    raise Exception(
                        "only {0} distinct point(s) after removing duplicates - "
                        "need at least {1}".format(len(cleaned), min_needed))

                count = create_model_lines(cleaned, close_loop, elevation_ft)
                made = "{0} Model Line segment(s)".format(count)

                transaction.Commit()
                ok_any = True
                message = "{0}: {1} ({2} point(s))".format(table['title'], made, len(cleaned))
                if dropped:
                    message += " - {0} duplicate point(s) skipped".format(dropped)
                results.append(message)
            except Exception as error:
                transaction.RollBack()
                results.append("{0}: FAILED - {1}".format(table['title'], str(error)))

        if ok_any:
            group.Assimilate()
        else:
            group.RollBack()
    except Exception as error:
        group.RollBack()
        TaskDialog.Show("Property Line from Excel", "Error: {0}".format(str(error)))
        return

    TaskDialog.Show("Property Line from Excel", "\n".join(results))


run()
