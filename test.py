# -*- coding: utf-8 -*-
"""
FULL-DOC AUTO DIAGNOSTIC. No ID list needed. Scans every curtain wall in
model, finds its dependent-link parent basic wall (if any), flags any
whose real location falls outside that parent's own line span.

Run inside Revit via pyRevit. Needs revit.doc (live Revit session).
"""
from Autodesk.Revit.DB import Wall, FilteredElementCollector, ElementClassFilter
from pyrevit import revit

doc = revit.doc

all_walls = list(FilteredElementCollector(doc).OfClass(Wall).WhereElementIsNotElementType())
basic_walls = [w for w in all_walls if str(w.WallType.Kind).lower() == "basic"]
curtain_walls = [w for w in all_walls if str(w.WallType.Kind).lower() == "curtain"]
wall_class_filter = ElementClassFilter(Wall)


def describe(e):
    if e is None:
        return "None"
    try:
        type_name = getattr(doc.GetElement(e.GetTypeId()), "Name", "?")
    except Exception:
        type_name = "?"
    try:
        cat = e.Category.Name if e.Category else "?"
    except Exception:
        cat = "?"
    return "Id={} Cat={} Type={}".format(e.Id.IntegerValue, cat, type_name)


def wall_line_xy(w):
    """Returns ((x0,y0),(x1,y1)) or None."""
    try:
        c = w.Location.Curve
        p0, p1 = c.GetEndPoint(0), c.GetEndPoint(1)
        return (p0.X, p0.Y), (p1.X, p1.Y)
    except Exception:
        return None


def unit_dir_and_length(p0, p1):
    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
    mag = (dx * dx + dy * dy) ** 0.5
    if mag < 1e-9:
        return None
    return (dx / mag, dy / mag), mag


def perp_and_along(pt, line_start, direction, length):
    vx = pt[0] - line_start[0]
    vy = pt[1] - line_start[1]
    perp = abs(vx * direction[1] - vy * direction[0])
    along = vx * direction[0] + vy * direction[1]
    ok = -1.0 <= along <= length + 1.0
    return perp, along, ok


def parent_basic_wall_of(curtain_wall):
    """Reverse lookup: which basic wall (if any) has this curtain wall
    as a GetDependentElements() member."""
    for bw in basic_walls:
        try:
            dep_ids = bw.GetDependentElements(wall_class_filter)
            if curtain_wall.Id in dep_ids:
                return bw
        except Exception:
            continue
    return None


print("=" * 78)
print("CURTAIN WALLS: {}   BASIC WALLS: {}".format(len(curtain_walls), len(basic_walls)))
print("=" * 78)

mismatches = []

for cw in curtain_walls:
    line = wall_line_xy(cw)
    if line is None:
        continue
    p0, p1 = line
    mid = ((p0[0] + p1[0]) / 2.0, (p0[1] + p1[1]) / 2.0)

    parent = parent_basic_wall_of(cw)

    if parent is None:
        best_bw, best_perp = None, 1e9
        for bw in basic_walls:
            bline = wall_line_xy(bw)
            if bline is None:
                continue
            ud = unit_dir_and_length(bline[0], bline[1])
            if ud is None:
                continue
            direction, length = ud
            perp, along, ok = perp_and_along(mid, bline[0], direction, length)
            if ok and perp < best_perp:
                best_perp = perp
                best_bw = bw
        print("[NO DEPENDENT-LINK] {}".format(describe(cw)))
        print("    location mid=({:.3f},{:.3f})".format(mid[0], mid[1]))
        if best_bw is not None:
            print("    nearest basic wall by geometry: {}  (perp={:.3f} ft)".format(
                describe(best_bw), best_perp))
        else:
            print("    no basic wall geometrically close either.")
        mismatches.append(cw)
        continue

    pline = wall_line_xy(parent)
    ud = unit_dir_and_length(pline[0], pline[1])
    if ud is None:
        continue
    direction, length = ud
    perp, along, ok = perp_and_along(mid, pline[0], direction, length)

    if not ok or perp > 1.0:
        print("[HOST MISMATCH] {}".format(describe(cw)))
        print("    dependent-link parent: {}".format(describe(parent)))
        print("    parent line: ({:.3f},{:.3f}) to ({:.3f},{:.3f})  length={:.3f} ft".format(
            pline[0][0], pline[0][1], pline[1][0], pline[1][1], length))
        print("    curtain wall location falls at along={:.3f} perp={:.3f} "
              "-- OUTSIDE parent's own span.".format(along, perp))
        mismatches.append(cw)

print("=" * 78)
print("SUMMARY: {} curtain wall(s) with no/mismatched host link out of {} total.".format(
    len(mismatches), len(curtain_walls)))
for cw in mismatches:
    print("  -> {}".format(describe(cw)))
print("=" * 78)