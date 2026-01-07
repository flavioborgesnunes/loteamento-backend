from __future__ import annotations

import json
import math
from typing import Any, Dict, Optional, Tuple

from django.contrib.gis.geos import GEOSGeometry, MultiPolygon
from django.db import transaction
from pyproj import Transformer
from shapely import affinity
from shapely.geometry import LineString
from shapely.geometry import MultiPolygon as ShpMultiPolygon
from shapely.geometry import Polygon, mapping, shape
from shapely.ops import transform as shp_transform
from shapely.ops import unary_union

# ----------------------------
# Helpers
# ----------------------------


def _anchor_start_rot_from_linha_base(
    *,
    linha_base: dict,
    tf_4326_to_m: Transformer,
    angle_deg: float,
    origin_xy,
    bounds,
    pos_h: str,
    pos_v: str,
):
    """
    ✅ Escolhe o endpoint âncora de forma determinística (independente da direção desenhada).
    Critério:
      - se pos_h == 'esquerda': escolhe o endpoint com MENOR X rotacionado
        senão: escolhe o endpoint com MAIOR X rotacionado
      - desempate por Y (topo/base), conforme pos_v
    Retorna (anchor_x, anchor_y, other_x, other_y) no sistema rotacionado.
    """
    line_m = _line_m_from_linha_base_geojson(linha_base, tf_4326_to_m)
    if line_m is None or line_m.is_empty:
        return None

    try:
        pA = line_m.coords[0]
        pB = line_m.coords[-1]
    except Exception:
        return None

    # rotaciona endpoints
    a_rot = _rotate_align(LineString([pA, pA]), float(
        angle_deg), origin_xy).coords[0]
    b_rot = _rotate_align(LineString([pB, pB]), float(
        angle_deg), origin_xy).coords[0]

    ax, ay = float(a_rot[0]), float(a_rot[1])
    bx, by = float(b_rot[0]), float(b_rot[1])

    # escolha por X conforme lado de início
    if pos_h == "esquerda":
        primary = min(ax, bx)
        candidates = []
        if ax == primary:
            candidates.append(("A", ax, ay))
        if bx == primary:
            candidates.append(("B", bx, by))
    else:
        primary = max(ax, bx)
        candidates = []
        if ax == primary:
            candidates.append(("A", ax, ay))
        if bx == primary:
            candidates.append(("B", bx, by))

    # desempate por Y conforme topo/base
    if len(candidates) > 1:
        if pos_v == "topo":
            # mais perto do topo = maior Y (no sistema rotacionado, topo é maxy)
            chosen = max(candidates, key=lambda t: t[2])
        else:
            chosen = min(candidates, key=lambda t: t[2])
    else:
        chosen = candidates[0]

    tag, cx, cy = chosen

    # other endpoint
    if tag == "A":
        ox, oy = bx, by
    else:
        ox, oy = ax, ay

    # clamp dentro dos bounds (segurança)
    minx, miny, maxx, maxy = bounds
    cx = min(max(cx, minx), maxx)
    cy = min(max(cy, miny), maxy)

    return (cx, cy, ox, oy)


def _line_m_from_linha_base_geojson(linha_base: dict, tf_4326_to_m: Transformer):
    if not isinstance(linha_base, dict):
        return None
    geom = linha_base.get("geometry") if linha_base.get(
        "type") == "Feature" else linha_base
    if not (isinstance(geom, dict) and geom.get("type") == "LineString"):
        return None
    coords = geom.get("coordinates") or []
    if not isinstance(coords, list) or len(coords) < 2:
        return None

    try:
        pts_m = [tf_4326_to_m.transform(float(x), float(y))
                 for (x, y) in coords]
        return LineString(pts_m)
    except Exception:
        return None


