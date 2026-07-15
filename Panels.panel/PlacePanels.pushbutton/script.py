# coding: ascii
"""Place panels on walls from optimized CSV output.

Placing on a front facade? Select "Force End (Right)" for X Ref and "Force 0" for Rotation.

Placing on a right facade? Select "Force End (Right)" for X Ref and "Force 90" for Rotation.

Placing on a back facade? Select "Force End (Right)" for X Ref and "Force 180" for Rotation.

Placing on a left facade? Select "Force End (Right)" for X Ref and "Force -90" for Rotation.

NOTE: If you choose "Use CSV Rotation" in the Set Rotation dialog, panels are
now auto-rotated to face the wall's true exterior normal (via
_face_panel_outward), so the manual Force options above shouldn't be
necessary anymore. They're kept available as a manual fallback/override in
case a wall's geometry or family doesn't support FacingOrientation.

"""

from Autodesk.Revit.DB import (
    FilteredElementCollector, Wall, Transaction, XYZ, Line,
    FamilySymbol, BuiltInCategory, BuiltInParameter, Transform, ElementId,
    DirectShape, ElementTransformUtils, FamilyPlacementType,
    HostObjectUtils, ShellLayerType, PlanarFace,
    Color, OverrideGraphicSettings, FillPatternElement # <-- Added for Visualization
)

try:
    from Autodesk.Revit.DB.Structure import StructuralType
except:
    from Autodesk.Revit.DB import Structure
    StructuralType = Structure.StructuralType

from pyrevit import revit
import csv
import os
import json
import math

import clr
clr.AddReference('System.Windows.Forms')
from System.Windows.Forms import FolderBrowserDialog, DialogResult

doc = revit.doc

# ========== SETTINGS ==========
DEFAULT_INPUT_DIR = None
PANELS_FILE = "optimized_panel_placement.csv"
USE_FOLDER_PICKER = True
PANEL_FAMILY_NAME = None

SHOW_CUTOUTS = False
ALLOW_TYPE_PARAM_CHANGE = True

# Diagnostic parameter dump guard.
_PARAM_DUMP_DONE = [False]

# After size/rotation, verify the placed family bounding box against the CSV
# left/right panel span and nudge it if the family origin/reference planes are
# not exactly centered on visible geometry.
ENABLE_BBOX_SPAN_ALIGNMENT = True
BBOX_ALIGN_TOLERANCE_IN = 0.001

# --- VOID CONTROL ---
ENABLE_VOID_CONTROL = True

# --- DEPTH SETTINGS ---
PANEL_THICKNESS_IN = 6.625  # 6" stud + 5/8" sheathing
FAMILY_ORIGIN_LOCATION = "Back"  # Matches the red dot on your interior stud face
MANUAL_DEPTH_OFFSET_IN = 0.0

# --- COORDINATE SETTINGS ---
PANEL_COORD_DEFAULT_REF = "start"
USE_CSV_ROTATION = True

# Runtime overrides
X_REF_OVERRIDE = None
ROTATION_OVERRIDE_DEG = None

# -----------------------------------------------------------------------
# PANEL SIZE
# -----------------------------------------------------------------------
WIDTH_PARAM_CANDIDATES  = ["Overall Width (default)", "Overall Width"]
HEIGHT_PARAM_CANDIDATES = ["Overall Height (default)", "Overall Height"]

# -----------------------------------------------------------------------
# VOID PARAMETERS  (canonical names; fallbacks handled in _set_first_found)
# -----------------------------------------------------------------------
V1_UNIT_WIDTH   = "UNIT 1 WIDTH"
V1_UNIT_HEIGHT  = "UNIT 1 HEIGHT"
V1_JAMB_CLR     = "VOID 1 JAMB CLR"
V1_HEAD_CLR     = "VOID 1 HEAD CLR"
V1_SILL_CLR     = "VOID 1 SILL CLR"
V1_X_OFFSET_CANDIDATES = ["Void 1 X Offset Left", "Void 1 X Offset", "V1_X_OFFSET"]
V1_Y_OFFSET     = "Void 1 Y Offset"
V1_VISIBLE      = "VOID 1"

V2_UNIT_WIDTH   = "UNIT 2 WIDTH"
V2_UNIT_HEIGHT  = "UNIT 2 HEIGHT"
V2_JAMB_CLR     = "VOID 2 JAMB CLR"
V2_HEAD_CLR     = "VOID 2 HEAD CLR"
V2_SILL_CLR     = "VOID 2 SILL CLR"
V2_X_OFFSET_CANDIDATES = ["Void 2 X Offset Left", "Void 2 X Offset", "V2_X_OFFSET"]
V2_Y_OFFSET     = "Void 2 Y Offset"
V2_VISIBLE      = "VOID 2"

# -----------------------------------------------------------------------
# CLEARANCE DEFAULTS BY OPENING TYPE (inches)
# -----------------------------------------------------------------------
CLEARANCE_BY_TYPE = {
    "door":               {"jamb": 0.0, "head": 0.0, "sill": 0.0},
    "storefront/curtain": {"jamb": 0.0, "head": 0.0, "sill": 0.0},
    "window":             {"jamb": 0.0, "head": 0.0, "sill": 0.0},
    "default":            {"jamb": 0.0, "head": 0.0, "sill": 0.0},
}

MIN_VOID_DIMENSION_IN = 0.5

USE_WALL_ENDCAP_EXTENSION = False
PANEL_SIDE_SIGN = 1


# ========== UTILITIES ==========

def _pick_input_folder(default_dir=None):
    try:
        fbd = FolderBrowserDialog()
        fbd.Description = "Select the folder containing '{0}'".format(PANELS_FILE)
        if default_dir and os.path.isdir(default_dir):
            fbd.SelectedPath = default_dir
        result = fbd.ShowDialog()
        if result == DialogResult.OK and fbd.SelectedPath:
            return str(fbd.SelectedPath)
    except:
        pass
    return None

