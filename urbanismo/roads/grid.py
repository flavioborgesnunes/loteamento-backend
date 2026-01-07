from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

from shapely.geometry import GeometryCollection, LineString, MultiLineString

from .geometry import (buffer_lines_as_corridors, ensure_multipolygon,
                       estimate_orientation_deg, line_across_bbox_x,
                       line_across_bbox_y, make_transformers, rotate_back,
                       rotate_to_angle, safe_intersection, to_fc, to_feature,
                       to_shapely, transform_geom)

# ---------- Helpers


def _extract_lines(g):
    if g is None or g.is_empty:
        return None
    if isinstance(g, LineString):
        return g
    if isinstance(g, MultiLineString):
        return g
    if isinstance(g, GeometryCollection):
        lines = []
        for part in g.geoms:
            if isinstance(part, LineString):
                lines.append(part)
            elif isinstance(part, MultiLineString):
                lines.extend(list(part.geoms))
        if not lines:
            return None
        return MultiLineString(lines)
    return None


def _pick_angle(params: Dict[str, Any], al_m) -> float:
    orient = params.get("orientacao_graus", None)
    if orient is not None:
        try:
            return float(orient) % 180.0
        except Exception:
            pass
    return float(estimate_orientation_deg(al_m)) % 180.0


def _gap_bounds_from_ref(ref: float, *, min_mult: float, max_mult: float, tol: float):
    ref = float(ref)
    tol = float(tol)
    min_mult = float(min_mult)
    max_mult = float(max_mult)

    min_gap = min_mult * ref * (1.0 - tol)
    max_gap = max_mult * ref * (1.0 + tol)

    min_gap = max(1.0, float(min_gap))
    max_gap = max(min_gap, float(max_gap))
    return min_gap, max_gap


def _feasible_k_range(total: float, min_gap: float, max_gap: float):
    if total <= 0:
        return (0, -1)
    k_min = int(math.ceil(total / max(max_gap, 1e-9)) - 1)
    k_max = int(math.floor(total / max(min_gap, 1e-9)) - 1)
    k_min = max(0, k_min)
    k_max = max(-1, k_max)
    return k_min, k_max


def _positions_for_k(total: float, k: int) -> List[float]:
    if k <= 0:
        return []
    gap = total / (k + 1)
    return [gap * (i + 1) for i in range(k)]


def _positions_variant(total: float, min_gap: float, max_gap: float, variant: str):
    k_min, k_max = _feasible_k_range(total, min_gap, max_gap)
    if k_max < 1:
        return [], 0, total

    if variant == "low":
        k = k_min
    elif variant == "high":
        k = k_max
    else:
        k = int(round((k_min + k_max) / 2))

    k = max(0, min(k, k_max))
    pos = _positions_for_k(total, k)
    gap = total / (k + 1) if k >= 0 else total
    return pos, k, gap


def compute_constraints(params: Dict[str, Any]) -> Dict[str, float]:
    """
    FIXOS:
      - frente_min_m
      - area_min_m2

    FLEXÍVEL:
      - prof_ideal_m, tol_prof
      - block_len_target, tol_block_len

    Regras flexíveis:
      - Y (paralelas): entre y_min_fundos..y_max_fundos fundos (tol y_tol_fundos)
      - X (travessas): entre x_min_mult..x_max_mult * block_len (tol x_tol)
    """
    frente_min = float(params.get(
        "frente_min_m", params.get("frente", 10.0)) or 10.0)
    area_min = float(params.get("area_min_m2", params.get(
        "area_minima_m2", 200.0)) or 200.0)

    prof_ideal = float(params.get(
        "prof_ideal_m", params.get("prof_min_m", 30.0)) or 30.0)
    tol_down = float(params.get("tol_prof_down", 0.25) or 0.25)
    tol_up = float(params.get("tol_prof_up", 0.50) or 0.50)

    calcada = float(params.get("calcada_largura_m", 2.0) or 2.0)
    fileiras_hint = int(params.get("fileiras", 2) or 2)

    larg_rua = float(params.get("larg_rua_horiz_m",
                     params.get("larg_rua_vert_m", 12.0)) or 12.0)

    # piso matemático (lote viável)
    prof_floor = area_min / max(frente_min, 0.01)
    prof_min_effective = max(prof_floor, prof_ideal * (1.0 - tol_down))
    prof_max_effective = max(prof_min_effective, prof_ideal * (1.0 + tol_up))

    # apenas debug/insight — ainda não estamos loteando aqui
    quadra_depth_min = fileiras_hint * prof_min_effective + 2.0 * calcada
    quadra_depth_max = fileiras_hint * prof_max_effective + 2.0 * calcada

    block_len_target = float(params.get(
        "compr_max_quarteirao_m", 160.0) or 160.0)
    tol_block = float(params.get("tol_block_len", 0.25) or 0.25)

    # ✅ Y: fundos (ajustável) - default 1..2 com tol 10%
    y_min_fundos = float(params.get("y_min_fundos", 1.0) or 1.0)
    y_max_fundos = float(params.get("y_max_fundos", 2.0) or 2.0)
    y_tol_fundos = float(params.get("y_tol_fundos", 0.10) or 0.10)

    y_min_gap, y_max_gap = _gap_bounds_from_ref(
        prof_ideal, min_mult=y_min_fundos, max_mult=y_max_fundos, tol=y_tol_fundos
    )

    # ✅ X: travessas — por block_len (ajustável). Defaults: usa tol_block como range.
    x_min_mult = float(params.get("x_min_mult", 1.0 -
                       tol_block) or (1.0 - tol_block))
    x_max_mult = float(params.get("x_max_mult", 1.0 +
                       tol_block) or (1.0 + tol_block))
    x_tol = float(params.get("x_tol", 0.0) or 0.0)

    x_min_gap, x_max_gap = _gap_bounds_from_ref(
        block_len_target, min_mult=x_min_mult, max_mult=x_max_mult, tol=x_tol
    )

    return {
        "frente_min_m": frente_min,
        "area_min_m2": area_min,
        "prof_ideal": prof_ideal,
        "prof_floor": prof_floor,
        "prof_min_effective": prof_min_effective,
        "prof_max_effective": prof_max_effective,
        "quadra_depth_min": quadra_depth_min,
        "quadra_depth_max": quadra_depth_max,
        "calcada_largura_m": calcada,
        "larg_rua": larg_rua,
        "block_len_target": block_len_target,
        "tol_block_len": tol_block,
        "y_min_fundos": y_min_fundos,
        "y_max_fundos": y_max_fundos,
        "y_tol_fundos": y_tol_fundos,
        "y_min_gap_m": y_min_gap,
        "y_max_gap_m": y_max_gap,
        "x_min_mult": x_min_mult,
        "x_max_mult": x_max_mult,
        "x_tol": x_tol,
        "x_min_gap_m": x_min_gap,
        "x_max_gap_m": x_max_gap,
    }