def _infer_corner_from_linha_base(*, linha_base: dict, tf_4326_to_m: Transformer, angle_deg: float, origin_xy, bounds):
    """
    Decide o lado de início baseado na DISTÂNCIA MÍNIMA da linha base às bordas do bounds rotacionado.
    Funciona bem para linhas pequenas e diagonais.
    Também retorna offsets (distâncias) para poder ancorar o cursor exatamente na linha.
    """
    line_m = _line_m_from_linha_base_geojson(linha_base, tf_4326_to_m)
    if line_m is None or line_m.is_empty:
        return None

    line_rot = _rotate_align(line_m, angle_deg, origin_xy)
    if line_rot.is_empty:
        return None

    minx, miny, maxx, maxy = bounds

    # linhas das bordas do retângulo rotacionado
    left_edge = LineString([(minx, miny), (minx, maxy)])
    right_edge = LineString([(maxx, miny), (maxx, maxy)])
    bottom_edge = LineString([(minx, miny), (maxx, miny)])
    top_edge = LineString([(minx, maxy), (maxx, maxy)])

    d_left = float(line_rot.distance(left_edge))
    d_right = float(line_rot.distance(right_edge))
    d_bottom = float(line_rot.distance(bottom_edge))
    d_top = float(line_rot.distance(top_edge))

    # escolhe por menor distância (e não por centróide)
    pos_h = "esquerda" if d_left <= d_right else "direita"
    pos_v = "topo" if d_top <= d_bottom else "base"

    return {
        "pos_h": pos_h,
        "pos_v": pos_v,
        "offsets_to_edges": {
            "left": d_left,
            "right": d_right,
            "top": d_top,
            "bottom": d_bottom,
        }
    }


def _as_bool(v, default: bool = False) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("1", "true", "t", "yes", "y", "sim"):
            return True
        if s in ("0", "false", "f", "no", "n", "nao", "não"):
            return False
    return default


def _geos_to_shp(g: GEOSGeometry):
    if not g or getattr(g, "empty", False):
        return None
    return shape(json.loads(g.geojson))


def _ensure_mpoly_shp(g):
    if g is None or getattr(g, "is_empty", False):
        return None
    if g.geom_type == "Polygon":
        return ShpMultiPolygon([g])
    if g.geom_type == "MultiPolygon":
        return g
    polys = []
    if hasattr(g, "geoms"):
        polys = [x for x in g.geoms if x.geom_type ==
                 "Polygon" and not x.is_empty]
    if polys:
        return ShpMultiPolygon(polys)
    return None


def _shp_to_geos_mpoly_4326(g_shp) -> MultiPolygon | None:
    if g_shp is None or getattr(g_shp, "is_empty", False):
        return None
    if g_shp.geom_type == "Polygon":
        g_shp = ShpMultiPolygon([g_shp])
    elif g_shp.geom_type == "MultiPolygon":
        pass
    else:
        polys = []
        if hasattr(g_shp, "geoms"):
            polys = [x for x in g_shp.geoms if x.geom_type ==
                     "Polygon" and not x.is_empty]
        if polys:
            g_shp = ShpMultiPolygon(polys)
        else:
            return None

    gj = {"type": "MultiPolygon", "coordinates": mapping(g_shp)["coordinates"]}
    gg = GEOSGeometry(json.dumps(gj), srid=4326)
    if gg.geom_type == "Polygon":
        gg = MultiPolygon(gg, srid=4326)
    else:
        gg.srid = 4326
    return gg


def _proj_shp(geom, tf: Transformer):
    return shp_transform(tf.transform, geom)


def _rotate_align(g, angle_deg: float, origin_xy):
    return affinity.rotate(g, -angle_deg, origin=origin_xy, use_radians=False)


def _rotate_back(g, angle_deg: float, origin_xy):
    return affinity.rotate(g, angle_deg, origin=origin_xy, use_radians=False)


def _estimate_orientation_deg_from_top_edge(al_m) -> float:
    try:
        minx, miny, maxx, maxy = al_m.bounds
        h = maxy - miny
        if h <= 1e-6:
            return 0.0

        y_threshold = maxy - 0.15 * h
        best_len = -1.0
        best_ang = 0.0

        geoms = list(al_m.geoms) if al_m.geom_type == "MultiPolygon" else [
            al_m]
        for poly in geoms:
            if poly.is_empty:
                continue
            ring = list(poly.exterior.coords)
            if len(ring) < 2:
                continue
            for i in range(len(ring) - 1):
                (x1, y1) = ring[i]
                (x2, y2) = ring[i + 1]
                ymid = 0.5 * (y1 + y2)
                if ymid < y_threshold:
                    continue
                dx = x2 - x1
                dy = y2 - y1
                seg_len = math.hypot(dx, dy)
                if seg_len > best_len:
                    best_len = seg_len
                    best_ang = math.degrees(math.atan2(dy, dx))

        return float(best_ang) if best_len > 0 else 0.0
    except Exception:
        return 0.0