def norm_id(val):
    try: return str(int(float(val)))
    except: return str(val).strip()

def get_wall_by_id(wall_id):
    try:
        # 1. Clean the string to a pure Python integer
        elem_id = int(float(wall_id))
        
        # 2. Explicitly load the .NET System library to handle 64-bit IDs
        import clr
        clr.AddReference("System")
        import System
        
        e_id = None
        try:
            # Revit 2024+ (Strict Int64)
            e_id = ElementId(System.Int64(elem_id))
        except Exception as e_64:
            try:
                # Fallback for Revit 2023 and older
                e_id = ElementId(elem_id)
            except Exception as e_32:
                print("  [DEBUG] ElementId cast failed. 64-bit error: {} | 32-bit error: {}".format(e_64, e_32))
                return None
                
        # 3. Retrieve and verify the wall geometry
        if e_id:
            element = doc.GetElement(e_id)
            if element:
                if isinstance(element, Wall):
                    return element
                else:
                    print("  [DEBUG] Element {} found, but it is a {} (not a Basic Wall).".format(elem_id, type(element)))
            else:
                print("  [DEBUG] doc.GetElement returned None for ID {}. Is the wall deleted or in a linked model?".format(elem_id))
                
    except Exception as main_err:
        print("  [DEBUG] Total crash in get_wall_by_id for ID {}: {}".format(wall_id, main_err))
        
    return None

def _feet(val_inch):
    return float(val_inch) / 12.0

def _safe_float(val, default=0.0):
    try: return float(val)
    except: return default

def _try_float_strict(val):
    """Return (float_value, True) if val is a non-empty numeric string, else (None, False)."""
    if val is None: return None, False
    s = str(val).strip()
    if s == "": return None, False
    try: return float(s), True
    except: return None, False

# --- NEW VISUALIZATION HELPER ---
def get_color_from_string(text):
    """Generates a consistent RGB Color based on a string (like 'T01')."""
    # Simple hash to generate consistent numbers from text
    hash_val = hash(text)
    
    # Extract R, G, B values (keep them bright/pastel by avoiding low numbers)
    r = (hash_val & 0xFF0000) >> 16
    g = (hash_val & 0x00FF00) >> 8
    b = hash_val & 0x0000FF
    
    # Ensure colors aren't too dark
    r = max(100, r)
    g = max(100, g)
    b = max(100, b)
    
    return Color(r, g, b)


# ========== GEOMETRY CORE ==========

def get_wall_base_elevation(wall):
    """
    Returns the wall base Z in Revit internal feet.
    This is the Level elevation + base offset.
    Used only for diagnostics/logging now — NOT as the panel Z base.
    """
    base_z = 0.0
    try:
        lvl_id = wall.get_Parameter(BuiltInParameter.WALL_BASE_CONSTRAINT).AsElementId()
        if lvl_id and lvl_id.IntegerValue > 0:
            level = doc.GetElement(lvl_id)
            if level:
                base_z = level.Elevation
    except:
        pass
    try:
        base_offset_param = wall.get_Parameter(BuiltInParameter.WALL_BASE_OFFSET)
        if base_offset_param:
            base_z += base_offset_param.AsDouble()
    except:
        pass
    return base_z

def compute_panel_base_point(wall, panel, extra_z_offset_in=0.0):
    # Retrieve explicitly defined coordinates from CSV
    x_in = _safe_float(panel.get("x_in", 0.0))
    y_in = _safe_float(panel.get("y_in", 0.0))
    width_in = _safe_float(panel.get("width_in", 0.0))
    x_along = _feet(x_in) + _feet(width_in / 2.0)
    
    ox = _safe_float(panel.get("wall_origin_x"))
    oy = _safe_float(panel.get("wall_origin_y"))
    oz = _safe_float(panel.get("wall_origin_z"))
    dx = _safe_float(panel.get("wall_dir_x"))
    dy = _safe_float(panel.get("wall_dir_y"))
    dz = _safe_float(panel.get("wall_dir_z"))
    nx = _safe_float(panel.get("wall_normal_x"))
    ny = _safe_float(panel.get("wall_normal_y"))
    
    csv_origin = XYZ(ox, oy, oz)
    csv_dir = XYZ(dx, dy, dz).Normalize() if XYZ(dx,dy,dz).GetLength() > 1e-9 else XYZ(1,0,0)
    wall_normal = XYZ(nx, ny, 0.0).Normalize() if XYZ(nx,ny,0).GetLength() > 1e-9 else XYZ(0,1,0)
    
    pt_xy = csv_origin + (csv_dir * x_along)
    
    # Simple Z calculation
    base_z = oz + _feet(y_in) + _feet(extra_z_offset_in)
    base_point_loc = XYZ(pt_xy.X, pt_xy.Y, base_z)
    
    # Rely purely on MANUAL_DEPTH_OFFSET_IN (default 0.0)
    calculated_offset = _feet(MANUAL_DEPTH_OFFSET_IN)
    
    if FAMILY_ORIGIN_LOCATION == "Front":
        calculated_offset += _feet(PANEL_THICKNESS_IN) / 2.0
    elif FAMILY_ORIGIN_LOCATION == "Back":
        calculated_offset -= _feet(PANEL_THICKNESS_IN) / 2.0
        
    return base_point_loc + (wall_normal * calculated_offset), csv_dir, wall_normal

