# -*- coding: utf-8 -*-
"""
WALL-BY-WALL EXPORT
Exports Basic Walls and unpacked Stacked Walls individually.
Openings (doors, windows, storefronts) are mapped to their specific host wall.
"""
from Autodesk.Revit.DB import (
    FilteredElementCollector, Wall, BuiltInParameter, LocationPoint, XYZ,
    Level, BuiltInCategory, ElementId, ElementClassFilter, Opening
)
from pyrevit import revit, forms
import csv
import os
import codecs
import json
import math

doc = revit.doc
uidoc = revit.uidoc

# ========== UI OUTPUT SELECTOR ==========
import clr
clr.AddReference('System.Windows.Forms')
from System.Windows.Forms import FolderBrowserDialog, DialogResult

dialog = FolderBrowserDialog()
dialog.Description = "Select Output Folder for Revit Walls"

initial_dir = os.path.join(os.path.expanduser("~"), "Desktop")
if not os.path.exists(initial_dir):
    initial_dir = os.path.expanduser("~")
dialog.SelectedPath = initial_dir

result = dialog.ShowDialog()
if result == DialogResult.OK:
    OUTPUT_DIR = dialog.SelectedPath
    print("Selected output folder: " + OUTPUT_DIR)
else:
    print("No folder selected. Exiting...")
    import sys
    sys.exit(0)

WALLS_FILE    = "walls.csv"
OPENINGS_FILE = "wall_openings.csv"
MAPPING_FILE  = "wall_mapping.csv"
WALLS_PATH    = os.path.join(OUTPUT_DIR, WALLS_FILE)
OPENINGS_PATH = os.path.join(OUTPUT_DIR, OPENINGS_FILE)
MAPPING_PATH  = os.path.join(OUTPUT_DIR, MAPPING_FILE)

if not os.path.isdir(OUTPUT_DIR):
    os.makedirs(OUTPUT_DIR)


SHEATHING_THICKNESS_IN = 0.625
STUD_DEPTH_IN = 6.0
PANEL_TOTAL_THICKNESS_IN = SHEATHING_THICKNESS_IN + STUD_DEPTH_IN

# ========== HELPERS ==========
try:
    basestring
except NameError:
    basestring = str

def rnum(v, nd=4):
    try: return round(float(v), nd)
    except: return ""

def xyz_str(p, nd=4):
    if not p: return ""
    return "({},{},{})".format(rnum(p.X, nd), rnum(p.Y, nd), rnum(p.Z, nd))

def get_bip(name):
    try: return getattr(BuiltInParameter, name)
    except: return None

def get_param(elem, key):
    if key is None: return None
    if not isinstance(key, basestring):
        try:
            p = elem.get_Parameter(key)
            if p: return p
        except: pass
    if isinstance(key, basestring):
        try:
            p = elem.LookupParameter(key)
            if p: return p
        except: pass
    return None

def get_param_val(elem, key, as_string=False):
    p = get_param(elem, key)
    if not p: return ""
    try:
        return p.AsValueString() if as_string else p.AsDouble()
    except:
        try: return p.AsInteger()
        except:
            try: return p.AsString() or ""
            except: return ""

def level_name(elem):
    try:
        lvl_id = elem.LevelId
        if lvl_id and lvl_id.IntegerValue > 0:
            lvl = doc.GetElement(lvl_id)
            return getattr(lvl, "Name", "")
    except: pass
    return (get_param_val(elem, get_bip("FAMILY_LEVEL_PARAM"), as_string=True)
            or get_param_val(elem, "Level", as_string=True)
            or "")


    
def get_single_wall_geometry(wall):
    """
    Compute the true visual-left -> visual-right extent of a single wall.
    Restored to use the pure Analytical Line to prevent projection gaps.
    """
    if not wall: return None

    lc = wall.Location.Curve
    p0 = lc.GetEndPoint(0)
    p1 = lc.GetEndPoint(1)
    
    try:
        normal = wall.Orientation
        up     = XYZ(0, 0, 1)
        visual_right_dir = normal.CrossProduct(up).Normalize()
    except Exception:
        visual_right_dir = XYZ(1, 0, 0)

    all_pts = [p0, p1]
    start_pt = min(all_pts, key=lambda p: p.DotProduct(visual_right_dir))
    end_pt   = max(all_pts, key=lambda p: p.DotProduct(visual_right_dir))
    vec = (end_pt - start_pt).Normalize()
    
    # Safely get Z limits without modifying the X/Y physical span
    try:
        bb = wall.get_BoundingBox(None)
        if bb:
            min_z = bb.Min.Z
            max_z = bb.Max.Z
        else:
            min_z = min(p.Z for p in all_pts)
            h = wall.get_Parameter(BuiltInParameter.WALL_USER_HEIGHT_PARAM).AsDouble()
            max_z = min_z + (h if h else 10.0)
    except Exception:
        min_z = min(p.Z for p in all_pts)
        max_z = min_z

    analytical_length = lc.Length

    # Re-map the start and end points to the true absolute Z plane
    start_pt = XYZ(start_pt.X, start_pt.Y, min_z)
    end_pt   = XYZ(end_pt.X, end_pt.Y, min_z)

    return {
        'start':     start_pt,
        'end':       end_pt,
        'direction': vec,
        'height':    max_z - min_z,
        'min_z':     min_z,
        'length':    analytical_length
    }

def _normalize_xy(v):
    mag = (v.X * v.X + v.Y * v.Y) ** 0.5
    if mag < 1e-9:
        return None
    return XYZ(v.X / mag, v.Y / mag, 0.0)