def _angle_from_linha_base_geojson(linha_base: dict, tf_4326_to_m: Transformer) -> Optional[float]:
    if not isinstance(linha_base, dict):
        return None
    geom = linha_base.get("geometry") if linha_base.get(
        "type") == "Feature" else linha_base
    if not (isinstance(geom, dict) and geom.get("type") == "LineString"):
        return None
    try:
        coords = geom.get("coordinates") or []
        if len(coords) < 2:
            return None
        (x1, y1) = coords[0]
        (x2, y2) = coords[-1]
        (mx1, my1) = tf_4326_to_m.transform(x1, y1)
        (mx2, my2) = tf_4326_to_m.transform(x2, y2)
        dx = mx2 - mx1
        dy = my2 - my1
        if abs(dx) < 1e-9 and abs(dy) < 1e-9:
            return None
        return float(math.degrees(math.atan2(dy, dx)))
    except Exception:
        return None


def _pick_corner(preferencia: str) -> Tuple[str, str]:
    pref = (preferencia or "NW").upper().strip()
    if pref == "NE":
        return ("direita", "topo")
    if pref == "SW":
        return ("esquerda", "base")
    if pref == "SE":
        return ("direita", "base")
    return ("esquerda", "topo")


def _row_band(bounds, prof_quarteirao: float, pos_v: str, row_index: int, passo_faixa: float) -> Optional[Polygon]:
    minx, miny, maxx, maxy = bounds
    if pos_v == "topo":
        y_top = maxy - row_index * passo_faixa
        y1 = y_top
        y0 = y_top - prof_quarteirao
    else:
        y_bottom = miny + row_index * passo_faixa
        y0 = y_bottom
        y1 = y_bottom + prof_quarteirao

    y0 = max(y0, miny)
    y1 = min(y1, maxy)
    if y1 <= y0:
        return None

    return Polygon([(minx, y0), (maxx, y0), (maxx, y1), (minx, y1), (minx, y0)])


def _clip_from_x(bounds, x_cursor: float, pos_h: str) -> Polygon:
    minx, miny, maxx, maxy = bounds
    if pos_h == "esquerda":
        x0, x1 = x_cursor, maxx
    else:
        x0, x1 = minx, x_cursor
    return Polygon([(x0, miny), (x1, miny), (x1, maxy), (x0, maxy), (x0, miny)])


def _pick_component_near_side(g_mpoly, pos_h: str):
    if g_mpoly is None or g_mpoly.is_empty:
        return None
    polys = list(g_mpoly.geoms) if g_mpoly.geom_type == "MultiPolygon" else [
        g_mpoly]
    polys = [p for p in polys if (
        p is not None and not p.is_empty and p.area > 1e-6)]
    if not polys:
        return None
    if pos_h == "esquerda":
        return min(polys, key=lambda p: p.bounds[0])
    return max(polys, key=lambda p: p.bounds[2])


def _fc_from_geos_queryset(qs, prop_builder=None) -> Dict[str, Any]:
    feats = []
    for row in qs:
        g = getattr(row, "geom", None)
        if not g or g.empty:
            continue
        geom = json.loads(g.geojson)
        props = prop_builder(row) if prop_builder else {}
        feats.append(
            {"type": "Feature", "geometry": geom, "properties": props})
    return {"type": "FeatureCollection", "features": feats}


def _remaining_rot_for_version(*, versao, inner_rot, tf_4326_to_m: Transformer, angle_deg: float, origin_xy):
    from parcelamento.models import Quarteirao

    qs = Quarteirao.objects.filter(versao=versao).only("geom")
    if not qs.exists():
        return inner_rot

    geoms = []
    for row in qs:
        g = row.geom
        if not g or g.empty:
            continue
        shp4326 = shape(json.loads(g.geojson))
        shp_m = _ensure_mpoly_shp(_proj_shp(shp4326, tf_4326_to_m))
        if shp_m is None or shp_m.is_empty:
            continue
        shp_rot = _ensure_mpoly_shp(_rotate_align(
            shp_m, angle_deg, origin_xy).buffer(0))
        if shp_rot and not shp_rot.is_empty:
            geoms.append(shp_rot)

    if not geoms:
        return inner_rot

    used = unary_union(geoms).buffer(0)
    rem = inner_rot.difference(used).buffer(0)
    rem = _ensure_mpoly_shp(rem)
    return rem if rem and not rem.is_empty else inner_rot