def _dump_param_names(inst):
    if "_PARAM_DUMP_DONE" not in globals():
        globals()["_PARAM_DUMP_DONE"] = [False]
    if _PARAM_DUMP_DONE[0]:
        return
    _PARAM_DUMP_DONE[0] = True
    print("  [DIAG] ---- INSTANCE PARAMETERS ----")
    for p in inst.Parameters:
        try: print("  [DIAG]   inst | '{0}'".format(p.Definition.Name))
        except: pass
    print("  [DIAG] ---- TYPE PARAMETERS ----")
    try:
        sym = inst.Symbol
        for p in sym.Parameters:
            try: print("  [DIAG]   type | '{0}'".format(p.Definition.Name))
            except: pass
    except: pass
    print("  [DIAG] ---- END PARAM DUMP ----")

def _resolve_param(inst, param_name):
    variants = [param_name]
    if param_name.endswith(" (default)"):
        variants.append(param_name[:-len(" (default)")])
    else:
        variants.append(param_name + " (default)")
    for name in variants:
        p = inst.LookupParameter(name)
        if p is not None:
            return p
        try:
            p = inst.Symbol.LookupParameter(name)
            if p is not None:
                return p
        except:
            pass
    return None

def _set_param(inst, param_name, value, label=""):
    try:
        p = _resolve_param(inst, param_name)
        if p is None:
            print("    [VOID] NOT FOUND: '{0}'".format(param_name))
            return False
        if p.IsReadOnly:
            print("    [VOID] READ-ONLY (formula): '{0}'".format(param_name))
            return False
        p.Set(value)
        if isinstance(value, float):
            print("    [VOID] SET {0} = {1:.3f} in".format(label or param_name, value * 12.0))
        elif value == 1:
            print("    [VOID] SET {0} = Yes".format(label or param_name))
        elif value == 0:
            print("    [VOID] SET {0} = No".format(label or param_name))
        else:
            print("    [VOID] SET {0} = {1}".format(label or param_name, value))
        return True
    except Exception as e:
        print("    [VOID] Error setting '{0}': {1}".format(param_name, e))
        return False

def _set_first_found(inst, candidates, value, label=""):
    """Try each name in candidates; set the first writable one found."""
    for name in candidates:
        p = _resolve_param(inst, name)
        if p is not None and not p.IsReadOnly:
            try:
                p.Set(value)
                disp = label or name
                if isinstance(value, float):
                    print("    [VOID] SET {0} = {1:.3f} in".format(disp, value * 12.0))
                else:
                    print("    [VOID] SET {0} = {1}".format(disp, value))
                return True
            except Exception as e:
                print("    [VOID] Error setting '{0}': {1}".format(name, e))
    tried = ", ".join("'{0}'".format(n) for n in candidates)
    print("    [VOID] NOT FOUND or READ-ONLY — tried: {0}".format(tried))
    return False

def _get_clearances(cutout):
    opening_type = str(cutout.get("type", "")).strip().lower()
    type_clr = CLEARANCE_BY_TYPE.get(opening_type, CLEARANCE_BY_TYPE.get("default", {}))
    jamb = _safe_float(cutout.get("jamb_clr_in", type_clr.get("jamb", 0.0)))
    head = _safe_float(cutout.get("head_clr_in", type_clr.get("head", 0.0)))
    sill = _safe_float(cutout.get("sill_clr_in", type_clr.get("sill", 0.0)))
    return jamb, head, sill