def find_group_for_opening(opening, group_data, wall_to_group):
    """
    Return the group-dict that an opening (door/window/curtain wall) belongs to.

    Priority order:
      1. opening.Host is in wall_to_group           (exact hosted match)
      2. HOST_ID_PARAM is in wall_to_group          (Stacked/curtain fallback)
      3. opening's own id is in wall_to_group       (self-hosted curtain wall)
      4. Geometric search: prefer walls whose SEGMENT actually contains the
         opening's projection; break ties by perpendicular distance. Two
         colinear walls used to both score perp=0 and whichever was first
         in group_data won -- a wrong pick sent the opening's dist_along
         projection onto a different wall's frame, and the cutout never
         intersected the correct panel.
    """
    try:
        host = opening.Host
        if host:
            g = wall_to_group.get(host.Id.IntegerValue)
            if g is not None: return g
    except Exception: pass
    try:
        hip = opening.get_Parameter(BuiltInParameter.HOST_ID_PARAM)
        if hip:
            g = wall_to_group.get(hip.AsElementId().IntegerValue)
            if g is not None: return g
    except Exception: pass
    try:
        g = wall_to_group.get(opening.Id.IntegerValue)
        if g is not None: return g
    except Exception: pass

    # --- Geometric fallback ---
    try:
        if isinstance(opening, Wall):
            lc = opening.Location.Curve
            pt = lc.Evaluate(0.5, True)
        elif isinstance(opening, Opening):
            # Native wall openings don't have Location.Point; derive a
            # midpoint from BoundaryRect corners.
            try:
                _rect = list(opening.BoundaryRect)
                _p0, _p1 = _rect[0], _rect[1]
                pt = XYZ((_p0.X + _p1.X) / 2.0,
                         (_p0.Y + _p1.Y) / 2.0,
                         (_p0.Z + _p1.Z) / 2.0)
            except Exception:
                pt = opening.Location.Point  # last-resort; will raise if unset
        else:
            pt = opening.Location.Point

        _op_label = "{} (id={})".format(
            opening.Category.Name if opening.Category else type(opening).__name__,
            opening.Id.IntegerValue)

        # Tiered scoring. Perpendicular distance dominates -- a coplanar wall
        # ALWAYS beats a non-coplanar wall, even if the non-coplanar one
        # happens to contain the projection. (The previous version was picking
        # perp=66 ft SEG-HITs over perp=0.05 ft near-misses, which sent every
        # curtain wall to the wrong facade.)
        #
        # Tier 0: coplanar AND projection lands inside the segment  (perfect)
        # Tier 1: coplanar, projection past the endpoint             (near-hit)
        # Tier 2: not coplanar, but projection lands in segment      (weak)
        # Tier 3: not coplanar and out-of-segment                    (worst)
        # Within a tier, smaller perp wins.
        _COPLANAR_PERP_FT = 0.5   # ~6" perp to count as on the same plane
        _tol_end          = 0.1   # ~1" forgiveness on segment ends

        _candidates = []
        for gd in group_data:
            geo = gd['geo']
            vx  = pt.X - geo['start'].X
            vy  = pt.Y - geo['start'].Y
            d   = geo['direction']
            perp   = abs(vx * d.Y - vy * d.X)
            along  = vx * d.X + vy * d.Y
            in_seg = (-_tol_end <= along <= geo['length'] + _tol_end)
            coplanar = perp < _COPLANAR_PERP_FT
            if   coplanar and in_seg: tier = 0
            elif coplanar:            tier = 1
            elif in_seg:              tier = 2
            else:                     tier = 3
            _candidates.append((tier, perp, gd, in_seg, coplanar))

        _candidates.sort(key=lambda c: (c[0], c[1]))
        _tier, _perp, chosen, _in_seg, _coplanar = _candidates[0]

        _tag = {
            0: "SEG-HIT coplanar",
            1: "COPLANAR past-endpoint (projection just outside segment)",
            2: "SEG-HIT non-coplanar (perp large -- suspicious)",
            3: "PERP-ONLY (no coplanar wall, no wall contains projection!)",
        }[_tier]
        print("  [FALLBACK] {} -> {}  ({}, perp={:.4f} ft)".format(
            _op_label, chosen['label'], _tag, _perp))
        return chosen
    except Exception as _e:
        try:
            print("  [FALLBACK] opening id={} -> group_data[0] "
                  "(exception in fallback: {})".format(
                      opening.Id.IntegerValue, _e))
        except Exception:
            pass
        return group_data[0]


# ========== GET SELECTED WALLS ==========
sel_ids = list(uidoc.Selection.GetElementIds())
if not sel_ids:
    forms.alert("Please select one or more walls before running this tool.", exitscript=True)

selected_walls    = []
selected_wall_ids = set()
for eid in sel_ids:
    elem = doc.GetElement(eid)
    if isinstance(elem, Wall):
        selected_walls.append(elem)
        selected_wall_ids.add(elem.Id.IntegerValue)

if not selected_walls:
    forms.alert("No walls found in the current selection.", exitscript=True)

print("Processing {} selected walls...".format(len(selected_walls)))

basic_walls = []
for wall in selected_walls:
    try:
        kind_name = str(wall.WallType.Kind).lower()
        if kind_name == "basic":
            basic_walls.append(wall)
        elif kind_name == "stacked":
            # Extract Basic Walls from inside the Stacked Wall
            member_ids = wall.GetStackedWallMemberIds()
            for mid in member_ids:
                member_wall = doc.GetElement(mid)
                if member_wall and isinstance(member_wall, Wall):
                    basic_walls.append(member_wall)
                    selected_wall_ids.add(mid.IntegerValue)
            print("  [INFO] Unpacked {} basic walls from Stacked Wall {}".format(len(member_ids), wall.Id.IntegerValue))
    except:
        basic_walls.append(wall)

print("  {} basic walls to analyze".format(len(basic_walls)))

# ========== PREPARE WALLS FOR INDIVIDUAL EXPORT ==========
print("\nPreparing walls for individual export...")

_group_data   = []
_wall_to_group = {}

for wall in basic_walls:
    _geo = get_single_wall_geometry(wall)
    if not _geo: continue
    
    _cid = wall.Id.IntegerValue
    _label = "Wall_{}".format(_cid)
    
    _gd = {'walls': [wall], 'geo': _geo, 'id': _cid, 'label': _label}
    _group_data.append(_gd)
    _wall_to_group[_cid] = _gd

print("  {} individual wall(s) queued for export.".format(len(_group_data)))

# ========== MERGE COPLANAR WALLS INTO CONTINUOUS FACADES ==========
# Revit users often draw a single physical facade as several separate walls
# with no offset between them. Panel placement can't span an artificial seam,
# so tall panels get chopped short. Fold every run of perfectly-aligned walls
# into one virtual facade before export.
#
# Two seam directions are supported:
#
#   HORIZONTAL SEAM  -- side-by-side along the facade length. Two walls have
#     the same base/top Z, meet at an XY endpoint, and lie on the same
#     infinite line in plan. Merging extends the length; height stays.
#
#   VERTICAL STACK   -- one wall sits on top of another (per-floor walls, or
#     the members Revit unpacks out of a Stacked Wall). Two walls share the
#     same XY footprint (start and end match in X and Y), and the top of one
#     lands on the base of the other in Z. Merging keeps the footprint and
#     sums the heights.
#
# In both cases all of the following must also match:
#   - wall thickness (Width parameter)
#   - outward normal (Orientation)
#   - visual left->right direction axis
# If anything fails, walls stay separate -- so intentional insets, steps,
# thickness changes, and gaps are always preserved.


_TOL_DIST   = 0.05     # ~5/8 inch. Loose enough for typical Revit noise;
                       # tight enough that a real inset (> ~1 inch) stays split.
_TOL_PERP   = 0.05     # colinearity in plan, same generosity
_TOL_THICK  = 0.001    # in feet
_TOL_DOT    = 0.9995   # ~1.8 degrees of angular slack
 
 
def _shared_type_checks(gd_a, gd_b):
    """Thickness + normal + direction-axis. Common to any merge kind."""
    ga, gb = gd_a['geo'], gd_b['geo']
 
    if ga['direction'].DotProduct(gb['direction']) < _TOL_DOT:
        return False
 
    try:
        if abs(gd_a['walls'][0].Width - gd_b['walls'][0].Width) > _TOL_THICK:
            return False
    except Exception:
        return False
 
    try:
        n_a = gd_a['walls'][0].Orientation
        n_b = gd_b['walls'][0].Orientation
        if n_a.DotProduct(n_b) < _TOL_DOT:
            return False
    except Exception:
        return False
 
    return True
 
 
def _proj_on_dir(pt, direction):
    """Signed scalar position of pt along the horizontal direction axis."""
    return pt.X * direction.X + pt.Y * direction.Y
 
 
def _perp_from_line(line_start, line_dir, pt):
    """Perpendicular (in-plan) distance from pt to the infinite line through
    line_start with direction line_dir."""
    _vx = pt.X - line_start.X
    _vy = pt.Y - line_start.Y
    return abs(_vx * line_dir.Y - _vy * line_dir.X)
 
 
