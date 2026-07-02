# -*- coding: utf-8 -*-
"""
WALL-BY-WALL EXPORT
Exports Basic Walls and unpacked Stacked Walls individually.
Openings (doors, windows, storefronts) are mapped to their specific host wall.
"""
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
    Accurately extracts Z-elevation using BoundingBox for Stacked Wall members.
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

    # Calculate absolute X/Y span
    all_pts = [p0, p1]
    start_pt = min(all_pts, key=lambda p: p.DotProduct(visual_right_dir))
    end_pt   = max(all_pts, key=lambda p: p.DotProduct(visual_right_dir))

    vec = (end_pt - start_pt).Normalize()

    # --- FIX: True Absolute Z-Elevation for Stacked Wall Members ---
    # Do not trust Location.Curve Z for sub-walls. Pull the true physical BoundingBox.
    try:
        bb = wall.get_BoundingBox(None)
        if bb:
            min_z = bb.Min.Z
            max_z = bb.Max.Z
        else:
            # Fallback if BB fails
            min_z = min(p.Z for p in all_pts)
            h = wall.get_Parameter(BuiltInParameter.WALL_USER_HEIGHT_PARAM).AsDouble()
            max_z = min_z + (h if h else 10.0)
    except Exception:
        min_z = min(p.Z for p in all_pts)
        max_z = min_z

    # Re-map the start and end points to the true absolute Z plane
    start_pt = XYZ(start_pt.X, start_pt.Y, min_z)
    end_pt   = XYZ(end_pt.X, end_pt.Y, min_z)

    return {
        'start':     start_pt,
        'end':       end_pt,
        'direction': vec,
        'height':    max_z - min_z,
        'min_z':     min_z,
        'length':    lc.Length
    }

def _normalize_xy(v):
    mag = (v.X * v.X + v.Y * v.Y) ** 0.5
    if mag < 1e-9:
        return None
    return XYZ(v.X / mag, v.Y / mag, 0.0)

def find_group_for_opening(opening, group_data, wall_to_group):
    """
    Return the group-dict that an opening (door/window/curtain wall) belongs to.
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
                def _corner_ext(endpoint, wall_dir, excl_id):
                    _tol = 0.15
                    _ext = 0.0
                    for _aw in _all_doc_walls:
                        if _aw.Id.IntegerValue == excl_id: continue
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

                _s_ext = _corner_ext(geo['start'], geo['direction'], combined_id)
                _e_ext = _corner_ext(geo['end'],   geo['direction'], combined_id)

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
                        # Map this curtain wall directly to its parent basic wall ID
                        _wall_to_group[_dep_id.IntegerValue] = _wall_to_group[_bwall.Id.IntegerValue]
                except Exception: pass
    except Exception: pass

# Final fallback for unbound curtain walls
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
                    _gd          = find_group_for_opening(opening, _group_data, _wall_to_group)
                    geo          = _gd['geo']
                    combined_id  = _gd['id']
                    _cwall_elem  = doc.GetElement(ElementId(combined_id))
                    host_wall_type = (getattr(doc.GetElement(_cwall_elem.GetTypeId()), "Name", "") if _cwall_elem else "")

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
                        _h_ft = 0.0
                        for _hbip in [BuiltInParameter.WALL_USER_HEIGHT_PARAM, BuiltInParameter.WALL_ATTR_HEIGHT_PARAM]:
                            try:
                                _hp = opening.get_Parameter(_hbip)
                                if _hp:
                                    _hv = _hp.AsDouble()
                                    if _hv and _hv > 0:
                                        _h_ft = _hv
                                        break
                            except: pass
                        if _w_ft <= 0 or _h_ft <= 0: continue
                        
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

                    # ---- Standard Doors / Windows ----
                    category          = opening.Category.Name if opening.Category else ""
                    opening_type_elem = doc.GetElement(opening.GetTypeId())
                    type_name         = getattr(opening_type_elem, "Name", "") or ""
                    family_name       = (getattr(opening_type_elem, "FamilyName", "") if hasattr(opening_type_elem, "FamilyName") else "")
                    loc_pt = opening.Location.Point if hasattr(opening.Location, "Point") else XYZ(0,0,0)
                    
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