def generate_grid_roads(
    *,
    al_wgs_geojson: Dict[str, Any],
    params: Dict[str, Any],
    srid_calc: int = 3857,
    angle_override_deg: Optional[float] = None,
    variant: str = "mid",  # low|mid|high
) -> Dict[str, Any]:
    tf = make_transformers(srid_calc)
    al_wgs = ensure_multipolygon(to_shapely(al_wgs_geojson))
    al_m = transform_geom(al_wgs, tf.wgs_to_m)

    origin = (al_m.centroid.x, al_m.centroid.y)
    angle = float(angle_override_deg) if angle_override_deg is not None else _pick_angle(
        params, al_m)

    c = compute_constraints(params)

    # rotaciona AL
    al_r = rotate_to_angle(al_m, angle, origin)
    minx, miny, maxx, maxy = al_r.bounds
    W = maxx - minx
    H = maxy - miny

    xs, kx, gapx = _positions_variant(
        W, c["x_min_gap_m"], c["x_max_gap_m"], variant)
    ys, ky, gapy = _positions_variant(
        H, c["y_min_gap_m"], c["y_max_gap_m"], variant)

    lines_x = [line_across_bbox_x(minx + x, miny, maxy)
               for x in xs]  # travessas
    lines_y = [line_across_bbox_y(miny + y, minx, maxx)
               for y in ys]  # paralelas

    all_lines_r: List[LineString] = []
    all_lines_r.extend(lines_x)
    all_lines_r.extend(lines_y)

    if not all_lines_r:
        return {
            "roads_mask_fc": to_fc([]),
            "roads_axis_fc": to_fc([]),
            "debug": {
                "strategy": "grid",
                "variant": variant,
                "angle_deg": angle,
                "W": W,
                "H": H,
                "n_axis": 0,
                "kx": kx,
                "ky": ky,
                "gapx": gapx,
                "gapy": gapy,
                "x_gap_range": [c["x_min_gap_m"], c["x_max_gap_m"]],
                "y_gap_range": [c["y_min_gap_m"], c["y_max_gap_m"]],
            },
        }

    ml_r = MultiLineString([ln for ln in all_lines_r])
    ml_m = rotate_back(ml_r, angle, origin)

    # clip do eixo pela AL
    axis_m_clipped = safe_intersection(ml_m, al_m)
    axis_m_clipped = _extract_lines(axis_m_clipped)

    # máscaras
    corridors_m = buffer_lines_as_corridors(ml_m, width_m=float(c["larg_rua"]))
    corridors_m = safe_intersection(corridors_m, al_m)

    axis_features = []
    if axis_m_clipped is not None and (not axis_m_clipped.is_empty):
        axis_wgs = transform_geom(axis_m_clipped, tf.m_to_wgs)
        if hasattr(axis_wgs, "geoms"):
            for g in axis_wgs.geoms:
                axis_features.append(to_feature(
                    g, {"strategy": "grid", "variant": variant, "angle_deg": angle, "kx": kx, "ky": ky}))
        else:
            axis_features.append(to_feature(axis_wgs, {
                                 "strategy": "grid", "variant": variant, "angle_deg": angle, "kx": kx, "ky": ky}))

    mask_features = []
    if corridors_m is not None and (not corridors_m.is_empty):
        mask_wgs = transform_geom(corridors_m, tf.m_to_wgs)
        if hasattr(mask_wgs, "geoms"):
            for g in mask_wgs.geoms:
                mask_features.append(to_feature(
                    g, {"strategy": "grid", "variant": variant, "angle_deg": angle, "larg_rua": float(c["larg_rua"]), "kx": kx, "ky": ky}))
        else:
            mask_features.append(to_feature(mask_wgs, {
                                 "strategy": "grid", "variant": variant, "angle_deg": angle, "larg_rua": float(c["larg_rua"]), "kx": kx, "ky": ky}))

    return {
        "roads_mask_fc": to_fc(mask_features),
        "roads_axis_fc": to_fc(axis_features),
        "debug": {
            "strategy": "grid",
            "variant": variant,
            "angle_deg": angle,
            "W": W,
            "H": H,
            "n_axis": len(axis_features),
            "kx": kx,
            "ky": ky,
            "gapx": gapx,
            "gapy": gapy,
            "x_gap_range": [c["x_min_gap_m"], c["x_max_gap_m"]],
            "y_gap_range": [c["y_min_gap_m"], c["y_max_gap_m"]],
        },
    }