def _x_extent(geo):
    """Projected (X-axis) low/high of a wall along its direction axis."""
    _a = _proj_on_dir(geo['start'], geo['direction'])
    _b = _proj_on_dir(geo['end'],   geo['direction'])
    return (min(_a, _b), max(_a, _b))
 
 
def _z_extent(geo):
    return (geo['min_z'], geo['min_z'] + geo['height'])
 
 
def _intervals_touch_or_overlap(a_lo, a_hi, b_lo, b_hi, tol):
    """True iff two 1D intervals [a_lo,a_hi] and [b_lo,b_hi] overlap or are
    within `tol` of touching."""
    return (a_hi + tol >= b_lo) and (b_hi + tol >= a_lo)
 
 
def _coplanar_mergeable(gd_a, gd_b):
    """Return 'coplanar' if the two groups can be merged, or None if not.

    The bounding-rectangle union in _merge_two_coplanar_groups only produces
    a physically correct wall when the two source rectangles perfectly tile
    the bounding rectangle -- with no phantom (uncovered) area.  That
    requires one of the axes to match exactly:

        HORIZONTAL SEAM : Z ranges match (same base AND top). X touches or
                          overlaps. Union spans the total length at the
                          shared height. No phantom.

        VERTICAL STACK  : X ranges match (same left AND right). Z touches
                          or overlaps. Union stacks the two Z ranges over
                          the shared footprint. No phantom.

        SAME-PLACE      : both match. Duplicates / coplanar construction
                          layers. Still merges cleanly.

    Any other pattern -- adjacent walls of different heights, brick-pattern
    offsets, stepped facades -- would create a phantom strip inside the
    merged bounding rectangle. Panel placement would fill that strip with
    panels even though no wall is physically there, which is exactly the
    "panels reach the tallest part of the facade even where the wall is
    shorter" symptom. Those pairs stay unmerged; each wall keeps its own
    correct height and gets its own panel layout.
    """
    if not _shared_type_checks(gd_a, gd_b):
        return None

    ga, gb = gd_a['geo'], gd_b['geo']

    # colinear in plan
    if _perp_from_line(ga['start'], ga['direction'], gb['start']) > _TOL_PERP:
        return None
    if _perp_from_line(ga['start'], ga['direction'], gb['end']) > _TOL_PERP:
        return None

    _ax_lo, _ax_hi = _x_extent(ga)
    _bx_lo, _bx_hi = _x_extent(gb)
    _az_lo, _az_hi = _z_extent(ga)
    _bz_lo, _bz_hi = _z_extent(gb)

    _x_matches   = (abs(_ax_lo - _bx_lo) < _TOL_DIST and
                    abs(_ax_hi - _bx_hi) < _TOL_DIST)
    _z_matches   = (abs(_az_lo - _bz_lo) < _TOL_DIST and
                    abs(_az_hi - _bz_hi) < _TOL_DIST)
    _x_connects  = _intervals_touch_or_overlap(_ax_lo, _ax_hi,
                                               _bx_lo, _bx_hi, _TOL_DIST)
    _z_connects  = _intervals_touch_or_overlap(_az_lo, _az_hi,
                                               _bz_lo, _bz_hi, _TOL_DIST)

    # Horizontal seam: Z matches, X touches/overlaps.
    if _z_matches and _x_connects:
        return "coplanar"
    # Vertical stack: X matches, Z touches/overlaps.
    if _x_matches and _z_connects:
        return "coplanar"

    return None
 
 
def _merge_two_coplanar_groups(gd_a, gd_b, kind):
    """Combine two aligned groups into the bounding rectangle of their union by projecting onto a perfect baseline."""
    _walls = gd_a['walls'] + gd_b['walls']
    ga, gb = gd_a['geo'], gd_b['geo']
    
    # 1. Adopt the first wall's geometry as the absolute truth baseline
    _base_pt = ga['start']
    _d = ga['direction']
    
    # 2. Gather all 4 original endpoints
    _pts = [ga['start'], ga['end'], gb['start'], gb['end']]
    
    # 3. Mathematically project all points onto the pure baseline vector
    # This strips out any microscopic perpendicular drift between the walls
    _projections = [ (p.X - _base_pt.X) * _d.X + (p.Y - _base_pt.Y) * _d.Y for p in _pts ]
    
    _min_proj = min(_projections)
    _max_proj = max(_projections)
    
    _new_length = _max_proj - _min_proj
    
    # 4. Construct perfectly straight new endpoints firmly on the idealized line
    _new_start_x = _base_pt.X + _d.X * _min_proj
    _new_start_y = _base_pt.Y + _d.Y * _min_proj
    
    _new_end_x = _base_pt.X + _d.X * _max_proj
    _new_end_y = _base_pt.Y + _d.Y * _max_proj
    
    # Z extent remains a straightforward union
    _new_min_z = min(ga['min_z'], gb['min_z'])
    _new_top   = max(ga['min_z'] + ga['height'], gb['min_z'] + gb['height'])
    _new_height = _new_top - _new_min_z
    
    _new_start = XYZ(_new_start_x, _new_start_y, _new_min_z)
    _new_end   = XYZ(_new_end_x,   _new_end_y,   _new_min_z)
    
    _new_geo = {
        'start':     _new_start,
        'end':       _new_end,
        'direction': _d,
        'height':    _new_height,
        'min_z':     _new_min_z,
        'length':    _new_length,
    }
    
    _cid = min(w.Id.IntegerValue for w in _walls)
    _label = "Facade_{}".format(_cid)
    return {'walls': _walls, 'geo': _new_geo, 'id': _cid, 'label': _label}
 
 
def _merge_coplanar_runs(group_data, wall_to_group):
    """Iteratively fold every aligned run into one group. Re-scanning from
    i=0 after any merge lets chains and mixed grids collapse all the way
    through in as many passes as it takes."""
    _merges = 0
    _passes = 0
    while _passes < 200:
        _passes += 1
        _merged_this_pass = False
        _i = 0
        while _i < len(group_data):
            _j = _i + 1
            _merged_i = False
            while _j < len(group_data):
                _kind = _coplanar_mergeable(group_data[_i], group_data[_j])
                if _kind:
                    _new_gd = _merge_two_coplanar_groups(
                        group_data[_i], group_data[_j], _kind)
                    group_data.pop(_j)
                    group_data.pop(_i)
                    group_data.insert(_i, _new_gd)
                    for _w in _new_gd['walls']:
                        wall_to_group[_w.Id.IntegerValue] = _new_gd
                    print("  [MERGE] {} walls -> {} "
                          "({:.2f} ft long x {:.2f} ft tall, base_z={:.2f})".format(
                        len(_new_gd['walls']), _new_gd['label'],
                        _new_gd['geo']['length'], _new_gd['geo']['height'],
                        _new_gd['geo']['min_z']))
                    _merges += 1
                    _merged_this_pass = True
                    _merged_i = True
                    break
                _j += 1
            if not _merged_i:
                _i += 1
        if not _merged_this_pass:
            break
 
    return _merges
 
 
