# -*- coding: utf-8 -*-
"""
FACADE-BY-FACADE EXPORT
Combines adjacent BASIC WALLS into a single facade sequence.
Openings (doors, windows) remain separate.
"""

def run_export():
from Autodesk.Revit.DB import (
    FilteredElementCollector, Wall, BuiltInParameter, LocationPoint, XYZ,
    Level, BuiltInCategory, ElementId, ElementClassFilter
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

if result == DialogResult.OK:
    OUTPUT_DIR = dialog.SelectedPath
    print("Selected output folder: " + OUTPUT_DIR)
else:
    print("No folder selected.")
    return None

WALLS_FILE    = "walls.csv"
OPENINGS_FILE = "wall_openings.csv"
MAPPING_FILE  = "wall_mapping.csv"
WALLS_PATH    = os.path.join(OUTPUT_DIR, WALLS_FILE)
OPENINGS_PATH = os.path.join(OUTPUT_DIR, OPENINGS_FILE)
MAPPING_PATH  = os.path.join(OUTPUT_DIR, MAPPING_FILE)

if not os.path.isdir(OUTPUT_DIR):
    os.makedirs(OUTPUT_DIR)

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

def get_sequential_wall_geometry(walls):
    """
    Compute the true visual-left → visual-right extent of a combined facade
    from the location curve endpoints of all selected wall segments.

    IMPORTANT — Location Line dependency:
      GetEndPoint() returns the LOCATION CURVE endpoint, which at a corner
      join is trimmed to the intersection of the two walls' reference lines.
      The reference line used depends on the wall's Location Line parameter:

        "Wall Center"     → endpoint is at the ADJACENT wall's center line
                            → inset from the outer corner by half the
                              adjacent wall's thickness
                            → leaves a visible gap at building corners

        "Finish Exterior" → endpoint is at the ADJACENT wall's exterior face
                            → sits at the outer corner of the building
                            → panels start flush with the facade face ✓

      RECOMMENDATION: set facade walls to "Finish Exterior" before exporting
      so that Start(X,Y,Z) captures the true outer face of the building.
    """
    if not walls: return None

    all_pts = []
    for w in walls:
        lc = w.Location.Curve
        all_pts.append(lc.GetEndPoint(0))
        all_pts.append(lc.GetEndPoint(1))

    try:
        normal = walls[0].Orientation
        up     = XYZ(0, 0, 1)
        visual_right_dir = normal.CrossProduct(up).Normalize()
    except Exception:
        visual_right_dir = XYZ(1, 0, 0)

    start_pt = min(all_pts, key=lambda p: p.DotProduct(visual_right_dir))
    end_pt   = max(all_pts, key=lambda p: p.DotProduct(visual_right_dir))

    min_z = min(p.Z for p in all_pts)
    max_z = min_z
    for w in walls:
        try:
            h   = w.get_Parameter(BuiltInParameter.WALL_USER_HEIGHT_PARAM).AsDouble()
            top = w.Location.Curve.GetEndPoint(0).Z + h
            if top > max_z: max_z = top
        except Exception: pass

    total_len = sum(w.Location.Curve.Length for w in walls)
    vec       = (end_pt - start_pt).Normalize()
    true_end  = start_pt + (vec * total_len)

    return {
        'start':     start_pt,
        'end':       true_end,
        'direction': vec,
        'height':    max_z - min_z,
        'min_z':     min_z
    }


# ========== FACADE GROUPING HELPERS ==========

def group_walls_by_facade(walls, dir_tol=0.05, perp_tol=1.0):
    """
    Partition basic walls into collinear facade groups.
    Two walls belong to the same facade if:
      1. Their XY direction vectors are parallel  (|cross| < dir_tol ≈ sin 3°)
      2. A point on one wall is within perp_tol ft of the other wall's line
    Returns a list of lists, each sub-list is one facade group.
    """
    groups   = []
    assigned = set()
    for i, wall in enumerate(walls):
        if i in assigned:
            continue
        try:
            lc = wall.Location.Curve
            p0 = lc.GetEndPoint(0)
            p1 = lc.GetEndPoint(1)
            dx, dy = p1.X - p0.X, p1.Y - p0.Y
            mag = (dx*dx + dy*dy) ** 0.5
            if mag < 0.001:
                groups.append([wall]); assigned.add(i); continue
            n = (dx/mag, dy/mag)
        except Exception:
            groups.append([wall]); assigned.add(i); continue

        group = [wall]
        assigned.add(i)

        for j, other in enumerate(walls):
            if j in assigned:
                continue
            try:
                olc = other.Location.Curve
                op0 = olc.GetEndPoint(0)
                op1 = olc.GetEndPoint(1)
                odx, ody = op1.X - op0.X, op1.Y - op0.Y
                omag = (odx*odx + ody*ody) ** 0.5
                if omag < 0.001:
                    continue
                on = (odx/omag, ody/omag)
                if abs(n[0]*on[1] - n[1]*on[0]) > dir_tol:   # not parallel
                    continue
                vx = op0.X - p0.X
                vy = op0.Y - p0.Y
                if abs(vx*n[1] - vy*n[0]) > perp_tol:        # not collinear
                    continue
                group.append(other)
                assigned.add(j)
            except Exception:
                continue

        groups.append(group)
    return groups


def get_facade_label(wall, used_labels):
    """
    Return a unique compass-direction label for a facade based on wall.Orientation.
    Cardinal examples:  N  S  E  W  NE  NW  SE  SW
    If two facades share a direction, append a counter: N, N2, N3, ...
    """
    import math as _m
    try:
        nx = wall.Orientation.X
        ny = wall.Orientation.Y
        angle  = _m.degrees(_m.atan2(ny, nx)) % 360.0
        names  = ['E','NE','N','NW','W','SW','S','SE']
        base   = names[int((angle + 22.5) / 45) % 8]
    except Exception:
        base = 'F'
    label = base
    n = 2
    while label in used_labels:
        label = '{}{}'.format(base, n)
        n += 1
    used_labels.add(label)
    return label


def find_group_for_opening(opening, group_data, wall_to_group):
    """
    Return the group-dict that an opening (door/window/curtain wall) belongs to.
    Tries host-wall lookup first; falls back to nearest facade by perpendicular distance.
    """
    # 1. Via .Host property (doors/windows)
    try:
        host = opening.Host
        if host:
            g = wall_to_group.get(host.Id.IntegerValue)
            if g is not None: return g
    except Exception: pass
    # 2. Via HOST_ID_PARAM (alternative for some family instances)
    try:
        hip = opening.get_Parameter(BuiltInParameter.HOST_ID_PARAM)
        if hip:
            g = wall_to_group.get(hip.AsElementId().IntegerValue)
            if g is not None: return g
    except Exception: pass
    # 3. Curtain wall — look up its own element id
    try:
        g = wall_to_group.get(opening.Id.IntegerValue)
        if g is not None: return g
    except Exception: pass
    # 4. Geometric fallback: nearest facade by perpendicular distance
    try:
        if isinstance(opening, Wall):
            lc = opening.Location.Curve
            pt = lc.Evaluate(0.5, True)
        else:
            pt = opening.Location.Point
        best = group_data[0]
        best_d = float('inf')
        for gd in group_data:
            geo = gd['geo']
            vx  = pt.X - geo['start'].X
            vy  = pt.Y - geo['start'].Y
            d   = geo['direction']
            perp = abs(vx * d.Y - vy * d.X)
            if perp < best_d:
                best_d = perp
                best   = gd
        return best
    except Exception:
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
        kind_name = str(wall.WallType.Kind)
        if kind_name.lower() == "basic":
            basic_walls.append(wall)
    except:
        basic_walls.append(wall)

print("  {} basic walls across all selected facades".format(len(basic_walls)))

# ========== GROUP WALLS INTO FACADES ==========
facade_groups = group_walls_by_facade(basic_walls)
print("  {} facade group(s) detected".format(len(facade_groups)))

_tol          = 0.01   # ft (~1/8 in)
_used_labels  = set()
_group_data   = []     # one dict per facade
for _grp in facade_groups:
    _geo   = get_sequential_wall_geometry(_grp)
    _cid   = _grp[0].Id.IntegerValue   # fallback
    _found = False
    for _cw in _grp:
        _lc = _cw.Location.Curve
        for _pt in [_lc.GetEndPoint(0), _lc.GetEndPoint(1)]:
            if _pt.DistanceTo(_geo['start']) < _tol:
                _cid = _cw.Id.IntegerValue
                _found = True
                break
        if _found: break
    _label = get_facade_label(_grp[0], _used_labels)
    _group_data.append({'walls': _grp, 'geo': _geo, 'id': _cid, 'label': _label})
    print("  Facade '{}': {} wall(s) | combined_id={}{}".format(
          _label, len(_grp), _cid,
          '' if _found else ' [WARN: no endpoint at geo start]'))

# Build wall-id → group lookup (basic walls only at this stage)
_wall_to_group = {}
for _gd in _group_data:
    for _w in _gd['walls']:
        _wall_to_group[_w.Id.IntegerValue] = _gd

# ========== EXPORT WALLS CSV ==========
print("\nExporting combined basic walls to CSV...")

try:
    with codecs.open(WALLS_PATH, mode="w", encoding="utf-8") as f:
        csv_writer = csv.writer(f)
        csv_writer.writerow([
            "WallId","FacadeId","WallKind","TypeName","FamilyName","Function","IsStructural",
            "Width(ft)","Length(ft)","UnconnectedHeight(ft)","Area(sf)","Volume(cf)",
            "BaseLevel","BaseOffset(ft)","TopConstraint","TopOffset(ft)","LocationLine",
            "CurveType","Start(X,Y,Z)","End(X,Y,Z)","Mid(X,Y,Z)","CurveLength(ft)",
            "ArcRadius(ft)","ArcAngle(rad)","ArcCenter(X,Y,Z)","AxisDir(unit XYZ)","Normal(unit XYZ)",
            "StructFaceOffset(ft)","Layers","WallCount","LevelElevations(in)"
        ])

        if not _group_data:
            print("  No basic walls to export.")
        else:
            levels        = list(FilteredElementCollector(doc).OfClass(Level))
            level_elev_in = sorted([int(round(l.Elevation * 12)) for l in levels])

            for _gd in _group_data:
                geo         = _gd['geo']
                combined_id = _gd['id']
                facade_id   = _gd['label']
                grp_walls   = _gd['walls']
                length_ft   = geo['start'].DistanceTo(geo['end'])
                height_ft   = geo['height']

                first_wall  = grp_walls[0]
                width_ft    = first_wall.Width
                wall_type   = doc.GetElement(first_wall.GetTypeId())
                kind_name   = "Basic (Combined)"
                type_name   = getattr(wall_type, "Name", "") or ""
                family_name = getattr(wall_type, "FamilyName", "") or ""

                function_str = (get_param_val(first_wall, "Function", as_string=True) or
                                get_param_val(first_wall, get_bip("WALL_ATTR_FUNCTION_PARAM"), as_string=True) or "")
                is_struct    = bool(getattr(first_wall, "Structural", False))
                area_sf      = rnum(length_ft * height_ft)
                vol_cf       = rnum(length_ft * height_ft * width_ft)
                base_lvl     = level_name(first_wall)
                base_off     = (get_param_val(first_wall, get_bip("WALL_BASE_OFFSET")) or
                                get_param_val(first_wall, "Base Offset") or "")
                top_con      = (get_param_val(first_wall, "Top Constraint", as_string=True) or
                                get_param_val(first_wall, get_bip("WALL_HEIGHT_TYPE"), as_string=True) or "")
                top_off      = (get_param_val(first_wall, get_bip("WALL_TOP_OFFSET")) or
                                get_param_val(first_wall, "Top Offset") or "")
                loc_line     = (get_param_val(first_wall, get_bip("WALL_KEY_REF_PARAM"), as_string=True) or
                                get_param_val(first_wall, "Location Line", as_string=True) or "")

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
                try:
                    wall_normal_xyz = first_wall.Orientation
                except Exception: pass

                struct_face_offset_ft = 0.0
                try:
                    _cs = wall_type.GetCompoundStructure()
                    if _cs:
                        _ly = list(_cs.GetLayers())
                        _ci = _cs.FirstCoreLayerIndex
                        _ext_nc = sum(_ly[i].Width for i in range(_ci))
                        _ll = str(loc_line).lower()
                        if ("finish" in _ll or "face" in _ll) and "exterior" in _ll and "core" not in _ll:
                            _loc_to_ext = 0.0
                        elif "core" in _ll and "exterior" in _ll:
                            _loc_to_ext = _ext_nc
                        elif "core" in _ll and ("center" in _ll or "centre" in _ll):
                            _cw2 = sum(_ly[i].Width for i in range(_ci, _cs.LastCoreLayerIndex + 1))
                            _loc_to_ext = _ext_nc + _cw2 / 2.0
                        elif "core" in _ll and "interior" in _ll:
                            _cw2 = sum(_ly[i].Width for i in range(_ci, _cs.LastCoreLayerIndex + 1))
                            _loc_to_ext = _ext_nc + _cw2
                        elif "interior" in _ll and ("finish" in _ll or "face" in _ll):
                            _loc_to_ext = width_ft
                        else:
                            _loc_to_ext = width_ft / 2.0
                        struct_face_offset_ft = round(_loc_to_ext - _ext_nc, 6)
                        print("    '{}' StructFaceOffset: {:.3f} in | ExtNonCore: {:.3f} in".format(
                              facade_id, struct_face_offset_ft * 12, _ext_nc * 12))
                except Exception as _sf_ex:
                    print("  [WARN] '{}' Could not compute StructFaceOffset: {}".format(facade_id, _sf_ex))

                mid_pt = geo['start'] + (geo['direction'] * (length_ft / 2.0))

                csv_writer.writerow([
                    combined_id, facade_id, kind_name, type_name, family_name, function_str, is_struct,
                    rnum(width_ft), rnum(length_ft), rnum(height_ft), area_sf, vol_cf,
                    base_lvl, rnum(base_off), top_con, rnum(top_off), loc_line,
                    "Line (Combined)", xyz_str(geo['start']), xyz_str(geo['end']), xyz_str(mid_pt), rnum(length_ft),
                    "", "", "", "", xyz_str(wall_normal_xyz) if wall_normal_xyz else "", rnum(struct_face_offset_ft),
                    layers_str, str(len(grp_walls)), json.dumps(level_elev_in)
                ])

                print("  Facade '{}': {} walls | {:.2f} ft × {:.2f} ft | id={}".format(
                      facade_id, len(grp_walls), rnum(length_ft, 2), rnum(height_ft, 2), combined_id))
                _loc_lower = str(loc_line).lower()
                if not ("finish exterior" in _loc_lower or
                        ("finish" in _loc_lower and "exterior" in _loc_lower)):
                    print("  [WARN] '{}' Location Line is '{}', not 'Finish Exterior'. "
                          "Corner endpoints may be inset.".format(facade_id, loc_line))

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

_seen_cw_ids = set()
curtain_walls_hosted = []

# Scenario A: directly selected curtain walls
for _sw in selected_walls:
    try:
        if str(_sw.WallType.Kind).lower() == "curtain":
            curtain_walls_hosted.append(_sw)
            _seen_cw_ids.add(_sw.Id.IntegerValue)
    except Exception: pass

# Scenario B: curtain walls embedded inside a selected basic wall
_wall_class_filter = ElementClassFilter(Wall)
for _bwall in basic_walls:
    try:
        for _dep_id in _bwall.GetDependentElements(_wall_class_filter):
            if _dep_id.IntegerValue in _seen_cw_ids: continue
            _dep = doc.GetElement(_dep_id)
            if isinstance(_dep, Wall):
                try:
                    if str(_dep.WallType.Kind).lower() == "curtain":
                        curtain_walls_hosted.append(_dep)
                        _seen_cw_ids.add(_dep_id.IntegerValue)
                except Exception: pass
    except Exception: pass

# Register Scenario-A curtain walls in _wall_to_group (by geometric proximity)
for _cw in curtain_walls_hosted:
    if _cw.Id.IntegerValue not in _wall_to_group:
        _gd_match = find_group_for_opening(_cw, _group_data, _wall_to_group)
        _wall_to_group[_cw.Id.IntegerValue] = _gd_match

all_openings_list = list(doors) + list(windows) + curtain_walls_hosted
print("  Doors: {} | Windows: {} | Curtain/Storefronts: {}".format(
    len(doors), len(windows), len(curtain_walls_hosted)))
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
                    # Route opening to its facade
                    _gd          = find_group_for_opening(opening, _group_data, _wall_to_group)
                    geo          = _gd['geo']
                    combined_id  = _gd['id']
                    _cwall_elem  = doc.GetElement(ElementId(combined_id))
                    host_wall_type = (getattr(doc.GetElement(_cwall_elem.GetTypeId()), "Name", "")
                                      if _cwall_elem else "")

                    # ---- Curtain Wall / Storefront ----------------------------------------
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
                        _h_ft = 0.0
                        for _hbip in [BuiltInParameter.WALL_USER_HEIGHT_PARAM,
                                      BuiltInParameter.WALL_ATTR_HEIGHT_PARAM]:
                            try:
                                _hp = opening.get_Parameter(_hbip)
                                if _hp:
                                    _hv = _hp.AsDouble()
                                    if _hv and _hv > 0:
                                        _h_ft = _hv
                                        break
                            except Exception: pass
                        if _w_ft <= 0 or _h_ft <= 0:
                            print("  [WARN] Curtain wall {} ({}): W={} H={} -- skipping.".format(
                                  opening_id, _cw_type_name, rnum(_w_ft), rnum(_h_ft)))
                            continue
                        _base_z = min(_p0.Z, _p1.Z)
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
                    # ---- End Curtain Wall -------------------------------------------------

                    category          = opening.Category.Name if opening.Category else ""
                    opening_type_elem = doc.GetElement(opening.GetTypeId())
                    type_name         = getattr(opening_type_elem, "Name", "") or ""
                    family_name       = (getattr(opening_type_elem, "FamilyName", "")
                                         if hasattr(opening_type_elem, "FamilyName") else "")
                    loc_pt = opening.Location.Point if hasattr(opening.Location, "Point") else XYZ(0,0,0)
                    dist_along = (loc_pt - geo['start']).DotProduct(geo['direction'])

                    def _get_dim(elem, bips, names):
                        sources = [elem]
                        try: sources.append(doc.GetElement(elem.GetTypeId()))
                        except Exception: pass
                        for src in sources:
                            if src is None: continue
                            for bip in bips:
                                try:
                                    p = src.get_Parameter(bip)
                                    if p:
                                        v = p.AsDouble()
                                        if v and v > 0: return v
                                except Exception: pass
                            for nm in names:
                                try:
                                    p = src.LookupParameter(nm)
                                    if p:
                                        v = p.AsDouble()
                                        if v and v > 0: return v
                                except Exception: pass
                        return 0.0

                    WIDTH_BIPS  = [BuiltInParameter.DOOR_WIDTH,  BuiltInParameter.WINDOW_WIDTH,
                                   BuiltInParameter.GENERIC_WIDTH, BuiltInParameter.FAMILY_WIDTH_PARAM]
                    HEIGHT_BIPS = [BuiltInParameter.DOOR_HEIGHT, BuiltInParameter.WINDOW_HEIGHT,
                                   BuiltInParameter.GENERIC_HEIGHT, BuiltInParameter.FAMILY_HEIGHT_PARAM]
                    WIDTH_NAMES  = ["Width","Rough Width","Nominal Width","Opening Width",
                                    "Frame Width","Clear Width","w","WIDTH"]
                    HEIGHT_NAMES = ["Height","Rough Height","Nominal Height","Opening Height",
                                    "Frame Height","Clear Height","Unconnected Height","h","HEIGHT"]

                    w_ft = _get_dim(opening, WIDTH_BIPS, WIDTH_NAMES)
                    h_ft = _get_dim(opening, HEIGHT_BIPS, HEIGHT_NAMES)

                    if w_ft <= 0 or h_ft <= 0:
                        print("  [WARN] Opening {} ({}): could not read W={} H={} -- "
                              "check family parameter names.".format(
                              opening.Id.IntegerValue,
                              getattr(doc.GetElement(opening.GetTypeId()), "Name", "?"),
                              w_ft, h_ft))
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
print("FACADE EXPORT COMPLETE")
print("=" * 70)
print("Walls CSV:    {}".format(WALLS_PATH))
print("Openings CSV: {}".format(OPENINGS_PATH))
print("Mapping CSV:  {}".format(MAPPING_PATH))

return OUTPUT_DIR