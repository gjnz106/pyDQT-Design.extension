# -*- coding: utf-8 -*-
"""
CAD to Family v1.0 - DQT
Turns a 3D CAD import (DWG, DXF, SAT, SKP, ...) into a loadable Generic Model
family and places the family exactly where every copy of the CAD is.

The CAD's 3D geometry is rebuilt as native Revit geometry inside the family:
  - solids become solid Freeform forms;
  - closed meshes (the triangles a DWG polyface or mesh is made of) are turned
    into solids first, then into solid forms;
  - anything that cannot become a solid (an open mesh, loose 3D faces) is kept
    as a mesh in the family (a DirectShape) so the shape is still complete.
The solid forms share a "Material" type parameter.

One family is made per CAD file and one instance of it placed on each copy of
the CAD - at the same point, turned the same way in plan. A copy that is tilted,
mirrored or scaled in 3D cannot be matched by turning a Generic Model in plan,
so it gets a family of its own with that orientation built in.

The family keeps its origin at the bottom centre of the CAD geometry, not at
the DWG's origin, so a drawing far from its origin does not end up with distant
geometry in the family.

Workflow:
  1. Select the CAD import(s) - or run with none selected and pick one.
  2. Choose all copies of the CAD in the project, or only the selected ones,
     and whether to delete the CAD once the family is placed.
  3. The family is built, loaded and placed in one step you can Undo.

Copyright (c) 2026 Dang Quoc Truong (DQT)
All rights reserved.
"""
try: __import__("dqt_usage").log_use(__file__)  # pyDQT usage log
except Exception: pass

__title__ = "CAD to\nFamily"
__author__ = "Dang Quoc Truong (DQT)"
__doc__ = ("Turn a 3D CAD import into a Generic Model family and place it "
           "exactly where every copy of the CAD is.")

# ==============================================================================
# IMPORTS
# ==============================================================================
import os
import math
import tempfile
import clr

clr.AddReference('RevitAPI')
clr.AddReference('RevitAPIUI')

import Autodesk.Revit.DB as DB
from Autodesk.Revit.DB import Transaction
from Autodesk.Revit.DB.Structure import StructuralType
from Autodesk.Revit.UI import (
    TaskDialog, TaskDialogCommonButtons, TaskDialogCommandLinkId,
    TaskDialogResult
)
from Autodesk.Revit.UI.Selection import ObjectType, ISelectionFilter
from System.Collections.Generic import List

doc = __revit__.ActiveUIDocument.Document
uidoc = __revit__.ActiveUIDocument
app = __revit__.Application

# ==============================================================================
# CONSTANTS
# ==============================================================================
TITLE = "CAD to Family"
HELP_PAGE = "cad_to_family.html"
TEMP_FOLDER = "DQT_CadToFamily"
MIN_VOLUME_FT3 = 1e-9                    # ignore empty / degenerate solids
MIN_AREA_FT2 = 1e-10                     # ignore degenerate triangles
MOVE_TOLERANCE_FT = 1e-6
ANGLE_TOLERANCE = 1e-6                   # rad
BASIS_TOLERANCE = 1e-6                   # unit vectors compared component-wise
MAX_NESTING = 8                          # CAD blocks inside blocks
CAD_EXTENSIONS = (".dwg", ".dxf", ".dgn", ".sat", ".skp", ".3dm", ".axm", ".obj",
                  ".stl")
# Characters a family name cannot have: Revit's own list plus the ones a file
# name cannot have (the family is saved to a file under its name to be loaded).
BAD_NAME_CHARS = "\\/:*?\"<>|{}[];`~"
MAX_NAME_PART = 80
FOOTER = "Dang Quoc Truong - DQT (c) 2026"
MATERIAL_PARAMETER = "Material"
# Generic Model templates that are NOT the plain, free-standing one.
HOSTED_WORDS = ("face based", "wall based", "ceiling based", "floor based",
                "roof based", "line based", "pattern based", "adaptive",
                "work plane")


class CopyError(Exception):
    """The CAD cannot be turned into a family, with the reason to show."""
    pass


class TemplateError(Exception):
    """The family template cannot be used."""
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


def element_name(element):
    try:
        return DB.Element.Name.GetValue(element)
    except Exception:
        pass
    try:
        return element.Name
    except Exception:
        return ""


def is_cad(element):
    """A CAD import or link placed in 3D (a 2D, view-specific one has no 3D
    geometry to turn into a family)."""
    try:
        return isinstance(element, DB.ImportInstance) and not element.ViewSpecific
    except Exception:
        return False


def cad_name(document, instance):
    """The CAD file's name as Revit shows it, e.g. "xblocplus_0.5m3.dwg"."""
    try:
        name = element_name(document.GetElement(instance.GetTypeId()))
        if name:
            return name
    except Exception:
        pass
    return "CAD"