def _diagnose_near_misses(group_data, max_report=8):
    """When no merges happen, scan all pairs and report the ones that were
    almost-mergeable, showing which criterion each failed. Helps decide
    whether to loosen a tolerance or clean up the Revit model."""
    _misses = []
    _n = len(group_data)
    for _i in range(_n):
        for _j in range(_i + 1, _n):
            gd_a = group_data[_i]
            gd_b = group_data[_j]
            ga = gd_a['geo']
            gb = gd_b['geo']
            _reasons = []
            _score = 0.0
 
            # thickness
            try:
                _dt = abs(gd_a['walls'][0].Width - gd_b['walls'][0].Width)
                if _dt > _TOL_THICK:
                    _reasons.append("thickness diff {:.4f} ft".format(_dt))
                    _score += 1000.0  # a thickness mismatch is decisive
            except Exception:
                _reasons.append("could not read thickness")
                _score += 1000.0
 
            # direction axis
            _dd = ga['direction'].DotProduct(gb['direction'])
            if _dd < _TOL_DOT:
                _reasons.append("direction dot {:.5f} (need >= {:.4f})"
                                .format(_dd, _TOL_DOT))
                _score += (1.0 - _dd) * 100
 
            # normal
            try:
                _dn = gd_a['walls'][0].Orientation.DotProduct(
                      gd_b['walls'][0].Orientation)
                if _dn < _TOL_DOT:
                    _reasons.append("normal dot {:.5f} (need >= {:.4f})"
                                    .format(_dn, _TOL_DOT))
                    _score += (1.0 - _dn) * 100
            except Exception:
                pass
 
            # colinearity
            _p1 = _perp_from_line(ga['start'], ga['direction'], gb['start'])
            _p2 = _perp_from_line(ga['start'], ga['direction'], gb['end'])
            _perp_max = max(_p1, _p2)
            if _perp_max > _TOL_PERP:
                _reasons.append("perp offset {:.4f} ft (need <= {:.3f})"
                                .format(_perp_max, _TOL_PERP))
                _score += _perp_max
 
            # X interval gap
            _ax_lo, _ax_hi = _x_extent(ga)
            _bx_lo, _bx_hi = _x_extent(gb)
            _x_gap = max(_bx_lo - _ax_hi, _ax_lo - _bx_hi, 0.0)
            if _x_gap > _TOL_DIST:
                _reasons.append("X gap {:.4f} ft".format(_x_gap))
                _score += _x_gap
 
            # Z interval gap
            _az_lo, _az_hi = _z_extent(ga)
            _bz_lo, _bz_hi = _z_extent(gb)
            _z_gap = max(_bz_lo - _az_hi, _az_lo - _bz_hi, 0.0)
            if _z_gap > _TOL_DIST:
                _reasons.append("Z gap {:.4f} ft".format(_z_gap))
                _score += _z_gap
 
            # Phantom-area diagnosis: rectangles overlap/touch but neither
            # X nor Z ranges match exactly. Merging would create a step or
            # L-shape and panel_calculator would fill the "missing" corner
            # with phantom panels. Reject and flag for the user.
            if _x_gap <= _TOL_DIST and _z_gap <= _TOL_DIST:
                _x_matches = (abs(_ax_lo - _bx_lo) < _TOL_DIST and
                              abs(_ax_hi - _bx_hi) < _TOL_DIST)
                _z_matches = (abs(_az_lo - _bz_lo) < _TOL_DIST and
                              abs(_az_hi - _bz_hi) < _TOL_DIST)
                if not (_x_matches or _z_matches):
                    _dz_base = abs(_az_lo - _bz_lo)
                    _dz_top  = abs(_az_hi - _bz_hi)
                    _dx_lo   = abs(_ax_lo - _bx_lo)
                    _dx_hi   = abs(_ax_hi - _bx_hi)
                    _reasons.append(
                        "would create phantom area (bases differ by {:.2f} ft, "
                        "tops by {:.2f} ft; left ends by {:.2f} ft, right ends "
                        "by {:.2f} ft) -- either Z or X range must match "
                        "exactly to merge".format(
                            _dz_base, _dz_top, _dx_lo, _dx_hi))
                    _score += min(_dz_base + _dz_top, _dx_lo + _dx_hi)
 
            # Only report pairs where every failure was small -- otherwise it's
            # not a near-miss, it's two unrelated walls.
            if not _reasons or _score > 3.0:
                continue
 
            _misses.append((_score, gd_a['label'], gd_b['label'], _reasons))
 
    _misses.sort(key=lambda t: t[0])
    if not _misses:
        print("  No near-miss pairs found -- walls are truly independent.")
        return
    print("  {} near-miss pair(s) that ALMOST merged. Top {}:".format(
        len(_misses), min(len(_misses), max_report)))
    for _score, _la, _lb, _reasons in _misses[:max_report]:
        print("    {} <-> {}".format(_la, _lb))
        for _r in _reasons:
            print("      - {}".format(_r))
 
 
_pre_merge_count = len(_group_data)
print("\nMerging coplanar aligned walls into continuous facades...")
_merge_count = _merge_coplanar_runs(_group_data, _wall_to_group)
_post_merge_count = len(_group_data)
if _post_merge_count < _pre_merge_count:
    print("  Collapsed {} walls into {} facade group(s) ({} merge(s) applied).".format(
        _pre_merge_count, _post_merge_count, _merge_count))
else:
    print("  No coplanar runs collapsed.")
    _diagnose_near_misses(_group_data)

# ========== EXPORT WALLS CSV ==========
print("\nExporting walls to CSV...")