def set_void_parameters_for_cutouts(inst, panel_data):
    if not ENABLE_VOID_CONTROL:
        return

    _dump_param_names(inst)

    cutouts    = panel_data.get("cutouts", [])
    num        = len(cutouts)
    min_ft     = _feet(MIN_VOID_DIMENSION_IN)
    panel_w_in = _safe_float(panel_data.get("width_in",  120))
    panel_w_ft = _feet(panel_w_in)
    panel_h_ft = _feet(_safe_float(panel_data.get("height_in", 120)))

    print("  [VOID] Panel '{0}': {1} cutout(s)".format(
        panel_data.get("panel_name", "?"), num))

    # ── VOID 1 ────────────────────────────────────────────────────────────
    if num >= 1:
        c = cutouts[0]
        unit_w_in   = max(_safe_float(c.get("raw_width_in",  c.get("width_in",  0))), MIN_VOID_DIMENSION_IN)
        unit_h_in   = max(_safe_float(c.get("raw_height_in", c.get("height_in", 0))), MIN_VOID_DIMENSION_IN)


        raw_x_in    = _safe_float(c.get("raw_x_in", c.get("x_in", 0)))
        # RESTORE MIRROR MATH: The panel is rotated 180 to face exterior, so its internal axes are flipped.
        x_offset_in = max(0.0, panel_w_in - unit_w_in - raw_x_in)
        

        
        y_offset_in = _safe_float(c.get("raw_y_in", c.get("y_in", 0)))
        jamb_clr_in, head_clr_in, sill_clr_in = _get_clearances(c)

        print("  [VOID1] ON | {0:.2f}x{1:.2f}in @ (Mirrored X={2:.2f},{3:.2f})in | clr J={4} H={5} S={6}".format(
            unit_w_in, unit_h_in, x_offset_in, y_offset_in, jamb_clr_in, head_clr_in, sill_clr_in))

        _set_param(inst, V1_VISIBLE,     1,                   "VOID 1")
        _set_param(inst, V1_UNIT_WIDTH,  _feet(unit_w_in),    "UNIT 1 WIDTH")
        _set_param(inst, V1_UNIT_HEIGHT, _feet(unit_h_in),    "UNIT 1 HEIGHT")
        _set_param(inst, V1_JAMB_CLR,    _feet(jamb_clr_in),  "VOID 1 JAMB CLR")
        _set_param(inst, V1_HEAD_CLR,    _feet(head_clr_in),  "VOID 1 HEAD CLR")
        _set_param(inst, V1_SILL_CLR,    _feet(sill_clr_in),  "VOID 1 SILL CLR")
        _set_first_found(inst, V1_X_OFFSET_CANDIDATES, _feet(x_offset_in), "Void 1 X Offset Left")
        _set_param(inst, V1_Y_OFFSET,    _feet(y_offset_in),  "Void 1 Y Offset")

    else:
        print("  [VOID1] OFF | clearing void offsets to zero")
        _set_param(inst, V1_VISIBLE,     0,     "VOID 1")
        _set_param(inst, V1_UNIT_WIDTH,  min_ft, "UNIT 1 WIDTH")
        _set_param(inst, V1_UNIT_HEIGHT, min_ft, "UNIT 1 HEIGHT")
        _set_param(inst, V1_JAMB_CLR,    0.0,   "VOID 1 JAMB CLR")
        _set_param(inst, V1_HEAD_CLR,    0.0,   "VOID 1 HEAD CLR")
        _set_param(inst, V1_SILL_CLR,    0.0,   "VOID 1 SILL CLR")
        _set_first_found(inst, V1_X_OFFSET_CANDIDATES, 0.0, "Void 1 X Offset Left")
        _set_param(inst, V1_Y_OFFSET,    0.0,   "Void 1 Y Offset")

    # ── VOID 2 ────────────────────────────────────────────────────────────
    if num >= 2:
        c2 = cutouts[1]
        unit_w2_in   = max(_safe_float(c2.get("raw_width_in",  c2.get("width_in",  0))), MIN_VOID_DIMENSION_IN)
        unit_h2_in   = max(_safe_float(c2.get("raw_height_in", c2.get("height_in", 0))), MIN_VOID_DIMENSION_IN)

        raw_x2_in    = _safe_float(c2.get("raw_x_in", c2.get("x_in", 0)))
        # RESTORE MIRROR MATH
        x2_offset_in = max(0.0, panel_w_in - unit_w2_in - raw_x2_in)

        
        
        y2_offset_in = _safe_float(c2.get("raw_y_in", c2.get("y_in", 0)))
        jamb2_clr_in, head2_clr_in, sill2_clr_in = _get_clearances(c2)

        print("  [VOID2] ON | {0:.2f}x{1:.2f}in @ (Mirrored X={2:.2f},{3:.2f})in | clr J={4} H={5} S={6}".format(
            unit_w2_in, unit_h2_in, x2_offset_in, y2_offset_in, jamb2_clr_in, head2_clr_in, sill2_clr_in))

        _set_param(inst, V2_VISIBLE,     1,                    "VOID 2")
        _set_param(inst, V2_UNIT_WIDTH,  _feet(unit_w2_in),    "UNIT 2 WIDTH")
        _set_param(inst, V2_UNIT_HEIGHT, _feet(unit_h2_in),    "UNIT 2 HEIGHT")
        _set_param(inst, V2_JAMB_CLR,    _feet(jamb2_clr_in),  "VOID 2 JAMB CLR")
        _set_param(inst, V2_HEAD_CLR,    _feet(head2_clr_in),  "VOID 2 HEAD CLR")
        _set_param(inst, V2_SILL_CLR,    _feet(sill2_clr_in),  "VOID 2 SILL CLR")
        _set_first_found(inst, V2_X_OFFSET_CANDIDATES, _feet(x2_offset_in), "Void 2 X Offset Left")
        _set_param(inst, V2_Y_OFFSET,    _feet(y2_offset_in),  "Void 2 Y Offset")

    else:
        print("  [VOID2] OFF | clearing void offsets to zero")
        _set_param(inst, V2_VISIBLE,     0,     "VOID 2")
        _set_param(inst, V2_UNIT_WIDTH,  min_ft, "UNIT 2 WIDTH")
        _set_param(inst, V2_UNIT_HEIGHT, min_ft, "UNIT 2 HEIGHT")
        _set_param(inst, V2_JAMB_CLR,    0.0,   "VOID 2 JAMB CLR")
        _set_param(inst, V2_HEAD_CLR,    0.0,   "VOID 2 HEAD CLR")
        _set_param(inst, V2_SILL_CLR,    0.0,   "VOID 2 SILL CLR")
        _set_first_found(inst, V2_X_OFFSET_CANDIDATES, 0.0, "Void 2 X Offset Left")
        _set_param(inst, V2_Y_OFFSET,    0.0,   "Void 2 Y Offset")

    if num > 2:
        print("  [VOID] WARNING: {0} cutouts but family supports only 2. "
              "Cutouts beyond index 1 ignored.".format(num))

              
# ========== PLACEMENT ==========

def _bbox_projection_span(bb, direction):
    """Return min/max projection of an element bounding box onto direction."""
    try:
        pts = [
            XYZ(bb.Min.X, bb.Min.Y, bb.Min.Z), XYZ(bb.Min.X, bb.Min.Y, bb.Max.Z),
            XYZ(bb.Min.X, bb.Max.Y, bb.Min.Z), XYZ(bb.Min.X, bb.Max.Y, bb.Max.Z),
            XYZ(bb.Max.X, bb.Min.Y, bb.Min.Z), XYZ(bb.Max.X, bb.Min.Y, bb.Max.Z),
            XYZ(bb.Max.X, bb.Max.Y, bb.Min.Z), XYZ(bb.Max.X, bb.Max.Y, bb.Max.Z),
        ]
        vals = [p.DotProduct(direction) for p in pts]
        return min(vals), max(vals)
    except:
        return None, None