def _clip_len_from_side(poly: Polygon, bounds, pos_h: str, max_len: float):
    """
    Recorta um Polygon (já em coords rotacionadas) para no máximo max_len
    medido a partir do lado inicial (esquerda ou direita).
    """
    if not poly or poly.is_empty:
        return poly
    minx, miny, maxx, maxy = bounds
    max_len = float(max_len or 0)
    if max_len <= 0:
        return poly

    if pos_h == "esquerda":
        x1 = min(poly.bounds[0] + max_len, maxx)
        clip = Polygon([(minx, miny), (x1, miny), (x1, maxy),
                       (minx, maxy), (minx, miny)])
    else:
        x0 = max(poly.bounds[2] - max_len, minx)
        clip = Polygon([(x0, miny), (maxx, miny),
                       (maxx, maxy), (x0, maxy), (x0, miny)])

    out = poly.intersection(clip).buffer(0)
    return out


def _clip_len_from_anchor_x(poly: Polygon, *, bounds, pos_h: str, anchor_x: float, max_len: float):
    """
    Recorta um Polygon (em coords rotacionadas) para no máximo max_len,
    medido a partir de anchor_x (ponto inicial da linha base).
    """
    if not poly or poly.is_empty:
        return poly

    minx, miny, maxx, maxy = bounds
    max_len = float(max_len or 0)
    if max_len <= 0:
        return poly

    anchor_x = float(anchor_x)

    if pos_h == "esquerda":
        x0 = max(minx, anchor_x)
        x1 = min(maxx, x0 + max_len)
    else:
        x1 = min(maxx, anchor_x)
        x0 = max(minx, x1 - max_len)

    clip = Polygon(
        [(x0, miny), (x1, miny), (x1, maxy), (x0, maxy), (x0, miny)])
    return poly.intersection(clip).buffer(0)


# ----------------------------
# Core
# ----------------------------