try:
    with codecs.open(WALLS_PATH, mode="w", encoding="utf-8") as f:
        csv_writer = csv.writer(f)
        csv_writer.writerow([
            "WallId","FacadeId","WallKind","TypeName","FamilyName","Function","IsStructural",
            "Width(ft)","Length(ft)","UnconnectedHeight(ft)","Area(sf)","Volume(cf)",
            "BaseLevel","BaseOffset(ft)","TopConstraint","TopOffset(ft)","LocationLine",
            "CurveType","Start(X,Y,Z)","End(X,Y,Z)","Mid(X,Y,Z)","CurveLength(ft)",
            "ArcRadius(ft)","ArcAngle(rad)","ArcCenter(X,Y,Z)","AxisDir(unit XYZ)","Normal(unit XYZ)",
            "StructFaceOffset(ft)","PanelStartExt(in)","PanelEndExt(in)",
            "Layers","WallCount","LevelElevations(in)",
            "wall_origin_x","wall_origin_y","wall_origin_z",
            "wall_dir_x","wall_dir_y","wall_dir_z",
            "wall_normal_x","wall_normal_y","wall_normal_z",
            "panel_total_thickness_in","sheathing_thickness_in","stud_depth_in"
        ])

        if not _group_data:
            print("  No basic walls to export.")
        else:
            levels        = list(FilteredElementCollector(doc).OfClass(Level))
            level_elev_in = sorted([round(l.Elevation * 12.0, 4) for l in levels])
            
            _all_doc_walls = list(FilteredElementCollector(doc).OfClass(Wall).WhereElementIsNotElementType())

            for _gd in _group_data:
                geo         = _gd['geo']
                combined_id = _gd['id']
                facade_id   = _gd['label']
                wall        = _gd['walls'][0]

                # ---- Corner extension logic for individual walls ----
                # Exclude EVERY wall that's part of this merged group, not just
                # combined_id -- a merged facade may include several Revit walls
                # whose internal seam endpoints must not be mistaken for a
                # perpendicular butt-joint neighbor.
                _excl_ids = set(w.Id.IntegerValue for w in _gd['walls'])
                def _corner_ext(endpoint, wall_dir, excl_ids):
                    _tol = 0.15
                    _ext = 0.0
                    for _aw in _all_doc_walls:
                        if _aw.Id.IntegerValue in excl_ids: continue
                        try: _alc = _aw.Location.Curve
                        except: continue
                        for _k in [0, 1]:
                            _aep = _alc.GetEndPoint(_k)
                            _d2d = XYZ(_aep.X - endpoint.X, _aep.Y - endpoint.Y, 0.0).GetLength()
                            if _d2d < _tol:
                                _adir = (_alc.GetEndPoint(1) - _alc.GetEndPoint(0)).Normalize()
                                if abs(wall_dir.DotProduct(_adir)) < 0.25:
                                    _ext = max(_ext, _aw.WallType.Width / 2.0)
                    return _ext

                _s_ext = _corner_ext(geo['start'], geo['direction'], _excl_ids)
                _e_ext = _corner_ext(geo['end'],   geo['direction'], _excl_ids)

                _panel_start_ext_in = _s_ext * 12
                _panel_end_ext_in   = _e_ext * 12

                length_ft   = geo['length']
                height_ft   = geo['height']
                width_ft    = wall.Width
                wall_type   = doc.GetElement(wall.GetTypeId())
                kind_name   = "Basic"
                type_name   = getattr(wall_type, "Name", "") or ""
                family_name = getattr(wall_type, "FamilyName", "") or ""

                function_str = (get_param_val(wall, "Function", as_string=True) or
                                get_param_val(wall, get_bip("WALL_ATTR_FUNCTION_PARAM"), as_string=True) or "")
                is_struct    = bool(getattr(wall, "Structural", False))
                area_sf      = rnum(length_ft * height_ft)
                vol_cf       = rnum(length_ft * height_ft * width_ft)
                base_lvl     = level_name(wall)
                base_off     = (get_param_val(wall, get_bip("WALL_BASE_OFFSET")) or get_param_val(wall, "Base Offset") or "")
                top_con      = (get_param_val(wall, "Top Constraint", as_string=True) or get_param_val(wall, get_bip("WALL_HEIGHT_TYPE"), as_string=True) or "")
                top_off      = (get_param_val(wall, get_bip("WALL_TOP_OFFSET")) or get_param_val(wall, "Top Offset") or "")
                loc_line     = (get_param_val(wall, get_bip("WALL_KEY_REF_PARAM"), as_string=True) or get_param_val(wall, "Location Line", as_string=True) or "")

                layers_info = []
                try:
                    compound_structure = wall_type.GetCompoundStructure()
                    if compound_structure:
                        for layer in compound_structure.GetLayers():
                            function_name = str(layer.Function)
                            material_id   = layer.MaterialId
                            material_name = "<By Category>"
                            if material_id.IntegerValue > 0:
                                material = doc.GetElement(material_id)
                                material_name = material.Name if material else "<By Category>"
                            thickness_in = round(layer.Width * 12, 3)
                            wraps        = "Yes" if layer.LayerCapFlag else "No"
                            layers_info.append("{} | {} | {} in | Wrap:{}".format(
                                function_name, material_name, thickness_in, wraps))
                except: pass
                layers_str = " || ".join(layers_info) if layers_info else "No Layers"

                wall_normal_xyz = None
                try: wall_normal_xyz = wall.Orientation
                except: pass

                struct_face_offset_ft = 0.0
                try:
                    _cs = wall_type.GetCompoundStructure()
                    if _cs:
                        _ly = list(_cs.GetLayers())
                        try:
                            _ci  = _cs.FirstCoreLayerIndex
                            _lci = _cs.LastCoreLayerIndex
                        except AttributeError:
                            _ci  = next((i for i, l in enumerate(_ly) if "struct" in str(l.Function).lower()), 0)
                            _lci = next((i for i in range(len(_ly)-1, _ci-1, -1) if "struct" in str(_ly[i].Function).lower()), _ci)

                        _ext_nc = sum(_ly[i].Width for i in range(_ci))
                        _ll = str(loc_line).lower()
                        if ("finish" in _ll or "face" in _ll) and "exterior" in _ll and "core" not in _ll: _loc_to_ext = 0.0
                        elif "core" in _ll and "exterior" in _ll: _loc_to_ext = _ext_nc
                        elif "core" in _ll and ("center" in _ll or "centre" in _ll):
                            _cw2 = sum(_ly[i].Width for i in range(_ci, _lci + 1))
                            _loc_to_ext = _ext_nc + _cw2 / 2.0
                        elif "core" in _ll and "interior" in _ll:
                            _cw2 = sum(_ly[i].Width for i in range(_ci, _lci + 1))
                            _loc_to_ext = _ext_nc + _cw2
                        elif "interior" in _ll and ("finish" in _ll or "face" in _ll): _loc_to_ext = width_ft
                        else: _loc_to_ext = width_ft / 2.0
                        struct_face_offset_ft = round(_loc_to_ext - _ext_nc, 6)
                except: pass

                mid_pt = geo['start'] + (geo['direction'] * (length_ft / 2.0))

                csv_writer.writerow([
                    combined_id, facade_id, kind_name, type_name, family_name, function_str, is_struct,
                    rnum(width_ft), rnum(length_ft), rnum(height_ft), area_sf, vol_cf,
                    base_lvl, rnum(base_off), top_con, rnum(top_off), loc_line,
                    "Line", xyz_str(geo['start']), xyz_str(geo['end']), xyz_str(mid_pt), rnum(length_ft),
                    "", "", "", "", xyz_str(wall_normal_xyz) if wall_normal_xyz else "", rnum(struct_face_offset_ft),
                    rnum(_panel_start_ext_in), rnum(_panel_end_ext_in),
                    layers_str, "1", json.dumps(level_elev_in),
                    rnum(geo['start'].X), rnum(geo['start'].Y), rnum(geo['start'].Z),
                    rnum(geo['direction'].X), rnum(geo['direction'].Y), rnum(geo['direction'].Z),
                    rnum(wall_normal_xyz.X) if wall_normal_xyz else "", rnum(wall_normal_xyz.Y) if wall_normal_xyz else "", rnum(wall_normal_xyz.Z) if wall_normal_xyz else "",
                    rnum(PANEL_TOTAL_THICKNESS_IN), rnum(SHEATHING_THICKNESS_IN), rnum(STUD_DEPTH_IN)
                ])

                print("  Wall '{}': {:.2f} ft × {:.2f} ft | id={}".format(
                      facade_id, rnum(length_ft, 2), rnum(height_ft, 2), combined_id))
                
    print("Walls exported successfully to: {}".format(WALLS_PATH))

except IOError as e:
    print("\nERROR: Cannot write to walls.csv - file may be open.")
    raise

# ========== COLLECT OPENINGS FROM SELECTED WALLS ==========
print("\nCollecting openings from selected walls...")

facade_wall_ids = set(selected_wall_ids)

def _hosted_on_facade(elem):
    try:
        host = elem.Host
        if host and host.Id.IntegerValue in facade_wall_ids: return True
    except Exception: pass
    try:
        host_id = elem.get_Parameter(BuiltInParameter.HOST_ID_PARAM)
        if host_id and host_id.AsElementId().IntegerValue in facade_wall_ids: return True
    except Exception: pass
    return False