def clean_name_part(name):
    """A name made safe to use as a family name / file name."""
    text = name or ""
    for ch in BAD_NAME_CHARS:
        text = text.replace(ch, "-")
    text = text.strip().rstrip(".").strip()
    if not text:
        return "Unnamed"
    return text[:MAX_NAME_PART].strip()


def family_base_name(cad_file_name):
    """"xblocplus_0.5m3.dwg" -> "xblocplus_0.5m3" (only a known CAD extension
    is dropped - "0.5m3" is part of the name, not an extension)."""
    name = cad_file_name or ""
    for ext in CAD_EXTENSIONS:
        if name.lower().endswith(ext):
            name = name[:-len(ext)]
            break
    return clean_name_part(name)


def unique_family_name(base, taken):
    """base, or "base v2", "base v3"... - whichever is not in taken (lower-case
    names). The chosen name is added to taken."""
    name = base
    number = 1
    while name.lower() in taken:
        number += 1
        name = "{0} v{1}".format(base, number)
    taken.add(name.lower())
    return name


def temp_folder():
    folder = os.path.join(tempfile.gettempdir(), TEMP_FOLDER)
    if not os.path.isdir(folder):
        os.makedirs(folder)
    return folder


def delete_file(path):
    try:
        if path and os.path.isfile(path):
            os.remove(path)
    except Exception:
        pass


def remove_temp_folder_if_empty():
    try:
        folder = os.path.join(tempfile.gettempdir(), TEMP_FOLDER)
        if os.path.isdir(folder) and not os.listdir(folder):
            os.rmdir(folder)
    except Exception:
        pass


def roll_back_quietly(transaction):
    """Roll back a transaction that may already have ended (a Commit that
    failed ends it), without letting that hide the original error."""
    try:
        if transaction.HasStarted() and not transaction.HasEnded():
            transaction.RollBack()
    except Exception:
        pass


def _open_help_page(html_filename):
    """Open this tool's page from the shared _Modify_Help folder in the
    default browser. False when the page cannot be found or opened."""
    try:
        panel_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(panel_dir, "_Modify_Help", html_filename)
        if not os.path.isfile(path):
            return False
        os.startfile(path)
        return True
    except Exception:
        return False


# ==============================================================================
# GEOMETRY - read from the CAD in its own coordinates
# ==============================================================================
def geometry_options():
    """Finished geometry at Fine detail, with no references or hidden objects."""
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


def usable_mesh(mesh):
    try:
        return mesh.NumTriangles > 0
    except Exception:
        return False


def flatten(geometry, transform, depth=0):
    """[(kind, object, transform)] - every solid and mesh in `geometry`, with
    the transform that takes it into the CAD's own coordinates. Blocks inside
    the CAD (nested geometry instances) are opened up."""
    found = []
    if geometry is None or depth > MAX_NESTING:
        return found
    for item in geometry:
        if isinstance(item, DB.Solid):
            if usable_solid(item):
                found.append(("solid", item, transform))
        elif isinstance(item, DB.Mesh):
            if usable_mesh(item):
                found.append(("mesh", item, transform))
        elif isinstance(item, DB.GeometryInstance):
            try:
                found.extend(flatten(item.GetSymbolGeometry(),
                                     transform.Multiply(item.Transform), depth + 1))
            except Exception:
                continue
    return found


def read_cad(instance):
    """(placement, items): the transform from the CAD's own coordinates to the
    project, and its 3D solids / meshes in the CAD's own coordinates."""
    geometry = instance.get_Geometry(geometry_options())
    if geometry is None:
        return None, []
    placement = None
    items = []
    loose = []
    for item in geometry:
        if placement is None and isinstance(item, DB.GeometryInstance):
            placement = item.Transform
            items.extend(flatten(item.GetSymbolGeometry(), DB.Transform.Identity))
        else:
            loose.append(item)
    if placement is None:
        try:
            placement = instance.GetTotalTransform()
        except Exception:
            placement = DB.Transform.Identity
    if loose:
        # geometry outside the CAD's own instance is in project coordinates
        back = placement.Inverse
        items.extend(flatten(loose, back))
    return placement, items


def item_points(kind, obj, transform):
    """Points that bound one solid / mesh, in the coordinates `transform`
    takes it to."""
    points = []
    if kind == "mesh":
        try:
            for vertex in obj.Vertices:
                points.append(transform.OfPoint(vertex))
        except Exception:
            pass
        return points
    try:
        box = obj.GetBoundingBox()
        box_transform = box.Transform
        lo, hi = box.Min, box.Max
        for x in (lo.X, hi.X):
            for y in (lo.Y, hi.Y):
                for z in (lo.Z, hi.Z):
                    points.append(transform.OfPoint(box_transform.OfPoint(DB.XYZ(x, y, z))))
    except Exception:
        pass
    return points