def _get_csv_origin_and_dir(panel, fallback_wall=None):
    """Return the same wall origin/dir basis used by compute_panel_base_point."""
    ox, ox_ok = _try_float_strict(panel.get("wall_origin_x"))
    oy, oy_ok = _try_float_strict(panel.get("wall_origin_y"))
    oz, oz_ok = _try_float_strict(panel.get("wall_origin_z"))
    dx, dx_ok = _try_float_strict(panel.get("wall_dir_x"))
    dy, dy_ok = _try_float_strict(panel.get("wall_dir_y"))
    dz, dz_ok = _try_float_strict(panel.get("wall_dir_z"))
    if all([ox_ok, oy_ok, oz_ok, dx_ok, dy_ok, dz_ok]):
        mag = math.sqrt(dx*dx + dy*dy + dz*dz)
        if mag > 1e-9:
            return XYZ(ox, oy, oz), XYZ(dx/mag, dy/mag, dz/mag)
    return None, None

def _panel_solid_span_along_dir(inst, direction):
    """Project the panel's solid geometry onto `direction`.
    Returns (min_proj, max_proj) in feet. Uses visible solids only. Geometry is post-placement+rotation already."""
    from Autodesk.Revit.DB import Options, Solid, GeometryInstance, ViewDetailLevel
    opt = Options()
    opt.IncludeNonVisibleObjects = False
    opt.DetailLevel = ViewDetailLevel.Fine
    try:
        geo = inst.get_Geometry(opt)
    except:
        return None, None
    if geo is None:
        return None, None

    solids = []
    def _walk(gset):
        for g in gset:
            try:
                if isinstance(g, Solid):
                    if g.Volume > 1e-6:
                        solids.append(g)
                elif isinstance(g, GeometryInstance):
                    _walk(g.GetInstanceGeometry())
            except:
                pass
    _walk(geo)
    if not solids:
        return None, None

    # FIX: Do not isolate the single largest body. A storefront cutout can split the
    # panel into two disjoint legs. We must measure the span of ALL solid parts together.
    vals = []
    for s in solids:
        for edge in s.Edges:
            try:
                c = edge.AsCurve()
                vals.append(c.GetEndPoint(0).DotProduct(direction))
                vals.append(c.GetEndPoint(1).DotProduct(direction))
            except:
                pass
    if not vals:
        return None, None
    return min(vals), max(vals)

def align_instance_bbox_to_csv_span(inst, wall, panel):
    """Snap the panel's ACTUAL visible left edge to the CSV target left edge.
    Family-origin-agnostic, rotation-safe. Measures the real solid, not the
    insertion convention and not the polluted world-AABB."""
    if not ENABLE_BBOX_SPAN_ALIGNMENT:
        return False
    try:
        origin, direction = _get_csv_origin_and_dir(panel, wall)
        if origin is None or direction is None:
            return False

        x_in     = _safe_float(panel.get("x_in", 0.0))
        width_in = _safe_float(panel.get("width_in", 0.0))
        if width_in <= 0:
            return False

        target_left  = origin.DotProduct(direction) + _feet(x_in)
        target_right = target_left + _feet(width_in)

        actual_left, actual_right = _panel_solid_span_along_dir(inst, direction)
        if actual_left is None:
            print("  [ALIGN] {0}: no solid geometry, skipped".format(panel.get("panel_name", "?")))
            return False

        delta = target_left - actual_left
        tol   = _feet(BBOX_ALIGN_TOLERANCE_IN)

        print("  [ALIGN] {0}: target L={1:.3f} R={2:.3f} | actual L={3:.3f} R={4:.3f} ft | "
              "width tgt={5:.2f} act={6:.2f} in | delta {7:.3f} in".format(
              panel.get("panel_name", "?"), target_left, target_right,
              actual_left, actual_right, width_in,
              (actual_right - actual_left) * 12.0, delta * 12.0))

        if abs(delta) > tol:
            ElementTransformUtils.MoveElement(doc, inst.Id, direction * delta)
            print("  [ALIGN] Moved {0} by {1:.3f} in -> left edge flush".format(
                  panel.get("panel_name", "?"), delta * 12.0))
            return True
    except Exception as e:
        print("  [ALIGN] skipped/failed for {0}: {1}".format(panel.get("panel_name", "?"), e))
    return False

def apply_csv_rotation(inst, panel):
    """Pure mathematical rotation based on the CSV vectors, flipped 180 to face sheathing out."""
    dx = _safe_float(panel.get("wall_dir_x", 0.0))
    dy = _safe_float(panel.get("wall_dir_y", 0.0))
    if abs(dx) < 1e-9 and abs(dy) < 1e-9: return
    
    # Add math.pi (180 degrees) so the family's -Y (Sheathing) points to the wall's Exterior Normal
    target_angle = math.atan2(dy, dx) + math.pi
    loc = inst.Location
    
    if hasattr(loc, "Rotation"):
        current_angle = loc.Rotation
        angle_to_rotate = target_angle - current_angle
        
        while angle_to_rotate > math.pi: angle_to_rotate -= 2 * math.pi
        while angle_to_rotate <= -math.pi: angle_to_rotate += 2 * math.pi
        
        if abs(angle_to_rotate) > 1e-5:
            doc.Regenerate()
            axis = Line.CreateBound(loc.Point, loc.Point + XYZ(0, 0, 10))
            ElementTransformUtils.RotateElement(doc, inst.Id, axis, angle_to_rotate)
            print("  [ROTATION] Aligned X-axis & Flipped 180 to {:.2f} deg".format(math.degrees(target_angle)))