doors   = [d for d in FilteredElementCollector(doc).OfCategory(BuiltInCategory.OST_Doors)
           .WhereElementIsNotElementType() if _hosted_on_facade(d)]
windows = [w for w in FilteredElementCollector(doc).OfCategory(BuiltInCategory.OST_Windows)
           .WhereElementIsNotElementType() if _hosted_on_facade(w)]

# Native "Wall Opening" elements (Architecture > Opening > Wall). These are
# rectangular cutouts placed directly in a wall -- not a door or window family,
# just a hole. Category OST_SWallRectOpening, class Opening. They need to be
# panelized like any other opening.
wall_openings = []
try:
    _all_openings = list(FilteredElementCollector(doc).OfClass(Opening))
    for _o in _all_openings:
        try:
            _host = _o.Host
            if _host is None: continue
            if _host.Id.IntegerValue not in facade_wall_ids: continue
            # Skip non-rectangular openings (e.g., arc-wall or shaft openings).
            # We only handle straight-wall rectangular cutouts.
            if not getattr(_o, "IsRectBoundary", False): continue
            wall_openings.append(_o)
        except Exception: pass
except Exception as _e:
    print("  Wall opening collection failed: {}".format(_e))

_seen_cw_ids = set()
curtain_walls_hosted = []

# Scenario A: directly selected curtain walls
for _sw in selected_walls:
    try:
        if str(_sw.WallType.Kind).lower() == "curtain":
            curtain_walls_hosted.append(_sw)
            _seen_cw_ids.add(_sw.Id.IntegerValue)
    except Exception: pass

# ---- AUTHORITATIVE PARENT MAP ----
# Before doing anything else, ask every selected basic wall which curtain
# walls it hosts. Revit exposes two APIs for this and they don't always
# agree in real projects:
#   * FindInserts(includeShadows=false, includeEmbeddedWalls=true, ...)
#     is the intended API for embedded curtain walls and is usually the
#     reliable one.
#   * GetDependentElements(ElementClassFilter(Wall)) works as a fallback
#     for models where FindInserts comes back empty.
# We try both and merge the results. This is the source of truth for
# routing openings back to their real host: it handles Scenario A (user
# selected the curtain walls) AND Scenario B (user only selected the
# basic walls) with the same code path, and it protects against the
# geometric fallback's habit of picking a colinear-but-wrong wall when
# several walls are in the selection.
_wall_class_filter = ElementClassFilter(Wall)
_curtain_to_parent = {}  # cw_id -> parent basic_wall element
_findinserts_hits = 0
_getdep_hits = 0
for _bwall in basic_walls:
    # (1) FindInserts -- explicitly returns embedded curtain walls
    try:
        _inserts = _bwall.FindInserts(False, True, True, False)
        for _dep_id in _inserts:
            _dep = doc.GetElement(_dep_id)
            if not (_dep and isinstance(_dep, Wall)): continue
            try:
                if str(_dep.WallType.Kind).lower() == "curtain":
                    if _dep_id.IntegerValue not in _curtain_to_parent:
                        _curtain_to_parent[_dep_id.IntegerValue] = _bwall
                        _findinserts_hits += 1
            except Exception: pass
    except Exception: pass
    # (2) GetDependentElements -- backup for models where FindInserts is empty
    try:
        for _dep_id in _bwall.GetDependentElements(_wall_class_filter):
            _dep = doc.GetElement(_dep_id)
            if not (_dep and isinstance(_dep, Wall)): continue
            try:
                if str(_dep.WallType.Kind).lower() == "curtain":
                    if _dep_id.IntegerValue not in _curtain_to_parent:
                        _curtain_to_parent[_dep_id.IntegerValue] = _bwall
                        _getdep_hits += 1
            except Exception: pass
    except Exception: pass

print("  Authoritative parent map: {} curtain wall(s) found "
      "({} via FindInserts, {} via GetDependentElements)".format(
          len(_curtain_to_parent), _findinserts_hits, _getdep_hits))

# Pull in any curtain walls found as dependents that weren't user-selected.
for _cw_id, _bwall in _curtain_to_parent.items():
    if _cw_id in _seen_cw_ids: continue
    _dep = doc.GetElement(ElementId(_cw_id))
    if _dep is not None:
        curtain_walls_hosted.append(_dep)
        _seen_cw_ids.add(_cw_id)

# ---- ROUTE EVERY CURTAIN WALL TO A GROUP ----
# Priority order:
#   1. Authoritative parent (in _curtain_to_parent)  <- fixes the batch bug
#   2. Geometric fallback via find_group_for_opening
# Curtain walls that end up in the fallback with a very large perpendicular
# offset almost certainly don't belong to any of the exported basic walls;
# they get dropped rather than misassigned, with a warning so the user knows
# to check their selection.
_ORPHAN_PERP_LIMIT_FT = 1.0  # >1 ft perpendicular = definitely not embedded in
                             # this wall; typical embedded curtain wall sits
                             # within an inch of the basic wall's centerline.
_routed_via_parent = 0
_routed_via_geometry = 0
_dropped_orphans = []
_kept_curtain_walls = []
for _cw in curtain_walls_hosted:
    _cw_id = _cw.Id.IntegerValue
    if _cw_id in _wall_to_group:
        _kept_curtain_walls.append(_cw)
        continue

    _parent = _curtain_to_parent.get(_cw_id)
    if _parent is not None:
        _parent_id = _parent.Id.IntegerValue
        if _parent_id in _wall_to_group:
            _wall_to_group[_cw_id] = _wall_to_group[_parent_id]
            _routed_via_parent += 1
            _kept_curtain_walls.append(_cw)
            continue

    # Geometric fallback -- but check the perpendicular distance and drop
    # curtain walls that clearly don't sit on any exported basic wall.
    # An embedded curtain wall is coplanar with its host (perp within a few
    # inches; wall thickness at most). Anything with perp > ~1 ft everywhere
    # is on a different wall that isn't in the selection.
    try:
        _cw_lc = _cw.Location.Curve
        _cw_pt = _cw_lc.Evaluate(0.5, True)
        _best_perp = float('inf')
        for _gd in _group_data:
            _geo = _gd['geo']
            _vx = _cw_pt.X - _geo['start'].X
            _vy = _cw_pt.Y - _geo['start'].Y
            _d = _geo['direction']
            _p = abs(_vx * _d.Y - _vy * _d.X)
            if _p < _best_perp: _best_perp = _p
        if _best_perp > _ORPHAN_PERP_LIMIT_FT:
            _dropped_orphans.append((_cw_id, _best_perp))
            continue  # skip this curtain wall entirely
    except Exception:
        pass

    _gd_match = find_group_for_opening(_cw, _group_data, _wall_to_group)
    _wall_to_group[_cw_id] = _gd_match
    _routed_via_geometry += 1
    _kept_curtain_walls.append(_cw)

curtain_walls_hosted = _kept_curtain_walls
print("  Curtain wall routing: {} via authoritative host, {} via geometric fallback"
      .format(_routed_via_parent, _routed_via_geometry))
