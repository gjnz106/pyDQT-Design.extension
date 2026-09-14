# -*- coding: utf-8 -*-
"""
Linked Element Box - DQT
Pick an element in a linked model and frame it with a section box in a 3D view.

The section box is applied to a 3D view that belongs to the CURRENT user
("DQT Link Box - <username>"), created on first use. This avoids every user
fighting over one shared "{3D}" view (which causes worksharing ownership
conflicts).
"""
from pyrevit import revit, DB, UI
from pyrevit.revit import Transaction

uidoc = revit.uidoc
doc = revit.doc


def _user_view_name():
    return "DQT Link Box - {}".format(doc.Application.Username)


def _find_user_view(name):
    for v in DB.FilteredElementCollector(doc).OfClass(DB.View3D):
        if v.IsTemplate:
            continue
        try:
            if v.Name == name:
                return v
        except:
            pass
    return None


def _first_3d_view_type():
    for t in DB.FilteredElementCollector(doc).OfClass(DB.ViewFamilyType):
        if t.ViewFamily == DB.ViewFamily.ThreeDimensional:
            return t
    return None


try:
    # Step 1: pick an element inside a linked model
    linked_ref = uidoc.Selection.PickObject(
        UI.Selection.ObjectType.LinkedElement,
        "Pick an element in the linked model")
    link_instance = doc.GetElement(linked_ref.ElementId)

    if not isinstance(link_instance, DB.RevitLinkInstance):
        UI.TaskDialog.Show("Error", "The selected object is not a RevitLinkInstance.")
    else:
        link_doc = link_instance.GetLinkDocument()
        linked_element = link_doc.GetElement(linked_ref.LinkedElementId)

        bbox = linked_element.get_BoundingBox(None)
        if not bbox:
            UI.TaskDialog.Show("Error", "Could not get the BoundingBox of the element.")
        else:
            # Step 2: transform all 8 corners to host coords so a rotated link
            # still yields a valid axis-aligned box.
            transform = link_instance.GetTotalTransform()
            pts = []
            for ix in (bbox.Min.X, bbox.Max.X):
                for iy in (bbox.Min.Y, bbox.Max.Y):
                    for iz in (bbox.Min.Z, bbox.Max.Z):
                        pts.append(transform.OfPoint(DB.XYZ(ix, iy, iz)))
            xs = [p.X for p in pts]
            ys = [p.Y for p in pts]
            zs = [p.Z for p in pts]
            off = 0.5  # ~150 mm padding each side
            bbox_transformed = DB.BoundingBoxXYZ()
            bbox_transformed.Min = DB.XYZ(min(xs) - off, min(ys) - off, min(zs) - off)
            bbox_transformed.Max = DB.XYZ(max(xs) + off, max(ys) + off, max(zs) + off)

            # Step 3: get this user's own 3D view (create on first use)
            view_name = _user_view_name()
            target_view = _find_user_view(view_name)
            created = False

            with Transaction("DQT - Link Box (per-user 3D view)"):
                if target_view is None:
                    vft = _first_3d_view_type()
                    if vft is None:
                        raise Exception("Could not find a 3D View Family Type.")
                    target_view = DB.View3D.CreateIsometric(doc, vft.Id)
                    created = True
                    try:
                        target_view.Name = view_name
                    except:
                        pass  # keep default name if it clashes
                target_view.SetSectionBox(bbox_transformed)
                try:
                    target_view.IsSectionBoxActive = True
                except:
                    pass

            # Activate after the transaction is closed
            uidoc.ActiveView = target_view

            msg = "Section Box applied to your own 3D view:\n{}".format(
                target_view.Name)
            if created:
                msg += "\n(New view created for user: {})".format(
                    doc.Application.Username)
            UI.TaskDialog.Show("Success", msg)

except Exception as e:
    UI.TaskDialog.Show("Error", "Could not complete: {}".format(str(e)))