def place_panel_family(wall, panel, symbol, extra_z_offset_in=0.0, is_cutout=False):
    if not ensure_symbol_active(symbol):
        return None

    try:
        pt, w_dir, w_norm = compute_panel_base_point(wall, panel, extra_z_offset_in)
    except Exception as e:
        print("[ERROR] Geometry calc failed for {0}: {1}".format(panel.get("panel_name", "?"), e))
        return None

    inst = None
    
    # FORCE UNHOSTED PLACEMENT
    # By tying the family to the Level instead of the Wall, we stop Revit from 
    # auto-flipping the axes based on the wall's drawing direction.
    try:
        lvl = get_wall_base_level(wall)
        if lvl:
            pt_rel = XYZ(pt.X, pt.Y, pt.Z - lvl.Elevation)

            inst = doc.Create.NewFamilyInstance(pt, symbol, StructuralType.NonStructural)
            if extra_z_offset_in == 0:
                    print("  [PLACE] Free-hosted (no level): {0}".format(panel.get("panel_name", "")))
    except Exception as e:
        print("[ERROR] Placement failed for {0}: {1}".format(panel.get("panel_name", "?"), e))
        return None

    doc.Regenerate()


    if ROTATION_OVERRIDE_DEG is not None:
        if abs(ROTATION_OVERRIDE_DEG) > 0.001:
            try:
                axis = Line.CreateBound(pt, pt + XYZ(0, 0, 10))
                ElementTransformUtils.RotateElement(doc, inst.Id, axis, math.radians(ROTATION_OVERRIDE_DEG))
            except: pass
    else:
        apply_csv_rotation(inst, panel)

    try:
        w_in = _safe_float(panel.get("width_in",  0))
        h_in = _safe_float(panel.get("height_in", 0))
        if w_in > 0 and h_in > 0:
            set_size_parameters(inst, w_in, h_in, symbol)
            doc.Regenerate()

            # =================================================================
            # THE WRAP FIX: FORCE HIDDEN SETBACKS TO ZERO
            # Setting the Width parameter can cause the family to re-derive
            # Sheathing Wrap_Left/Right to nonzero values. Zero them AFTER
            # width is set and BEFORE alignment, so the geometry we measure
            # in align is the geometry we want to keep.
            # =================================================================
            try:
                wrap_left = inst.LookupParameter("Sheathing Wrap_Left")
                if wrap_left and not wrap_left.IsReadOnly:
                    wrap_left.Set(0.0)
                wrap_right = inst.LookupParameter("Sheathing Wrap_Right")
                if wrap_right and not wrap_right.IsReadOnly:
                    wrap_right.Set(0.0)
                doc.Regenerate()
            except Exception:
                pass

            align_instance_bbox_to_csv_span(inst, wall, panel)
            doc.Regenerate()
        else:
            print("  [WARN] Zero/missing size for {0}".format(panel.get("panel_name", "?")))
    except Exception as e:
        print("  [WARN] Size/alignment failed for {0}: {1}".format(panel.get("panel_name", "?"), e))
    
    if not is_cutout:
        set_void_parameters_for_cutouts(inst, panel)
        doc.Regenerate()
        # Voids may have shifted the outer bounds via family formulas.
        # Re-snap the left edge to the CSV target.
        try:
            w_in = _safe_float(panel.get("width_in", 0))
            h_in = _safe_float(panel.get("height_in", 0))
            if w_in > 0 and h_in > 0:
                align_instance_bbox_to_csv_span(inst, wall, panel)
                doc.Regenerate()
        except Exception as e:
            print("  [WARN] Re-align after voids failed for {0}: {1}".format(panel.get("panel_name", "?"), e))


    try:
        p = _find_param_by_candidates(inst, ["Name", "Panel Name", "Mark"])
        if p and not p.IsReadOnly:
            p.Set(panel.get("panel_name", ""))
    except: pass

    return inst

# ========== STANDARD HELPERS ==========

def get_element_name(element):
    try:
        p = element.get_Parameter(BuiltInParameter.SYMBOL_NAME)
        if p: return p.AsString()
    except: pass
    try: return element.Name
    except: return "Unknown"

def get_family_name(symbol):
    try: return symbol.Family.Name
    except: return "Unknown"

def get_all_family_symbols():
    collector = FilteredElementCollector(doc).OfClass(FamilySymbol)
    families_dict = {}
    for symbol in collector:
        families_dict.setdefault(get_family_name(symbol), []).append(symbol)
    return families_dict

def get_panel_family_symbol(family_name):
    from pyrevit import forms
    if family_name:
        collector = FilteredElementCollector(doc).OfClass(FamilySymbol)
        for s in collector:
            if get_family_name(s) == family_name:
                return s, False
    families_dict = get_all_family_symbols()
    family_names  = sorted(families_dict.keys())
    family_names.insert(0, "< Use DirectShape (3D Solid Panels) >")
    selected_family = forms.SelectFromList.show(
        family_names, title="Select Panel Placement Method", button_name="Select", multiselect=False)
    if not selected_family: return None, False
    if selected_family == "< Use DirectShape (3D Solid Panels) >": return None, True
    symbols = families_dict[selected_family]
    if len(symbols) == 1: return symbols[0], False
    symbol_names  = [get_element_name(s) for s in symbols]
    selected_type = forms.SelectFromList.show(
        symbol_names, title="Select Family Type", button_name="Select", multiselect=False)
    if not selected_type: return symbols[0], False
    for symbol in symbols:
        if get_element_name(symbol) == selected_type:
            return symbol, False
    return symbols[0], False

def ensure_symbol_active(symbol):
    try:
        if not symbol.IsActive: symbol.Activate()
        return True
    except: return False

def _find_param_by_candidates(element, candidates):
    for p in element.Parameters:
        try:
            nm = p.Definition.Name
            if nm and any(nm == c for c in candidates): return p
        except: continue
    for p in element.Parameters:
        try:
            nm = p.Definition.Name
            if nm and any(nm.lower() == c.lower() for c in candidates): return p
        except: continue
    lower_cands = [c.lower() for c in candidates]
    for p in element.Parameters:
        try:
            nm = p.Definition.Name
            if nm and any(c in nm.lower() for c in lower_cands): return p
        except: continue
    return None