def run_incremental_quarteiroes_calcadas(
    *,
    project,
    restricoes,
    versao,
    creating_new: bool,
    linha_base: Optional[dict],
    params: Dict[str, Any],
    max_quarteiroes: int,
    user,
) -> Dict[str, Any]:
    """
    Regras:
      - prof_lote_m + fileiras => prof_quarteirao (obrigatório)
      - compr_max_quarteirao_m SEMPRE preenchido e SEMPRE respeitado
      - pode trocar linha_base com versao existente usando params.start_new_phase=true
        (gera “outra direção”; a sobra tende a ficar no meio)
    """
    from parcelamento.models import Calcada, ParcelamentoVersao, Quarteirao

    # AL (4674)
    al_geos = getattr(restricoes, "area_loteavel", None) or getattr(
        restricoes, "aoi_snapshot", None)
    if not al_geos or al_geos.empty:
        raise ValueError(
            "Restrição sem area_loteavel nem aoi_snapshot (AL vazia).")

    if getattr(al_geos, "srid", None) != 4674:
        al_geos = al_geos.clone()
        al_geos.transform(4674)

    # params
    preferencia = str(params.get("preferencia_cardinal")
                      or "NW").upper().strip()

    fileiras = int(params.get("fileiras") or 2)
    fileiras = 1 if fileiras not in (1, 2) else fileiras

    prof_lote_m = float(params.get("prof_lote_m")
                        or params.get("prof_min_m") or 25.0)
    if prof_lote_m <= 0:
        raise ValueError("prof_lote_m deve ser > 0.")
    prof_quarteirao = float(prof_lote_m * fileiras)

    calcada_largura_m = float(params.get("calcada_largura_m") or 2.5)
    calcada_encosta_aoi = _as_bool(params.get(
        "calcada_encosta_aoi"), default=False)

    larg_rua_horiz_m = float(params.get("larg_rua_horiz_m") or 12.0)
    larg_rua_vert_m = float(params.get("larg_rua_vert_m") or 12.0)
    frente_min_m = float(params.get("frente_min_m") or 10.0)

    # ✅ agora é obrigatório/sempre preenchido
    compr_max_quarteirao_m = float(
        params.get("compr_max_quarteirao_m") or 120.0)
    if compr_max_quarteirao_m <= 0:
        compr_max_quarteirao_m = 120.0

    srid_calc = int(params.get("srid_calc") or 3857)
    orientacao_modo = params.get("orientacao_modo") or "auto_top_edge"
    orientacao_graus = params.get("orientacao_graus", None)

    girar_90 = _as_bool(params.get("girar_90"), default=False)

    start_new_phase = _as_bool(params.get("start_new_phase"), default=False)

    # transforms
    tf_4674_to_m = Transformer.from_crs(4674, srid_calc, always_xy=True)
    tf_m_to_4674 = Transformer.from_crs(srid_calc, 4674, always_xy=True)
    tf_4674_to_4326 = Transformer.from_crs(4674, 4326, always_xy=True)
    tf_4326_to_m = Transformer.from_crs(4326, srid_calc, always_xy=True)

    al_shp_4674 = _ensure_mpoly_shp(_geos_to_shp(al_geos))
    if al_shp_4674 is None or al_shp_4674.is_empty:
        raise ValueError("AL inválida após conversão (shapely).")

    al_m = _ensure_mpoly_shp(_proj_shp(al_shp_4674, tf_4674_to_m).buffer(0))
    if al_m is None or al_m.is_empty:
        raise ValueError("AL métrica vazia.")

    # inner AL
    if (not calcada_encosta_aoi) and calcada_largura_m > 0:
        inner_al_m = _ensure_mpoly_shp(
            al_m.buffer(-calcada_largura_m).buffer(0)) or al_m
    else:
        inner_al_m = al_m

    passo_faixa = float(prof_quarteirao + larg_rua_horiz_m +
                        (2.0 * calcada_largura_m))

    # criar/usar versão (garantindo NOT NULL)
    created = False
    with transaction.atomic():
        if creating_new:
            last_num = (
                ParcelamentoVersao.objects.filter(project=project)
                .exclude(numero__isnull=True)
                .order_by("-numero")
                .values_list("numero", flat=True)
                .first()
            )
            next_num = int(last_num or 0) + 1

            versao = ParcelamentoVersao.objects.create(
                project=project,
                restricoes=restricoes,
                area_loteavel_snapshot=al_geos,
                numero=next_num,

                preferencia_cardinal=preferencia,
                fileiras=fileiras,
                prof_min_m=prof_lote_m,  # aqui guardamos prof do lote
                frente_min_m=frente_min_m,

                calcada_largura_m=calcada_largura_m,
                calcada_encosta_aoi=calcada_encosta_aoi,

                larg_rua_horiz_m=larg_rua_horiz_m,
                larg_rua_vert_m=larg_rua_vert_m,

                compr_max_quarteirao_m=compr_max_quarteirao_m,

                srid_calc=srid_calc,
                step_index=0,
                cursor_state={
                    "phase": 1,
                    "x_cursor_m": 0.0,
                    "row_index": 0,
                    "done": False,
                    "linha_base": linha_base or None,
                },
                debug_last={},
            )
            created = True
        else:
            if versao is None:
                raise ValueError(
                    "versao é obrigatória quando versao_id é fornecido.")
            if versao.project_id != project.id:
                raise ValueError(
                    "versao não pertence ao project da restrição.")

            # ancora na restrição (se necessário)
            if versao.restricoes_id is None:
                versao.restricoes = restricoes
            if versao.area_loteavel_snapshot is None:
                versao.area_loteavel_snapshot = al_geos

            # ✅ sempre garantir NOT NULL
            versao.preferencia_cardinal = preferencia
            versao.fileiras = fileiras
            versao.prof_min_m = prof_lote_m
            versao.frente_min_m = frente_min_m
            versao.calcada_largura_m = calcada_largura_m
            versao.calcada_encosta_aoi = calcada_encosta_aoi
            versao.larg_rua_horiz_m = larg_rua_horiz_m
            versao.larg_rua_vert_m = larg_rua_vert_m
            versao.compr_max_quarteirao_m = compr_max_quarteirao_m
            versao.srid_calc = srid_calc

            if orientacao_graus is not None:
                versao.orientacao_graus = float(orientacao_graus)

            # ✅ trocar linha base “em tempo real” se start_new_phase=true
            if start_new_phase:
                cs = versao.cursor_state or {}
                old_phase = int(cs.get("phase") or 1)
                versao.cursor_state = {
                    **cs,
                    "phase": old_phase + 1,
                    "x_cursor_m": 0.0,
                    "row_index": 0,
                    "done": False,
                    "linha_base": linha_base or cs.get("linha_base"),
                }

            versao.save()

    # ângulo (prioridade: linha_base da fase)
    cs = versao.cursor_state or {}
    linha_base_effective = cs.get("linha_base") or linha_base

    girar_90 = _as_bool(params.get("girar_90"), default=False)

    angle = None
    if linha_base_effective:
        angle = _angle_from_linha_base_geojson(
            linha_base_effective, tf_4326_to_m)

    # fallback se não tiver linha base
    if angle is None:
        if orientacao_modo == "usar_orientacao_graus" and orientacao_graus is not None:
            angle = float(orientacao_graus)
        else:
            angle = _estimate_orientation_deg_from_top_edge(inner_al_m)

    # aplica perpendicular
    if girar_90:
        angle = float(angle) + 90.0

    # normaliza (opcional, mas ajuda debug/consistência)
    angle = ((float(angle) + 180.0) % 360.0) - 180.0

    # rotaciona
    origin = (inner_al_m.centroid.x, inner_al_m.centroid.y)
    inner_rot = _ensure_mpoly_shp(_rotate_align(
        inner_al_m, angle, origin).buffer(0))
    if inner_rot is None or inner_rot.is_empty:
        raise ValueError("Falha ao rotacionar AL interna.")

    bounds = inner_rot.bounds

    # default pelo preferencia_cardinal
    pos_h, pos_v = _pick_corner(preferencia)

    # ✅ se tiver linha base, ela manda no lado de início (nascer junto dela)
    if linha_base_effective:
        inferred = _infer_corner_from_linha_base(
            linha_base=linha_base_effective,
            tf_4326_to_m=tf_4326_to_m,
            angle_deg=float(angle),
            origin_xy=origin,
            bounds=bounds,
        )
        if inferred:
            pos_h = inferred["pos_h"]
            pos_v = inferred["pos_v"]

    done = _as_bool(cs.get("done"), default=False)
    x_cursor_rel = float(cs.get("x_cursor_m") or 0.0)
    row_index = int(cs.get("row_index") or 0)

    debug: Dict[str, Any] = {
        "phase": int((versao.cursor_state or {}).get("phase") or 1),
        "angle_deg": round(float(angle), 6),
        "preferencia_cardinal": preferencia,
        "pos_h": pos_h,
        "pos_v": pos_v,
        "fileiras": fileiras,
        "prof_lote_m": prof_lote_m,
        "prof_quarteirao_m": prof_quarteirao,
        "passo_faixa": passo_faixa,
        "compr_max_quarteirao_m": float(compr_max_quarteirao_m),
        "calcada": {"largura_m": calcada_largura_m, "encosta_aoi": calcada_encosta_aoi},
        "cursor_before": {"x_cursor_m": x_cursor_rel, "row_index": row_index, "done": done},
        "max_quarteiroes": int(max_quarteiroes or 1),
        "attempts": [],
        "created_ids": [],
        "stopped_reason": None,
    }

    if linha_base_effective:
        debug["linha_base_effective"] = True
        if inferred:
            debug["linha_base_anchor"] = inferred
    else:
        debug["linha_base_effective"] = False

        anchor_rot = None
    anchor_x = None
    anchor_y = None

    if linha_base_effective:
        anchor_rot = _anchor_start_rot_from_linha_base(
            linha_base=linha_base_effective,
            tf_4326_to_m=tf_4326_to_m,
            angle_deg=float(angle),
            origin_xy=origin,
            bounds=bounds,
            pos_h=pos_h,
            pos_v=pos_v,
        )
        if anchor_rot:
            anchor_x, anchor_y, other_x, other_y = anchor_rot
            debug["linha_base_anchor_endpoints_rot"] = {
                "anchor": {"x": anchor_x, "y": anchor_y},
                "other": {"x": other_x, "y": other_y},
                "pos_h": pos_h,
                "pos_v": pos_v,
            }

    if done:
        q_qs = Quarteirao.objects.filter(versao=versao).order_by("id")
        c_qs = Calcada.objects.filter(versao=versao).order_by("id")
        q_ids = list(q_qs.values_list("id", flat=True))
        q_index = {qid: i + 1 for i, qid in enumerate(q_ids)}
        return {
            "versao_id": versao.id,
            "created": created,
            "quarteiroes": _fc_from_geos_queryset(q_qs, prop_builder=lambda r: {"id": r.id, "versao_id": versao.id, "numero": q_index.get(r.id)}),
            "calcadas": _fc_from_geos_queryset(c_qs, prop_builder=lambda r: {"id": r.id, "versao_id": versao.id, "largura_m": float(r.largura_m)}),
            "metrics": {"step_index": versao.step_index, "n_quarteiroes_total": len(q_ids), "n_calcadas_total": c_qs.count(), "n_novos": 0},
            "debug": debug,
        }

    # evita colapso: trabalha no restante
    remaining_rot = _remaining_rot_for_version(
        versao=versao,
        inner_rot=inner_rot,
        tf_4326_to_m=tf_4326_to_m,
        angle_deg=float(angle),
        origin_xy=origin,
    )

    minx, miny, maxx, maxy = bounds

    if anchor_x is not None:
        # começa exatamente no X da linha base (em coords rotacionadas)
        x_cursor = float(anchor_x)
    else:
        # fallback antigo
        if pos_h == "esquerda":
            x_cursor = minx + x_cursor_rel
        else:
            x_cursor = maxx - x_cursor_rel

    created_quarteiroes = 0
    max_attempts = max(12, int(max_quarteiroes or 1) * 25)
    attempts = 0

    # área mínima: evita fragmentos
    min_area_ok = max(80.0, 0.08 * abs((maxx - minx) * prof_quarteirao))

    with transaction.atomic():
        while created_quarteiroes < int(max_quarteiroes or 1) and attempts < max_attempts:
            attempts += 1

            band = _row_band(bounds, prof_quarteirao,
                             pos_v, row_index, passo_faixa)
            if band is None:
                done = True
                debug["stopped_reason"] = "no_more_rows"
                break

            side_clip = _clip_from_x(bounds, x_cursor, pos_h)
            band_clip = band.intersection(side_clip).buffer(0)
            if band_clip.is_empty:
                debug["attempts"].append(
                    {"ok": False, "reason": "band_clip_empty", "row_index": row_index})
                row_index += 1
                x_cursor_rel = 0.0
                x_cursor = (minx if pos_h == "esquerda" else maxx)
                continue

            cand = _ensure_mpoly_shp(
                remaining_rot.intersection(band_clip).buffer(0))
            if cand is None or cand.is_empty:
                debug["attempts"].append(
                    {"ok": False, "reason": "no_space_in_band", "row_index": row_index})
                row_index += 1
                x_cursor_rel = 0.0
                x_cursor = (minx if pos_h == "esquerda" else maxx)
                continue

            poly = _pick_component_near_side(cand, pos_h)
            if poly is None or poly.is_empty:
                debug["attempts"].append(
                    {"ok": False, "reason": "no_component", "row_index": row_index})
                row_index += 1
                x_cursor_rel = 0.0
                x_cursor = (minx if pos_h == "esquerda" else maxx)
                continue

            # ✅ respeita compr_max_quarteirao_m (sempre)
            if anchor_x is not None:
                poly = _clip_len_from_anchor_x(
                    poly,
                    bounds=bounds,
                    pos_h=pos_h,
                    anchor_x=float(anchor_x),
                    max_len=compr_max_quarteirao_m,
                )
            else:
                poly = _clip_len_from_side(
                    poly, bounds=bounds, pos_h=pos_h, max_len=compr_max_quarteirao_m)

            poly = _ensure_mpoly_shp(poly.buffer(0))
            if poly is None or poly.is_empty or poly.area < min_area_ok:
                debug["attempts"].append(
                    {"ok": False, "reason": "too_small_after_clip", "row_index": row_index})
                row_index += 1
                x_cursor_rel = 0.0
                x_cursor = (minx if pos_h == "esquerda" else maxx)
                continue

            # volta pro sistema não rotacionado
            q_m = _rotate_back(poly, angle, origin).buffer(0)

            # calçada
            c_m = None
            if calcada_largura_m > 0:
                ring = q_m.buffer(calcada_largura_m).difference(q_m).buffer(0)
                ring_clip = ring.intersection(al_m).buffer(0)
                c_m = ring_clip if not ring_clip.is_empty else None

            # projeta -> 4326
            q_4674 = _proj_shp(q_m, tf_m_to_4674)
            q_4326 = _proj_shp(q_4674, tf_4674_to_4326)
            q_geos = _shp_to_geos_mpoly_4326(_ensure_mpoly_shp(q_4326))
            if q_geos is None or q_geos.empty:
                debug["attempts"].append(
                    {"ok": False, "reason": "q_geos_empty_after_proj", "row_index": row_index})
                row_index += 1
                x_cursor_rel = 0.0
                x_cursor = (minx if pos_h == "esquerda" else maxx)
                continue

            q_obj = Quarteirao.objects.create(
                versao=versao, geom=q_geos, origem="heuristica", created_by_ia=False)

            c_obj = None
            if c_m is not None:
                c_4674 = _proj_shp(c_m, tf_m_to_4674)
                c_4326 = _proj_shp(c_4674, tf_4674_to_4326)
                c_geos = _shp_to_geos_mpoly_4326(_ensure_mpoly_shp(c_4326))
                if c_geos is not None and not c_geos.empty:
                    c_obj = Calcada.objects.create(
                        versao=versao,
                        geom=c_geos,
                        largura_m=calcada_largura_m,
                        origem="heuristica",
                        created_by_ia=False,
                    )

            created_quarteiroes += 1
            debug["created_ids"].append({"quarteirao_id": q_obj.id, "calcada_id": getattr(
                c_obj, "id", None), "row_index": row_index})
            debug["attempts"].append(
                {"ok": True, "row_index": row_index, "area_m2": float(poly.area)})

            # consome imediatamente (evita overlap dentro da mesma chamada)
            remaining_rot = _ensure_mpoly_shp(
                remaining_rot.difference(poly).buffer(0)) or remaining_rot

            # próximo: por padrão vai pra próxima faixa
            row_index += 1
            x_cursor_rel = 0.0
            x_cursor = (minx if pos_h == "esquerda" else maxx)

        # se criou e não gerou nada -> apaga versão
        if created and created_quarteiroes == 0:
            vid = versao.id
            versao.delete()
            raise ValueError(
                f"Não foi possível gerar nenhum quarteirão. Versão #{vid} descartada.")

        # salva estado
        versao.step_index = int(versao.step_index or 0) + \
            int(created_quarteiroes)
        versao.cursor_state = {
            **(versao.cursor_state or {}),
            "x_cursor_m": float(x_cursor_rel),
            "row_index": int(row_index),
            "done": bool(done),
        }
        versao.debug_last = debug
        versao.save(update_fields=["step_index", "cursor_state", "debug_last"])

    q_qs = Quarteirao.objects.filter(versao=versao).order_by("id")
    c_qs = Calcada.objects.filter(versao=versao).order_by("id")
    q_ids = list(q_qs.values_list("id", flat=True))
    q_index = {qid: i + 1 for i, qid in enumerate(q_ids)}

    return {
        "versao_id": versao.id,
        "created": created,
        "quarteiroes": _fc_from_geos_queryset(q_qs, prop_builder=lambda r: {"id": r.id, "versao_id": versao.id, "numero": q_index.get(r.id)}),
        "calcadas": _fc_from_geos_queryset(c_qs, prop_builder=lambda r: {"id": r.id, "versao_id": versao.id, "largura_m": float(r.largura_m)}),
        "metrics": {"step_index": versao.step_index, "n_quarteiroes_total": len(q_ids), "n_calcadas_total": c_qs.count(), "n_novos": int(created_quarteiroes)},
        "debug": debug,
    }