if _dropped_orphans:
    print("  [WARN] Dropped {} curtain wall(s) that don't sit on any exported basic wall "
          "(perpendicular offset > {:.1f} ft):".format(
              len(_dropped_orphans), _ORPHAN_PERP_LIMIT_FT))
    for _id, _p in _dropped_orphans[:15]:
        print("           id={}  perp={:.2f} ft".format(_id, _p))
    if len(_dropped_orphans) > 15:
        print("           ... {} more".format(len(_dropped_orphans) - 15))
    print("           If these should have been exported, add their host basic "
          "walls to the selection.")

all_openings_list = list(doors) + list(windows) + curtain_walls_hosted + list(wall_openings)
print("  Doors: {} | Windows: {} | Curtain/Storefronts: {} | Wall Openings: {}".format(
    len(doors), len(windows), len(curtain_walls_hosted), len(wall_openings)))
print("  Total openings detected: {}".format(len(all_openings_list)))

# ========== EXPORT OPENINGS CSV ==========
print("\nExporting openings to CSV...")

try:
    with codecs.open(OPENINGS_PATH, mode="w", encoding="utf-8") as f:
        csv_writer = csv.writer(f)
        csv_writer.writerow([
            "OpeningId","OpeningType","Category","TypeName","FamilyName",
            "HostWallId","HostWallType","Level","SillHeight(ft)",
            "Width(ft)","Height(ft)","Thickness(ft)",
            "PositionAlongWall(ft)","LeftEdgeAlongWall(ft)","RightEdgeAlongWall(ft)",
            "Location(X,Y,Z)","FacingOrientation","HandOrientation",
            "FromRoom","ToRoom","Mark","Comments","Area(sf)"
        ])

        if _group_data:
            for opening in all_openings_list:
                try:
                    opening_id = opening.Id.IntegerValue
                    # Show host chain so we know whether the assignment came from
                    # opening.Host, HOST_ID_PARAM, self-id, or the geometric
                    # fallback. If two of these disagree, the export is picking a
                    # wall the opening isn't actually on.
                    _host_ids = []
                    try:
                        _h = opening.Host
                        _host_ids.append(("Host", _h.Id.IntegerValue if _h else None))
                    except Exception: _host_ids.append(("Host", "exc"))
                    try:
                        _hp = opening.get_Parameter(BuiltInParameter.HOST_ID_PARAM)
                        _host_ids.append(("HOST_ID_PARAM",
                                          _hp.AsElementId().IntegerValue if _hp else None))
                    except Exception: _host_ids.append(("HOST_ID_PARAM", "exc"))

                    _gd          = find_group_for_opening(opening, _group_data, _wall_to_group)
                    geo          = _gd['geo']
                    combined_id  = _gd['id']
                    _cwall_elem  = doc.GetElement(ElementId(combined_id))
                    host_wall_type = (getattr(doc.GetElement(_cwall_elem.GetTypeId()), "Name", "") if _cwall_elem else "")

                    _cat = opening.Category.Name if opening.Category else "?"
                    print("  [OPENING] {} id={} host_chain={} -> {} (combined_id={})".format(
                        _cat, opening_id, _host_ids, _gd['label'], combined_id))

                    # ---- Curtain Wall / Storefront ----
                    # ---- Curtain Wall / Storefront ----
                    if isinstance(opening, Wall):
                        _cw_type      = doc.GetElement(opening.GetTypeId())
                        _cw_type_name = getattr(_cw_type, "Name", "") or ""
                        _lc  = opening.Location.Curve
                        _p0  = _lc.GetEndPoint(0)
                        _p1  = _lc.GetEndPoint(1)
                        _d0  = (_p0 - geo['start']).DotProduct(geo['direction'])
                        _d1  = (_p1 - geo['start']).DotProduct(geo['direction'])
                        _left_ft  = min(_d0, _d1)
                        _right_ft = max(_d0, _d1)
                        _w_ft = _lc.Length
                        
                        # --- THE STOREFRONT FIX: PHYSICAL HEIGHT OVERRIDE ---
                        _h_ft = 0.0
                        try:
                            # 1. Trust the physical 3D Bounding Box (fixes level-constrained height=0 bug)
                            bb = opening.get_BoundingBox(None)
                            if bb:
                                _h_ft = bb.Max.Z - bb.Min.Z
                        except: pass
                        
                        if _h_ft <= 0.0:
                            # 2. Fallback to parameters if BB fails
                            for _hbip in [BuiltInParameter.WALL_USER_HEIGHT_PARAM, BuiltInParameter.WALL_ATTR_HEIGHT_PARAM]:
                                try:
                                    _hp = opening.get_Parameter(_hbip)
                                    if _hp and _hp.AsDouble() > 0:
                                        _h_ft = _hp.AsDouble()
                                        break
                                except: pass
                                
                        if _w_ft <= 0 or _h_ft <= 0: 
                            print("  [WARN] Skipping Storefront {} due to 0 width/height".format(opening_id))
                            continue
                        
                        _base_off = 0.0
                        try:
                            _bop = opening.get_Parameter(BuiltInParameter.WALL_BASE_OFFSET)
                            if _bop: _base_off = _bop.AsDouble()
                        except: pass
                        
                        _base_z = min(_p0.Z, _p1.Z) + _base_off
                        _sill_ft = _base_z - geo['min_z']
                        _center  = (_left_ft + _right_ft) / 2.0
                        _mid_xy  = _lc.Evaluate(0.5, True)
                        _loc_pt  = XYZ(_mid_xy.X, _mid_xy.Y, _base_z)
                        _thk_ft  = opening.Width
                        _lvl     = level_name(opening)
                        _mark    = get_param_val(opening, "Mark",     as_string=True)
                        _comments = get_param_val(opening, "Comments", as_string=True)
                        
                        csv_writer.writerow([
                            opening_id, "Storefront/Curtain", "Storefront/Curtain",
                            _cw_type_name, "Curtain Wall",
                            combined_id, host_wall_type, _lvl, rnum(_sill_ft),
                            rnum(_w_ft), rnum(_h_ft), rnum(_thk_ft),
                            rnum(_center), rnum(_left_ft), rnum(_right_ft),
                            xyz_str(_loc_pt), "", "",
                            "", "", _mark, _comments, rnum(_w_ft * _h_ft)
                        ])
                        continue


                    # ---- Wall Opening (native rectangular cutout) ----
                    # These are Autodesk.Revit.DB.Opening instances with
                    # IsRectBoundary = True. They don't carry Location.Point,
                    # dimension parameters, or family/type names -- just a
                    # BoundaryRect of two opposite corners in world XYZ.
                    if isinstance(opening, Opening):
                        try:
                            _rect_pts = list(opening.BoundaryRect)
                        except Exception:
                            print("  [WARN] Skipping Wall Opening {}: no BoundaryRect"
                                  .format(opening_id))
                            continue
                        if not _rect_pts or len(_rect_pts) < 2:
                            print("  [WARN] Skipping Wall Opening {}: BoundaryRect < 2 pts"
                                  .format(opening_id))
                            continue

                        _pa, _pb = _rect_pts[0], _rect_pts[1]
                        _z_lo = min(_pa.Z, _pb.Z)
                        _z_hi = max(_pa.Z, _pb.Z)
                        _h_ft = _z_hi - _z_lo

                        # Project both corners onto the merged facade direction to
                        # get along-wall extent. In-plane the two corners share the
                        # same XY position projected onto direction axis and differ
                        # only in Z, but numerically both projections are computed
                        # and the extremes taken so this stays robust to whichever
                        # order BoundaryRect returns.
                        _d0 = (_pa - geo['start']).DotProduct(geo['direction'])
                        _d1 = (_pb - geo['start']).DotProduct(geo['direction'])
                        _left_ft  = min(_d0, _d1)
                        _right_ft = max(_d0, _d1)
                        _w_ft = _right_ft - _left_ft

                        if _w_ft <= 0 or _h_ft <= 0:
                            print("  [WARN] Skipping Wall Opening {}: zero width/height "
                                  "(w={:.3f}, h={:.3f})".format(opening_id, _w_ft, _h_ft))
                            continue

                        _sill_ft = _z_lo - geo['min_z']
                        _center  = (_left_ft + _right_ft) / 2.0
                        _mid_xy  = XYZ((_pa.X + _pb.X) / 2.0,
                                       (_pa.Y + _pb.Y) / 2.0, _z_lo)
                        _thk_ft  = 0.0  # native openings have no wall-fill thickness
                        _lvl     = level_name(opening)
                        _mark    = get_param_val(opening, "Mark",     as_string=True)
                        _comments = get_param_val(opening, "Comments", as_string=True)

                        csv_writer.writerow([
                            opening_id, "Wall Opening", "Wall Opening",
                            "Rectangular Wall Opening", "Native",
                            combined_id, host_wall_type, _lvl, rnum(_sill_ft),
                            rnum(_w_ft), rnum(_h_ft), rnum(_thk_ft),
                            rnum(_center), rnum(_left_ft), rnum(_right_ft),
                            xyz_str(_mid_xy), "", "",
                            "", "", _mark, _comments, rnum(_w_ft * _h_ft)
                        ])
                        continue


                    # ---- Standard Doors / Windows ----
                    category          = opening.Category.Name if opening.Category else ""
                    opening_type_elem = doc.GetElement(opening.GetTypeId())
                    type_name         = getattr(opening_type_elem, "Name", "") or ""
                    family_name       = (getattr(opening_type_elem, "FamilyName", "") if hasattr(opening_type_elem, "FamilyName") else "")
                    
                    # --- THE CUTOUT FIX: MISSING LOCATION FALLBACK ---
                    loc_pt = None
                    if hasattr(opening.Location, "Point") and opening.Location.Point:
                        loc_pt = opening.Location.Point
                    else:
                        # Curtain Wall Doors masquerading as standard doors have no Point.
                        # Extract their true location from the BoundingBox center.
                        try:
                            bb = opening.get_BoundingBox(None)
                            if bb:
                                loc_pt = XYZ((bb.Min.X + bb.Max.X)/2.0, (bb.Min.Y + bb.Max.Y)/2.0, bb.Min.Z)
                        except: pass
                        
                    if not loc_pt:
                        loc_pt = geo['start'] # Absolute worst-case fallback
                        
                    # Because geo['start'] is now the specific wall's start, this math is perfectly local
                    dist_along = (loc_pt - geo['start']).DotProduct(geo['direction'])


                    def _get_dim(elem, bips, names):
                        sources = [elem]
                        try: sources.append(doc.GetElement(elem.GetTypeId()))
                        except: pass
                        for src in sources:
                            if src is None: continue
                            for bip in bips:
                                try:
                                    p = src.get_Parameter(bip)
                                    if p:
                                        v = p.AsDouble()
                                        if v and v > 0: return v
                                except: pass
                            for nm in names:
                                try:
                                    p = src.LookupParameter(nm)
                                    if p:
                                        v = p.AsDouble()
                                        if v and v > 0: return v
                                except: pass
                        return 0.0

                    WIDTH_BIPS  = [BuiltInParameter.DOOR_WIDTH,  BuiltInParameter.WINDOW_WIDTH, BuiltInParameter.GENERIC_WIDTH, BuiltInParameter.FAMILY_WIDTH_PARAM]
                    HEIGHT_BIPS = [BuiltInParameter.DOOR_HEIGHT, BuiltInParameter.WINDOW_HEIGHT, BuiltInParameter.GENERIC_HEIGHT, BuiltInParameter.FAMILY_HEIGHT_PARAM]
                    WIDTH_NAMES  = ["Width","Rough Width","Nominal Width","Opening Width","Frame Width","Clear Width","w","WIDTH"]
                    HEIGHT_NAMES = ["Height","Rough Height","Nominal Height","Opening Height","Frame Height","Clear Height","Unconnected Height","h","HEIGHT"]

                    w_ft = _get_dim(opening, WIDTH_BIPS, WIDTH_NAMES)
                    h_ft = _get_dim(opening, HEIGHT_BIPS, HEIGHT_NAMES)

                    if w_ft <= 0 or h_ft <= 0: continue
                    thk_ft        = get_param_val(opening, "Thickness")
                    left_edge_ft  = dist_along - (w_ft / 2.0)
                    right_edge_ft = dist_along + (w_ft / 2.0)
                    sill_height_ft = loc_pt.Z - geo['min_z']

                    lvl           = level_name(opening)
                    facing_orient = xyz_str(opening.FacingOrientation) if hasattr(opening, "FacingOrientation") else ""
                    hand_orient   = xyz_str(opening.HandOrientation)   if hasattr(opening, "HandOrientation")   else ""
                    from_room     = get_param_val(opening, "From Room", as_string=True)
                    to_room       = get_param_val(opening, "To Room",   as_string=True)
                    mark          = get_param_val(opening, "Mark",      as_string=True)
                    comments      = get_param_val(opening, "Comments",  as_string=True)
                    area_sf       = w_ft * h_ft

                    csv_writer.writerow([
                        opening_id, category, category, type_name, family_name,
                        combined_id, host_wall_type, lvl, rnum(sill_height_ft),
                        rnum(w_ft), rnum(h_ft), rnum(thk_ft),
                        rnum(dist_along), rnum(left_edge_ft), rnum(right_edge_ft),
                        xyz_str(loc_pt), facing_orient, hand_orient,
                        from_room, to_room, mark, comments, rnum(area_sf)
                    ])
                except Exception as ex:
                    print("  Failed to export opening {}: {}".format(opening.Id.IntegerValue, ex))
                    continue

    print("Openings exported successfully to: {}".format(OPENINGS_PATH))

except IOError as e:
    print("\nERROR: Cannot write to wall_openings.csv")
    raise

# ========== EXPORT WALL MAPPING CSV ==========
print("\nExporting wall mapping...")
try:
    with codecs.open(MAPPING_PATH, mode="w", encoding="utf-8") as f:
        csv_writer = csv.writer(f)
        csv_writer.writerow(["CombinedWallId", "FacadeId", "OriginalWallId"])
        for _gd in _group_data:
            for wall in _gd['walls']:
                csv_writer.writerow([_gd['id'], _gd['label'], wall.Id.IntegerValue])
    print("Wall mapping exported successfully.\n")
except Exception as e:
    print("Error exporting mapping: {}".format(e))

# ========== SUMMARY ==========
print("\n" + "=" * 70)
print("WALL-BY-WALL EXPORT COMPLETE")
print("=" * 70)
print("Walls CSV:    {}".format(WALLS_PATH))
print("Openings CSV: {}".format(OPENINGS_PATH))
print("Mapping CSV:  {}".format(MAPPING_PATH))