def set_size_parameters(inst, width_in, height_in, symbol=None):
    width_ft  = _feet(width_in)
    height_ft = _feet(height_in)
    changed   = False

    w_param = _find_param_by_candidates(inst, WIDTH_PARAM_CANDIDATES)
    h_param = _find_param_by_candidates(inst, HEIGHT_PARAM_CANDIDATES)

    if w_param: print("    [SIZE] Width param:  {0}".format(w_param.Definition.Name))
    if h_param: print("    [SIZE] Height param: {0}".format(h_param.Definition.Name))

    try:
        if w_param and not w_param.IsReadOnly:
            w_param.Set(width_ft);  changed = True
        if h_param and not h_param.IsReadOnly:
            h_param.Set(height_ft); changed = True
    except: pass

    if not changed and ALLOW_TYPE_PARAM_CHANGE and symbol:
        try:
            wtp = _find_param_by_candidates(symbol, WIDTH_PARAM_CANDIDATES)
            htp = _find_param_by_candidates(symbol, HEIGHT_PARAM_CANDIDATES)
            if wtp and not wtp.IsReadOnly: wtp.Set(width_ft)
            if htp and not htp.IsReadOnly: htp.Set(height_ft)
        except: pass

def get_wall_base_level(wall):
    try:
        lvl_id = wall.get_Parameter(BuiltInParameter.WALL_BASE_CONSTRAINT).AsElementId()
        if lvl_id and lvl_id.IntegerValue > 0:
            level = doc.GetElement(lvl_id)
            if level:
                return level
    except:
        pass
    try:
        wall_base_z = get_wall_base_elevation(wall)
        levels = FilteredElementCollector(doc).OfCategory(
            BuiltInCategory.OST_Levels).WhereElementIsNotElementType().ToElements()
        closest = None
        closest_dist = float('inf')
        for lvl in levels:
            dist = abs(lvl.Elevation - wall_base_z)
            if dist < closest_dist:
                closest_dist = dist
                closest = lvl
        if closest:
            print("  [WARN] Wall base level not found via param, using closest level: "
                  "{0} @ {1:.2f} ft".format(closest.Name, closest.Elevation))
            return closest
    except:
        pass
    return None

def create_panel_as_direct_shape(wall, panel):
    try:
        pt, w_dir, w_norm = compute_panel_base_point(wall, panel)
        w_ft      = _feet(_safe_float(panel.get("width_in",  0)))
        h_ft      = _feet(_safe_float(panel.get("height_in", 0)))
        half_w_ft = w_ft / 2.0
        thk       = 1.0 / 12.0
        origin = pt - (w_dir * half_w_ft)
        v1 = origin + (w_norm * 0.01)
        v2 = v1 + (w_dir * w_ft)
        v3 = v2 + XYZ(0, 0, h_ft)
        v4 = v1 + XYZ(0, 0, h_ft)
        v5 = origin + (w_norm * (0.01 + thk))
        v6 = v5 + (w_dir * w_ft)
        v7 = v6 + XYZ(0, 0, h_ft)
        v8 = v5 + XYZ(0, 0, h_ft)
        lines = [
            Line.CreateBound(v1, v2), Line.CreateBound(v2, v3),
            Line.CreateBound(v3, v4), Line.CreateBound(v4, v1),
            Line.CreateBound(v5, v6), Line.CreateBound(v6, v7),
            Line.CreateBound(v7, v8), Line.CreateBound(v8, v5),
            Line.CreateBound(v1, v5), Line.CreateBound(v2, v6),
            Line.CreateBound(v3, v7), Line.CreateBound(v4, v8),
        ]
        ds = DirectShape.CreateElement(doc, ElementId(int(BuiltInCategory.OST_GenericModel)))
        ds.SetShape(lines)
        ds.Name = panel.get("panel_name", "PanelSolid")
        print("  [DS] Created: {0}".format(ds.Name))
        return ds
    except Exception as e:
        print("  [DS] Failed: {0}".format(e))
        return None

def create_cutout_visualization(wall, panel, cutout_data, symbol, use_ds):
    if not use_ds and symbol:
        try:
            g_x = _safe_float(panel.get("x_in", 0)) + _safe_float(cutout_data.get("x_in", 0))
            g_y = _safe_float(panel.get("y_in", 0)) + _safe_float(cutout_data.get("y_in", 0))
            fake_panel = panel.copy()
            fake_panel.update({
                "panel_name": "CUT_" + str(cutout_data.get("id", "")),
                "x_in":       g_x,
                "y_in":       g_y,
                "width_in":   cutout_data.get("width_in",  0),
                "height_in":  cutout_data.get("height_in", 0),
                "cutouts":    [],
            })
            place_panel_family(wall, fake_panel, symbol, extra_z_offset_in=2.0, is_cutout=True)
            return True
        except: pass
    return False


# ========== MAIN ==========