def base_point(items, outer=None):
    """The family's origin: the middle of the geometry's footprint at its lowest
    point - in the CAD's own coordinates, or after `outer` when given."""
    xs, ys, zs = [], [], []
    for kind, obj, transform in items:
        full = outer.Multiply(transform) if outer is not None else transform
        for point in item_points(kind, obj, full):
            xs.append(point.X)
            ys.append(point.Y)
            zs.append(point.Z)
    if not xs:
        raise CopyError("its 3D geometry has no extent")
    return DB.XYZ((min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0, min(zs))


def linear_part(transform):
    """The transform without its translation (rotation / mirror / scale)."""
    linear = DB.Transform(transform)
    linear.Origin = DB.XYZ(0, 0, 0)
    return linear


def _close(a, b, tolerance=BASIS_TOLERANCE):
    return (abs(a.X - b.X) <= tolerance and abs(a.Y - b.Y) <= tolerance
            and abs(a.Z - b.Z) <= tolerance)


def plan_angle(placement):
    """The angle a copy is turned in plan (radians) when it is ONLY turned in
    plan - None when it is tilted, mirrored or scaled, which a Generic Model
    cannot copy by turning."""
    bx, by, bz = placement.BasisX, placement.BasisY, placement.BasisZ
    if not _close(bz, DB.XYZ(0, 0, 1)):
        return None
    if abs(bx.Z) > BASIS_TOLERANCE or abs(bx.GetLength() - 1.0) > BASIS_TOLERANCE:
        return None
    if not _close(DB.XYZ(0, 0, 1).CrossProduct(bx), by):
        return None
    return math.atan2(bx.Y, bx.X)


# ==============================================================================
# FAMILY GEOMETRY
# ==============================================================================
def triangles(mesh, transform):
    """The mesh's triangles as [p0, p1, p2] lists, moved by `transform`;
    degenerate (zero-area) triangles are left out."""
    result = []
    for index in range(mesh.NumTriangles):
        try:
            triangle = mesh.get_Triangle(index)
            points = [transform.OfPoint(triangle.get_Vertex(k)) for k in range(3)]
        except Exception:
            continue
        area = points[1].Subtract(points[0]).CrossProduct(
            points[2].Subtract(points[0])).GetLength() / 2.0
        if area > MIN_AREA_FT2:
            result.append(points)
    return result


def build_shape(triangle_lists):
    """Feed triangles to Revit's TessellatedShapeBuilder as ONE connected face
    set, aiming for a solid and falling back to a mesh. (solids, meshes)."""
    builder = DB.TessellatedShapeBuilder()
    builder.OpenConnectedFaceSet(True)
    for points in triangle_lists:
        builder.AddFace(DB.TessellatedFace(List[DB.XYZ](points),
                                           DB.ElementId.InvalidElementId))
    builder.CloseConnectedFaceSet()
    builder.Target = DB.TessellatedShapeBuilderTarget.Solid
    builder.Fallback = DB.TessellatedShapeBuilderFallback.Mesh
    result = builder.Build()
    if result is None:                    # Revit 2018+: Build() returns nothing
        result = builder.GetBuildResult()
    solids, meshes = [], []
    for obj in result.GetGeometricalObjects():
        if isinstance(obj, DB.Solid):
            if usable_solid(obj):
                solids.append(obj)
        else:
            meshes.append(obj)
    return solids, meshes


def add_material_parameter(family_doc):
    """A "Material" type parameter for the solid forms, or None if this Revit
    version will not make one (the forms then use By Category)."""
    manager = family_doc.FamilyManager
    try:
        return manager.AddParameter(MATERIAL_PARAMETER, DB.GroupTypeId.Materials,
                                    DB.SpecTypeId.Reference.Material, False)
    except Exception:
        pass
    try:
        return manager.AddParameter(MATERIAL_PARAMETER,
                                    DB.BuiltInParameterGroup.PG_MATERIALS,
                                    DB.ParameterType.Material, False)
    except Exception:
        return None


def link_material(family_doc, form, parameter):
    if parameter is None:
        return
    try:
        target = form.get_Parameter(DB.BuiltInParameter.MATERIAL_ID_PARAM)
        if target is not None:
            family_doc.FamilyManager.AssociateElementParameterToFamilyParameter(
                target, parameter)
    except Exception:
        pass


def add_mesh_shape(family_doc, objects):
    """Keep geometry that cannot be a solid as a DirectShape in the family.
    True when Revit took it."""
    if not objects:
        return False
    try:
        if not DB.DirectShape.IsSupportedDocument(family_doc):
            return False
    except AttributeError:
        pass                              # older Revit: just try
    except Exception:
        return False
    try:
        shape = DB.DirectShape.CreateElement(
            family_doc, DB.ElementId(DB.BuiltInCategory.OST_GenericModel))
        shape.SetShape(List[DB.GeometryObject](objects))
        return True
    except Exception:
        return False


def add_form(family_doc, solid, material):
    """A solid Freeform form from `solid`, tied to the Material parameter.
    False when Revit refuses the solid."""
    try:
        form = DB.FreeFormElement.Create(family_doc, solid)
    except Exception:
        return False
    link_material(family_doc, form, material)
    return True


class BuildStats(object):
    """What went into a family - for the message at the end."""

    def __init__(self):
        self.solids = 0          # CAD solids copied as solid forms
        self.mesh_solids = 0     # solid forms made from CAD meshes
        self.mesh_shapes = 0     # pieces kept as DirectShape geometry
        self.skipped = 0         # pieces Revit refused

    def add(self, other):
        self.solids += other.solids
        self.mesh_solids += other.mesh_solids
        self.mesh_shapes += other.mesh_shapes
        self.skipped += other.skipped

    @property
    def total(self):
        return self.solids + self.mesh_solids + self.mesh_shapes


def fill_family(family_doc, items, outer):
    """Put the CAD's solids and meshes into the family document, each taken
    through `outer` (CAD coordinates -> family coordinates). Runs inside the
    caller's family transaction. Returns BuildStats."""
    stats = BuildStats()
    material = add_material_parameter(family_doc)

    def keep(solid, counter):
        if add_form(family_doc, solid, material):
            setattr(stats, counter, getattr(stats, counter) + 1)
        elif add_mesh_shape(family_doc, [solid]):
            stats.mesh_shapes += 1
        else:
            stats.skipped += 1

    leftover = []                         # (triangles, meshes) that made no solid
    for kind, obj, transform in items:
        full = outer.Multiply(transform)
        if kind == "solid":
            try:
                moved = DB.SolidUtils.CreateTransformed(obj, full)
            except Exception:
                stats.skipped += 1
                continue
            keep(moved, "solids")
            continue
        tris = triangles(obj, full)
        if not tris:
            stats.skipped += 1
            continue
        try:
            solids, meshes = build_shape(tris)
        except Exception:
            solids, meshes = [], []
        if solids:
            for solid in solids:
                keep(solid, "mesh_solids")
        else:
            leftover.append((tris, meshes))

    if len(leftover) > 1:
        # Loose faces (one mesh per 3D face) can close up into a solid together.
        try:
            solids, _ = build_shape([t for tris, _ in leftover for t in tris])
        except Exception:
            solids = []
        if solids:
            for solid in solids:
                keep(solid, "mesh_solids")
            leftover = []
    for tris, meshes in leftover:
        if add_mesh_shape(family_doc, meshes):
            stats.mesh_shapes += 1
        else:
            stats.skipped += 1
    return stats


# ==============================================================================
# FAMILY TEMPLATE
# ==============================================================================
def template_search_roots(application):
    """Folders to look for the template in: the one set in Options > File
    Locations, then the standard install location for this Revit version."""
    roots = []
    try:
        configured = application.FamilyTemplatePath
        if configured:
            roots.append(configured)
    except Exception:
        pass
    try:
        program_data = os.environ.get("ProgramData")
        version = application.VersionNumber
        if program_data and version:
            roots.append(os.path.join(program_data, "Autodesk",
                                      "RVT " + version, "Family Templates"))
    except Exception:
        pass
    return roots


def template_rank(file_name):
    """How good a match this file is for the plain Generic Model template: 0
    best, None not a candidate (not a Generic Model template, or a hosted /
    adaptive / line-based kind)."""
    base = os.path.splitext(file_name)[0].lower()
    if "generic model" not in base:
        return None
    for word in HOSTED_WORDS:
        if word in base:
            return None
    if base == "metric generic model":
        return 0
    if base == "generic model":
        return 1
    return 2


def find_generic_model_template(application, max_depth=3):
    """Path of the Generic Model family template, or None."""
    searched = set()
    for root in template_search_roots(application):
        key = os.path.normcase(os.path.normpath(root))
        if key in searched or not os.path.isdir(root):
            continue
        searched.add(key)
        best = None
        base_depth = root.rstrip("\\/").count(os.sep)
        for folder, subfolders, files in os.walk(root):
            if folder.count(os.sep) - base_depth >= max_depth:
                del subfolders[:]
            for file_name in files:
                if not file_name.lower().endswith(".rft"):
                    continue
                rank = template_rank(file_name)
                if rank is None:
                    continue
                order = (rank, len(folder), folder.lower(), file_name.lower())
                if best is None or order < best[0]:
                    best = (order, os.path.join(folder, file_name))
        if best is not None:
            return best[1]
    return None


def ask_for_template():
    """Let the user browse for the template when it cannot be found (a
    localized install names it differently). None if they cancel."""
    try:
        clr.AddReference('System.Windows.Forms')
        from System.Windows.Forms import OpenFileDialog, DialogResult
        dialog = OpenFileDialog()
        dialog.Title = ("Pick the Generic Model family template "
                        "(Metric Generic Model.rft)")
        dialog.Filter = "Family templates (*.rft)|*.rft"
        if dialog.ShowDialog() == DialogResult.OK:
            return dialog.FileName
    except Exception:
        pass
    return None


# ==============================================================================
# BUILD THE FAMILY FILE (in a family document of its own)
# ==============================================================================
def build_family_file(application, template_path, items, outer, family_name):
    """Make a family from the template with the CAD's geometry in it (taken
    through `outer`), save it as <family_name>.rfa in the temp folder and
    return (path, BuildStats). The family document is always closed again."""
    family_doc = application.NewFamilyDocument(template_path)
    if family_doc is None:
        raise TemplateError("Revit could not open the family template "
                            "{0}".format(template_path))
    try:
        category = family_doc.OwnerFamily.FamilyCategory
        if (category is None or
                eid_int(category.Id) != int(DB.BuiltInCategory.OST_GenericModel)):
            raise TemplateError(
                "{0} is not a Generic Model family template.".format(template_path))

        transaction = Transaction(family_doc, "DQT - CAD geometry")
        transaction.Start()
        try:
            stats = fill_family(family_doc, items, outer)
            if stats.total == 0:
                raise CopyError("Revit refused all of its 3D geometry")
            if transaction.Commit() != DB.TransactionStatus.Committed:
                raise CopyError("Revit rejected the CAD's geometry in the family")
        except Exception:
            roll_back_quietly(transaction)
            raise

        path = os.path.join(temp_folder(), family_name + ".rfa")
        options = DB.SaveAsOptions()
        options.OverwriteExistingFile = True
        family_doc.SaveAs(path, options)
        return path, stats
    finally:
        try:
            family_doc.Close(False)
        except Exception:
            pass


# ==============================================================================
# PLAN - which family each copy of the CAD gets
# ==============================================================================
class Placement(object):
    """One copy of the CAD and where its family instance goes."""

    def __init__(self, instance, location, angle):
        self.instance = instance
        self.location = location   # project point of the family's origin
        self.angle = angle         # plan rotation (radians) - 0 for a built-in one


class FamilyPlan(object):
    """One family to build and the copies it is placed on."""

    def __init__(self, cad_type_id, name, items, outer, label, origin=None):
        self.cad_type_id = cad_type_id
        self.name = name
        self.items = items
        self.outer = outer         # CAD coordinates -> family coordinates
        self.origin = origin       # the family's origin in CAD coordinates
        self.label = label         # the CAD file's name, for messages
        self.placements = []
        self.path = None
        self.stats = None


def orientation_key(linear):
    """Copies tilted / mirrored / scaled exactly the same way share a family."""
    return tuple(round(value, 6) for basis in (linear.BasisX, linear.BasisY, linear.BasisZ)
                 for value in (basis.X, basis.Y, basis.Z))


def plan_families(document, instances, taken_names):
    """[FamilyPlan] for these CAD instances: one shared family per CAD file
    for the copies only turned in plan, and one family per orientation for
    the copies that are tilted, mirrored or scaled. Raises CopyError naming
    the CAD when it has no 3D geometry."""
    plans = []
    by_type = {}
    for instance in instances:
        by_type.setdefault(eid_int(instance.GetTypeId()), []).append(instance)
    for type_key in sorted(by_type):
        copies = by_type[type_key]
        label = cad_name(document, copies[0])
        base = family_base_name(label)
        shared = None
        tilted = {}                       # orientation -> its own FamilyPlan
        for instance in copies:
            placement, items = read_cad(instance)
            if not items:
                raise CopyError(
                    "{0} has no 3D solids or meshes - only lines, or nothing "
                    "that can become family geometry".format(label))
            angle = plan_angle(placement)
            if angle is not None:
                if shared is None:
                    origin = base_point(items)
                    shared = FamilyPlan(copies[0].GetTypeId(),
                                        unique_family_name(base, taken_names),
                                        items, DB.Transform.CreateTranslation(origin.Negate()),
                                        label, origin)
                    plans.append(shared)
                shared.placements.append(
                    Placement(instance, placement.OfPoint(shared.origin), angle))
            else:
                linear = linear_part(placement)
                key = orientation_key(linear)
                own = tilted.get(key)
                if own is None:
                    origin = base_point(items, linear)
                    outer = DB.Transform.CreateTranslation(origin.Negate()).Multiply(linear)
                    own = FamilyPlan(copies[0].GetTypeId(),
                                     unique_family_name("{0} (tilted {1})".format(
                                         base, len(tilted) + 1), taken_names),
                                     items, outer, label, origin)
                    tilted[key] = own
                    plans.append(own)
                own.placements.append(
                    Placement(instance, own.origin.Add(placement.Origin), 0.0))
    return plans


# ==============================================================================
# LOAD AND PLACE (in the project)
# ==============================================================================
def existing_family_names(document):
    """Lower-case names of every family already in the project."""
    names = set()
    try:
        for family in DB.FilteredElementCollector(document).OfClass(DB.Family):
            try:
                names.add(family.Name.lower())
            except Exception:
                continue
    except Exception:
        pass
    return names


def find_family(document, family_name):
    for family in DB.FilteredElementCollector(document).OfClass(DB.Family):
        try:
            if family.Name == family_name:
                return family
        except Exception:
            continue
    return None


def project_levels(document):
    try:
        return sorted(DB.FilteredElementCollector(document).OfClass(DB.Level),
                      key=lambda level: level.Elevation)
    except Exception:
        return []


def level_for(document, instance, location, levels):
    """The CAD's own level when it has one, else the highest level at or below
    the family's origin, else the lowest level."""
    try:
        level = document.GetElement(instance.LevelId)
        if isinstance(level, DB.Level):
            return level
    except Exception:
        pass
    below = [level for level in levels if level.Elevation <= location.Z + MOVE_TOLERANCE_FT]
    if below:
        return below[-1]
    return levels[0] if levels else None


def place_instance(document, symbol, location, level):
    try:
        if level is not None:
            return document.Create.NewFamilyInstance(
                location, symbol, level, StructuralType.NonStructural)
        return document.Create.NewFamilyInstance(
            location, symbol, StructuralType.NonStructural)
    except AttributeError:
        # Revit versions that removed Document.Create.NewFamilyInstance
        return DB.FamilyInstance.Create(
            document, symbol.Id, location, level, StructuralType.NonStructural)


def snap_to(document, instance, target):
    """Move the instance so its insertion point is exactly `target`, whatever
    height Revit gave it from the level."""
    try:
        point = instance.Location.Point
    except Exception:
        return False
    delta = target.Subtract(point)
    if delta.GetLength() <= MOVE_TOLERANCE_FT:
        return False
    DB.ElementTransformUtils.MoveElement(document, instance.Id, delta)
    return True


def turn_to(document, instance, location, angle):
    """Turn the instance in plan by `angle` about its insertion point."""
    if abs(angle) <= ANGLE_TOLERANCE:
        return False
    axis = DB.Line.CreateBound(location, location.Add(DB.XYZ(0, 0, 1)))
    DB.ElementTransformUtils.RotateElement(document, instance.Id, axis, angle)
    return True


def copy_workset(document, source, target):
    """Put the family instance on the CAD's workset."""
    if not document.IsWorkshared:
        return
    try:
        from_param = source.get_Parameter(DB.BuiltInParameter.ELEM_PARTITION_PARAM)
        to_param = target.get_Parameter(DB.BuiltInParameter.ELEM_PARTITION_PARAM)
        if from_param is not None and to_param is not None and not to_param.IsReadOnly:
            to_param.Set(from_param.AsInteger())
    except Exception:
        pass


def write_comments(instance, label, source):
    try:
        comments = instance.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
        if comments is not None and not comments.IsReadOnly:
            comments.Set("From CAD {0} (id {1})".format(label, eid_int(source.Id)))
    except Exception:
        pass


def load_family(document, plan):
    """Load the plan's family file; returns its (activated) family type."""
    result = document.LoadFamily(plan.path)
    loaded = result[0] if isinstance(result, tuple) else result
    if not loaded:
        raise CopyError("Revit could not load the family {0}".format(plan.name))
    family = find_family(document, plan.name)
    if family is None:
        raise CopyError("the loaded family {0} could not be found".format(plan.name))
    symbol_ids = list(family.GetFamilySymbolIds())
    if not symbol_ids:
        raise CopyError("the family {0} has no type to place".format(plan.name))
    symbol = document.GetElement(symbol_ids[0])
    if not symbol.IsActive:
        symbol.Activate()
    return symbol


def remaining_instances(document, type_id, removed):
    """CAD instances of this type (2D ones included) that are not being deleted."""
    left = 0
    for instance in DB.FilteredElementCollector(document).OfClass(DB.ImportInstance):
        try:
            if instance.GetTypeId() == type_id and eid_int(instance.Id) not in removed:
                left += 1
        except Exception:
            continue
    return left


def apply_plans(document, plans, delete_cad):
    """Load every family and place its instances - and delete the CAD when
    asked - in ONE transaction (one Undo). Returns the new instance ids."""
    new_ids = []
    transaction = Transaction(document, "DQT - CAD to Family")
    transaction.Start()
    try:
        levels = project_levels(document)
        placed = []
        for plan in plans:
            symbol = load_family(document, plan)
            for spot in plan.placements:
                level = level_for(document, spot.instance, spot.location, levels)
                instance = place_instance(document, symbol, spot.location, level)
                placed.append((instance, spot, plan))
        document.Regenerate()
        for instance, spot, plan in placed:
            snap_to(document, instance, spot.location)
            turn_to(document, instance, spot.location, spot.angle)
            copy_workset(document, spot.instance, instance)
            write_comments(instance, plan.label, spot.instance)
            new_ids.append(instance.Id)

        if delete_cad:
            removed = set()
            ids = List[DB.ElementId]()
            type_ids = []
            for plan in plans:
                for spot in plan.placements:
                    key = eid_int(spot.instance.Id)
                    if key not in removed:
                        removed.add(key)
                        ids.Add(spot.instance.Id)
                if all(t != plan.cad_type_id for t in type_ids):
                    type_ids.append(plan.cad_type_id)
            empty_types = [t for t in type_ids
                           if remaining_instances(document, t, removed) == 0]
            document.Delete(ids)
            for type_id in empty_types:
                try:
                    if document.GetElement(type_id) is not None:
                        document.Delete(type_id)
                except Exception:
                    pass

        if transaction.Commit() != DB.TransactionStatus.Committed:
            raise CopyError("Revit rolled the change back")
    except Exception:
        roll_back_quietly(transaction)
        raise
    return new_ids


def run_plans(document, application, template_path, plans, delete_cad):
    """Build every family file (no project transaction open), then load and
    place them all in one transaction. Temporary files are always removed."""
    try:
        for plan in plans:
            plan.path, plan.stats = build_family_file(
                application, template_path, plan.items, plan.outer, plan.name)
        return apply_plans(document, plans, delete_cad)
    finally:
        for plan in plans:
            delete_file(plan.path)
        remove_temp_folder_if_empty()


# ==============================================================================
# SELECTION
# ==============================================================================
class CadFilter(ISelectionFilter):
    def AllowElement(self, element):
        return is_cad(element)

    def AllowReference(self, reference, position):
        return False


def selected_cad():
    """The 3D CAD instances in the current selection, or the one the user picks
    when none is selected. [] if cancelled."""
    found = []
    try:
        for element_id in uidoc.Selection.GetElementIds():
            element = doc.GetElement(element_id)
            if is_cad(element):
                found.append(element)
    except Exception:
        pass
    if found:
        return found
    try:
        reference = uidoc.Selection.PickObject(
            ObjectType.Element, CadFilter(),
            "Select the 3D CAD import to turn into a family")
    except Exception:
        return []                 # Escape
    element = doc.GetElement(reference.ElementId)
    return [element] if is_cad(element) else []


def all_copies(document, type_ids):
    """Every 3D CAD instance of these CAD types in the project."""
    keys = set(eid_int(t) for t in type_ids)
    found = []
    for instance in DB.FilteredElementCollector(document).OfClass(DB.ImportInstance):
        try:
            if is_cad(instance) and eid_int(instance.GetTypeId()) in keys:
                found.append(instance)
        except Exception:
            continue
    return found


# ==============================================================================
# MESSAGES
# ==============================================================================
def show(instruction, content=""):
    dialog = TaskDialog(TITLE)
    dialog.MainInstruction = instruction
    dialog.MainContent = content
    dialog.FooterText = FOOTER
    dialog.Show()


def ask_scope(document, selected, everything):
    """("all" | "selected", delete the CAD?) or (None, False) when cancelled.
    Offers the usage guide too - opening it shows the question again."""
    names = {}
    for instance in selected:
        names.setdefault(cad_name(document, instance), [0, 0])[0] += 1
    for instance in everything:
        names.setdefault(cad_name(document, instance), [0, 0])[1] += 1
    lines = ["{0}: {1} selected, {2} in the project".format(name, s, a)
             for name, (s, a) in sorted(names.items())]
    more = len(everything) > len(selected)
    while True:
        dialog = TaskDialog(TITLE)
        dialog.MainInstruction = "Turn the CAD into a Generic Model family"
        dialog.MainContent = (
            "\n".join(lines) + "\n\nOne family is made per CAD file and placed "
            "exactly where each copy of it is.")
        if more:
            dialog.AddCommandLink(
                TaskDialogCommandLinkId.CommandLink1,
                "All {0} copies in the project".format(len(everything)),
                "Every 3D copy of the selected CAD file(s) gets a family instance.")
            dialog.AddCommandLink(
                TaskDialogCommandLinkId.CommandLink2,
                "Only the {0} selected".format(len(selected)))
        else:
            dialog.AddCommandLink(
                TaskDialogCommandLinkId.CommandLink2,
                "Create the family and place {0} instance(s)".format(len(selected)))
        dialog.AddCommandLink(TaskDialogCommandLinkId.CommandLink3,
                              "How it works (usage guide)")
        dialog.VerificationText = ("Delete the CAD once the families are placed "
                                   "(Undo brings it back)")
        dialog.CommonButtons = TaskDialogCommonButtons.Cancel
        dialog.DefaultButton = TaskDialogResult.Cancel
        dialog.FooterText = FOOTER
        answer = dialog.Show()
        if answer == TaskDialogResult.CommandLink3:
            if not _open_help_page(HELP_PAGE):
                show("The usage guide could not be opened.",
                     "It is " + HELP_PAGE + " in the Modify panel's _Modify_Help folder.")
            continue
        delete_cad = bool(dialog.WasVerificationChecked())
        if answer == TaskDialogResult.CommandLink1:
            return "all", delete_cad
        if answer == TaskDialogResult.CommandLink2:
            return "selected", delete_cad
        return None, False


def geometry_text(stats):
    parts = []
    if stats.solids:
        parts.append("{0} CAD solid(s) as solid forms".format(stats.solids))
    if stats.mesh_solids:
        parts.append("{0} solid form(s) made from CAD meshes".format(stats.mesh_solids))
    if stats.mesh_shapes:
        parts.append("{0} piece(s) kept as DirectShape geometry (an open mesh "
                     "cannot become a solid: it shows and prints, but cannot be "
                     "cut or joined, and takes no material)".format(stats.mesh_shapes))
    if stats.skipped:
        parts.append("{0} piece(s) Revit refused were left out".format(stats.skipped))
    return "; ".join(parts)


def done_text(plans, delete_cad):
    lines = []
    for plan in plans:
        lines.append(u"• {0}: {1} instance(s) - {2}.".format(
            plan.name, len(plan.placements), geometry_text(plan.stats)))
    tilted = [plan for plan in plans if "(tilted " in plan.name]
    text = "\n".join(lines)
    if tilted:
        text += ("\n\n{0} copy(ies) of the CAD are tilted, mirrored or scaled in "
                 "3D: a Generic Model only turns in plan, so they got {1} family(ies) "
                 "of their own (one per orientation) with it built in.".format(
                     sum(len(plan.placements) for plan in tilted), len(tilted)))
    text += ("\n\nThe new instances are selected (Generic Models). The solid forms "
             "share the family's \"Material\" type parameter.")
    text += ("\n\nThe CAD was deleted." if delete_cad else
             "\n\nThe CAD is still there - hide or delete it once you have "
             "checked the family.")
    return text


# ==============================================================================
# MAIN
# ==============================================================================
def run():
    if doc.IsFamilyDocument:
        show("Open a project first.",
             "CAD to Family works in a project, not in a family.")
        return
    selected = selected_cad()
    if not selected:
        return
    everything = all_copies(doc, [instance.GetTypeId() for instance in selected])
    scope, delete_cad = ask_scope(doc, selected, everything)
    if scope is None:
        return
    targets = everything if scope == "all" else selected

    template_path = find_generic_model_template(app) or ask_for_template()
    if not template_path:
        show("No Generic Model family template was found.",
             "The tool builds the family from Revit's \"Metric Generic Model.rft\" "
             "template. Set its folder in Options > File Locations > Default path "
             "for family templates, then run the tool again.")
        return

    try:
        plans = plan_families(doc, targets, existing_family_names(doc))
        new_ids = run_plans(doc, app, template_path, plans, delete_cad)
    except TemplateError as error:
        show("The family template cannot be used.", str(error))
        return
    except Exception as error:
        show("The family could not be created - nothing was changed.", str(error))
        return

    try:
        uidoc.Selection.SetElementIds(List[DB.ElementId](new_ids))
    except Exception:
        pass
    show("Created {0} family(ies), {1} instance(s) placed on the CAD.".format(
        len(plans), len(new_ids)), done_text(plans, delete_cad))


run()