def main():
    print("--- PANEL PLACEMENT: CORE CENTER ALIGNMENT WITH VOID CONTROL ---")

    if USE_FOLDER_PICKER:
        path = _pick_input_folder(DEFAULT_INPUT_DIR)
        if not path: return
        panels_path = os.path.join(path, PANELS_FILE)
    else:
        path = DEFAULT_INPUT_DIR or os.getcwd()
        panels_path = os.path.join(path, PANELS_FILE)

    if not os.path.exists(panels_path):
        print("CSV not found: " + panels_path)
        return

    panels = []
    # Open with utf-8-sig encoding so the BOM (\ufeff) is automatically stripped
    # from the first column name. Without this, csv.DictReader sees the key as
    # '\ufeffpanel_name' instead of 'panel_name', making row.get('panel_name')
    # return None for every row — causing every panel to show as [PLACE] None.
    try:
        f = open(panels_path, 'r', encoding='utf-8-sig')
    except TypeError:
        # IronPython 2: open() doesn't support encoding kwarg;
        # use codecs.open which strips BOM automatically with utf-8-sig
        import codecs
        f = codecs.open(panels_path, 'r', encoding='utf-8-sig')
    with f:
        reader = csv.DictReader(f)
        for row in reader:
            try:    cutouts = json.loads(row.get("cutouts_json", "[]"))
            except: cutouts = []
            panels.append({
                "wall_id":        norm_id(row.get("wall_id")),
                "x_in":           row.get("x_in"),
                "y_in":           row.get("y_in"),
                "width_in":       row.get("width_in"),
                "height_in":      row.get("height_in"),
                "x_ref":          row.get("x_ref"),
                "panel_name":     row.get("panel_name"),
                "rotation_deg":   row.get("rotation_deg"),
                "cutouts":        cutouts,
                # Wall geometry baked in by panel_calculator (Fix 2).
                # compute_panel_base_point uses these to bypass Revit re-derivation.
                "wall_origin_x":  row.get("wall_origin_x"),
                "wall_origin_y":  row.get("wall_origin_y"),
                "wall_origin_z":  row.get("wall_origin_z"),
                "wall_dir_x":     row.get("wall_dir_x"),
                "wall_dir_y":     row.get("wall_dir_y"),
                "wall_dir_z":     row.get("wall_dir_z"),
                "wall_normal_x":  row.get("wall_normal_x"),
                "wall_normal_y":  row.get("wall_normal_y"),
                "wall_normal_z":  row.get("wall_normal_z"),
            })

    print("Loaded {0} panels.".format(len(panels)))

    sym, use_ds = get_panel_family_symbol(PANEL_FAMILY_NAME)
    if not sym and not use_ds: return

    from pyrevit import forms
    global X_REF_OVERRIDE, ROTATION_OVERRIDE_DEG

    if not use_ds:
        print("Using Family: " + get_family_name(sym))

        xref_ops = ["Use CSV Default", "Force Start (Left)", "Force End (Right)"]
        res = forms.SelectFromList.show(xref_ops, button_name="Set X Ref", multiselect=False)
        if res == xref_ops[1]: X_REF_OVERRIDE = "start"
        elif res == xref_ops[2]: X_REF_OVERRIDE = "end"

        rot_ops = ["Use CSV Rotation", "Force 0", "Force 90", "Force -90", "Force 180"]
        res = forms.SelectFromList.show(rot_ops, button_name="Set Rotation", multiselect=False)
        if res == rot_ops[1]: ROTATION_OVERRIDE_DEG = 0.0
        elif res == rot_ops[2]: ROTATION_OVERRIDE_DEG = 90.0
        elif res == rot_ops[3]: ROTATION_OVERRIDE_DEG = -90.0
        elif res == rot_ops[4]: ROTATION_OVERRIDE_DEG = 180.0

    panels_map = {}
    for p in panels:
        panels_map.setdefault(p["wall_id"], []).append(p)
        
    # --- VISUALIZATION SETUP ---
    active_view = doc.ActiveView
    
    # 1. Prepare Wall Transparency Override
    wall_override = OverrideGraphicSettings()
    wall_override.SetSurfaceTransparency(80)
    
    # 2. Get the Solid Fill pattern ID for the panels
    solid_fill_collector = FilteredElementCollector(doc).OfClass(FillPatternElement)
    solid_fill_id = None
    for fp in solid_fill_collector:
        if fp.GetFillPattern().IsSolidFill:
            solid_fill_id = fp.Id
            break

    t = Transaction(doc, "Place Panels with Void Control and Colorization")
    t.Start()

    if sym and not use_ds:
        try:
            if not sym.IsActive:
                sym.Activate()
                doc.Regenerate()
        except Exception as e:
            print("[WARN] Could not activate symbol: {0}".format(e))

    count = 0
    try:
        for wid, wall_panels in panels_map.items():
            wall = get_wall_by_id(wid)
            if not wall:
                print("Wall {0} not found.".format(wid))
                continue

            print("\n--- Wall {0} ---".format(wid))
            
            # --- APPLY WALL TRANSPARENCY ---
            try:
                active_view.SetElementOverrides(wall.Id, wall_override)
            except Exception as e:
                print("  [WARN] Failed to set transparency for Wall {0}: {1}".format(wid, e))
                
            for p in wall_panels:
                if use_ds:
                    res = create_panel_as_direct_shape(wall, p)
                else:
                    res = place_panel_family(wall, p, sym)
                    
                if res: 
                    count += 1
                    
                    # --- APPLY PANEL COLOR OVERRIDE ---
                    if solid_fill_id:
                        try:
                            # Extract the base type name from the panel_name (e.g. "T01-P01" -> "T01")
                            p_name = p.get("panel_name", "Default")
                            type_name = p_name.split('-')[0] if '-' in p_name else p_name
                            
                            panel_color = get_color_from_string(type_name)
                            
                            panel_override = OverrideGraphicSettings()
                            panel_override.SetSurfaceForegroundPatternId(solid_fill_id)
                            panel_override.SetSurfaceForegroundPatternColor(panel_color)
                            
                            active_view.SetElementOverrides(res.Id, panel_override)
                        except Exception as e:
                            print("  [WARN] Failed to colorize panel {0}: {1}".format(p.get("panel_name", "?"), e))

                if SHOW_CUTOUTS:
                    for c in p["cutouts"]:
                        create_cutout_visualization(wall, p, c, sym, use_ds)

        t.Commit()
        print("\nDone. Placed {0} panels.".format(count))

    except Exception as e:
        print("[ERROR] Placement loop failed: {0}".format(e))
        import traceback
        traceback.print_exc()
        try:
            t.RollBack()
        except:
            pass
        print("Transaction rolled back.")


if __name__ == "__main__":
    main()