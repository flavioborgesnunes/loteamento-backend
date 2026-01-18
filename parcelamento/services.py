# parcelamento/services.py
from __future__ import annotations

import json
import math
from typing import Dict, List, Optional, Tuple

from django.contrib.gis.geos import GEOSGeometry
from pyproj import Transformer
from shapely import affinity
from shapely.geometry import (GeometryCollection, LineString, MultiLineString,
                              MultiPolygon, Point, Polygon, mapping, shape)
from shapely.ops import split
from shapely.ops import transform as shp_transform
from shapely.ops import unary_union

from .commands.executor import executar_comandos_pre

# ------------------------------------------------------------------------------
# Constantes / SRIDs
# ------------------------------------------------------------------------------
SRID_INPUT = 4674  # SIRGAS 2000 (igual ao app restricoes)


# ------------------------------------------------------------------------------
# Utils básicos
# ------------------------------------------------------------------------------
def _ensure_multipolygon(g):
    if isinstance(g, Polygon):
        return MultiPolygon([g])
    if isinstance(g, MultiPolygon):
        return g
    if hasattr(g, "geom_type") and str(g.geom_type).endswith("Polygon"):
        try:
            return MultiPolygon([Polygon(g.exterior, g.interiors)])
        except Exception:
            pass
    raise ValueError("AL deve ser Polygon/MultiPolygon")


def _ensure_multipoly_from_any(g):
    if g is None:
        return None
    if isinstance(g, (Polygon, MultiPolygon)):
        return _ensure_multipolygon(g)
    if hasattr(g, "geom_type") and str(g.geom_type).endswith("Polygon"):
        return _ensure_multipolygon(g)
    return None


def shapely_transform(geom, transformer: Transformer):
    def _tx_xy(x, y, z=None):
        x2, y2 = transformer.transform(x, y)
        return (x2, y2) if z is None else (x2, y2, z)

    return shp_transform(_tx_xy, geom)


def estimate_orientation_deg(geom_m):
    """
    Estima orientação dominante (0..180) a partir do retângulo mínimo.
    """
    try:
        minrect = geom_m.minimum_rotated_rectangle
        coords = list(minrect.exterior.coords)
        max_d, ang = 0, 0
        for i in range(len(coords) - 1):
            x1, y1 = coords[i]
            x2, y2 = coords[i + 1]
            dx, dy = (x2 - x1, y2 - y1)
            d = math.hypot(dx, dy)
            if d > max_d:
                max_d = d
                ang = math.degrees(math.atan2(dy, dx))
        return ang % 180.0
    except Exception:
        return 0.0


def _gen_parallel_lines_covering_bbox(bbox, spacing, angle_deg, center):
    minx, miny, maxx, maxy = bbox
    W, H = (maxx - minx, maxy - miny)
    diag = math.hypot(W, H) + spacing * 2
    cx, cy = center
    base = LineString([(cx - diag / 2, cy), (cx + diag / 2, cy)])
    base = affinity.rotate(base, angle_deg, origin=(cx, cy), use_radians=False)
    ortho = angle_deg + 90
    lines = []
    n = int((max(W, H) + diag) / spacing) + 4
    for k in range(-n, n + 1):
        l = affinity.translate(
            base,
            xoff=math.cos(math.radians(ortho)) * k * spacing,
            yoff=math.sin(math.radians(ortho)) * k * spacing,
        )
        lines.append(l)
    return lines


def buffer_lines_as_corridors(lines: List[LineString], width_m: float):
    half = max(width_m, 0.0) / 2.0
    return [l.buffer(half, cap_style=2, join_style=2) for l in lines]


def _remover_corridores_extremos(
    al_m,
    corridors: list,
    angle_deg: float,
    origin: tuple[float, float],
) -> list:
    """
    Remove no máximo 1 corredor em cada extremidade (na direção perpendicular ao eixo),
    para não começar/terminar com rua.
    """
    if not corridors:
        return corridors

    try:
        al_al = affinity.rotate(
            al_m, -angle_deg, origin=origin, use_radians=False)
    except Exception:
        return corridors

    if al_al.is_empty:
        return corridors

    infos: list[tuple] = []
    for c in corridors:
        if c is None or c.is_empty:
            continue
        cen = c.centroid
        cen_al = affinity.rotate(
            cen, -angle_deg, origin=origin, use_radians=False)
        infos.append((c, cen_al.y))

    if len(infos) <= 2:
        return [c for c, _ in infos]

    ys = [y for _, y in infos]
    min_y = min(ys)
    max_y = max(ys)
    span = max_y - min_y
    eps = max(span * 0.01, 1e-6)

    kept: list = []
    removed_min = False
    removed_max = False

    for c, y in sorted(infos, key=lambda t: t[1]):
        if not removed_min and abs(y - min_y) <= eps:
            removed_min = True
            continue
        if not removed_max and abs(y - max_y) <= eps:
            removed_max = True
            continue
        kept.append(c)

    if not kept:
        return [c for c, _ in infos]

    return kept


def _geom_from_fc(fc: Optional[dict], to_m: Transformer):
    """
    Converte um FeatureCollection (SRID_INPUT) para união (unary_union) em metros (SRID cálculo).
    """
    if not fc or fc.get("type") != "FeatureCollection":
        return None
    geoms = []
    for f in fc.get("features", []):
        try:
            g = shapely_transform(shape(f.get("geometry")), to_m)
            if not g.is_empty:
                geoms.append(g)
        except Exception:
            pass
    if not geoms:
        return None
    u = unary_union(geoms)
    return u if not u.is_empty else None


def _buffer_centerlines_with_attr(
    ruas_eixo_fc: Optional[dict], tf_in_to_m: Transformer, fallback_width: float
):
    """
    Recebe eixos de rua (LineString) com possível propriedade de largura.
    Faz buffer por (largura/2) para virar corredor de PAVIMENTO.
    """
    if not isinstance(ruas_eixo_fc, dict) or ruas_eixo_fc.get("type") != "FeatureCollection":
        return None
    polys = []
    for f in ruas_eixo_fc.get("features", []):
        try:
            g = shapely_transform(shape(f.get("geometry")), tf_in_to_m)
            if g.is_empty:
                continue
            props = f.get("properties") or {}
            w = float(props.get("largura_m") or props.get(
                "width_m") or fallback_width)
            polys.append(g.buffer(max(w, 0) / 2.0, cap_style=2, join_style=2))
        except Exception:
            pass
    if not polys:
        return None
    u = unary_union(polys)
    return u if not u.is_empty else None


def _extract_centerlines_m(
    ruas_eixo_fc: Optional[dict], tf_in_to_m: Transformer
) -> List[LineString]:
    lines: List[LineString] = []
    if not isinstance(ruas_eixo_fc, dict) or ruas_eixo_fc.get("type") != "FeatureCollection":
        return lines
    for f in ruas_eixo_fc.get("features", []):
        try:
            g = shapely_transform(shape(f.get("geometry")), tf_in_to_m)
            if isinstance(g, LineString) and not g.is_empty:
                lines.append(g)
            elif isinstance(g, MultiLineString) and not g.is_empty:
                lines.extend([seg for seg in g.geoms if isinstance(
                    seg, LineString) and not seg.is_empty])
        except Exception:
            pass
    return lines


def _angle_deg_of_line(line: LineString) -> float:
    x1, y1, x2, y2 = *list(line.coords)[0], *list(line.coords)[-1]
    return math.degrees(math.atan2(y2 - y1, x2 - x1)) % 180.0


def _angle_diff(a: float, b: float) -> float:
    d = abs((a - b) % 180.0)
    return d if d <= 90.0 else 180.0 - d


def _filter_corridors_min_edge_gap(
    al_clean,
    pav_list: list,
    cl_list: list,
    sw_list: list,
    angle_deg: float,
    origin: tuple[float, float],
    axis: str,
    min_gap: float,
):
    """
    Remove corredores (pavimentos) que ficam perto demais da borda da AL,
    garantindo que exista pelo menos 'min_gap' de espaço entre o pavimento
    e a borda (na direção perpendicular à via).

    axis:
      - "y": para vias paralelas (horizontais no frame rotacionado)
      - "x": para vias transversais (verticais no frame rotacionado)
    """
    if not pav_list:
        return pav_list, cl_list, sw_list

    # Rotaciona AL para frame alinhado
    al_al = affinity.rotate(al_clean, -angle_deg,
                            origin=origin, use_radians=False)
    axmin, aymin, axmax, aymax = al_al.bounds

    kept_pav, kept_cl, kept_sw = [], [], []

    for pav, cl, sw in zip(pav_list, cl_list, sw_list):
        pav_al = affinity.rotate(
            pav, -angle_deg, origin=origin, use_radians=False)
        pxmin, pymin, pxmax, pymax = pav_al.bounds

        if axis == "y":
            # distância para borda inferior e superior no eixo Y
            gap_min = pymin - aymin
            gap_max = aymax - pymax
        else:
            # distância para borda esquerda e direita no eixo X
            gap_min = pxmin - axmin
            gap_max = axmax - pxmax

        # mantém apenas se respeitar a folga mínima dos dois lados
        if gap_min >= (min_gap - 1e-6) and gap_max >= (min_gap - 1e-6):
            kept_pav.append(pav)
            kept_cl.append(cl)
            kept_sw.append(sw)

    return kept_pav, kept_cl, kept_sw


def _get_min_gap_to_edge_param(params: dict, prof_min: float) -> float:
    """
    Folga mínima exigida entre via e borda.
    Por padrão = prof_min (1 fundo de lote).
    """
    try:
        v = params.get("min_gap_borda_m")
        if v is None:
            return float(prof_min)
        return max(float(v), float(prof_min))
    except Exception:
        return float(prof_min)


# ------------------------------------------------------------------------------
# Calçadas derivadas das VIAS (por via) + util de montagem
# ------------------------------------------------------------------------------
def _corridor_to_sidewalk(pav_poly, calcada_w: float, al_m):
    """
    Sidewalk = buffer(pav, +calcada) - pav, intersect AL
    """
    w = max(float(calcada_w), 0.0)
    if w <= 0 or pav_poly is None or pav_poly.is_empty:
        return None
    total = pav_poly.buffer(w, cap_style=2, join_style=2)
    if total.is_empty:
        return None
    if al_m is not None and not al_m.is_empty:
        total = total.intersection(al_m)
    if total.is_empty:
        return None
    sw = total.difference(pav_poly)
    if sw.is_empty:
        return None
    return sw


# ------------------------------------------------------------------------------
# Classificação anti-irregularidade (quarteirões -> quarteiroes / areas_vazias)
# ------------------------------------------------------------------------------
def _classificar_quarteiroes_e_vazios(quarteiroes_mp: MultiPolygon, params: dict):
    """
    Retorna (validos_mp, vazios_mp, motivos_por_geom)
    - motivos_por_geom: lista paralela aos geoms em vazios_mp (para properties.motivo)
    """
    if not quarteiroes_mp or quarteiroes_mp.is_empty:
        return None, None, []

    # thresholds (ajustáveis via params)
    min_area = float(params.get("min_area_quarteirao_m2", 400.0))
    min_compact = float(params.get("min_compactness", 0.12)
                        )  # 0..1 (baixo = recortado)
    min_width_index = float(params.get("min_width_index", 1.2))  # A/P (m)
    min_bbox_ratio = float(params.get("min_bbox_ratio", 0.08))  # 0..1

    validos = []
    vazios = []
    motivos = []

    for g in quarteiroes_mp.geoms:
        if g is None or g.is_empty:
            continue

        try:
            area = float(abs(g.area))
            per = float(g.length) if g.length else 0.0
            if per <= 0:
                vazios.append(g)
                motivos.append("degenerado_perimetro_zero")
                continue

            compact = (4.0 * math.pi * area) / (per * per) if per > 0 else 0.0

            # "largura" aproximada (anti-tira): A/P (m)
            width_index = area / per if per > 0 else 0.0

            minx, miny, maxx, maxy = g.bounds
            w = max(maxx - minx, 0.0)
            h = max(maxy - miny, 0.0)
            bbox_ratio = (min(w, h) / max(w, h)) if max(w, h) > 0 else 0.0

            # decisão
            motivo = None
            if area < min_area:
                motivo = "area_muito_pequena"
            elif compact < min_compact:
                motivo = "muito_irregular_compactness"
            elif width_index < min_width_index:
                motivo = "muito_fino_width_index"
            elif bbox_ratio < min_bbox_ratio:
                motivo = "muito_alongado_bbox_ratio"

            if motivo:
                vazios.append(g)
                motivos.append(motivo)
            else:
                validos.append(g)

        except Exception:
            vazios.append(g)
            motivos.append("erro_classificacao")

    validos_mp = MultiPolygon(validos) if validos else None
    vazios_mp = MultiPolygon(vazios) if vazios else None
    return validos_mp, vazios_mp, motivos


# ------------------------------------------------------------------------------
# Lógica principal (vias/quarteirões/calçadas) em 3 cenários
# ------------------------------------------------------------------------------
def build_road_and_blocks(
    al_geojson: dict, params: dict, srid_calc: int = 3857
) -> Tuple[dict, dict, dict, dict, dict]:
    """
    Retorna (vias_fc, quarteiroes_fc, calcadas_fc, vias_area_fc, areas_vazias_fc)

    - vias_fc: eixos de vias (linhas)
    - vias_area_fc: pavimento (polígonos) SEM calçada
    - calcadas_fc: calçadas derivadas das vias (polígonos) COM via_idx
    - quarteiroes_fc: polígonos válidos
    - areas_vazias_fc: polígonos irregulares/sobras
    """
    tf_in_to_m = Transformer.from_crs(SRID_INPUT, srid_calc, always_xy=True)
    tf_m_to_in = Transformer.from_crs(srid_calc, SRID_INPUT, always_xy=True)

    def _to_in(g):
        return shapely_transform(g, tf_m_to_in)

    # aceita Feature ou Geometry
    geom_mapping = al_geojson
    if isinstance(geom_mapping, dict) and geom_mapping.get("type") == "Feature":
        geom_mapping = geom_mapping.get("geometry") or geom_mapping

    al_m = shapely_transform(_ensure_multipolygon(
        shape(geom_mapping)), tf_in_to_m)

    prof_min = float(params.get("prof_min_m", 30))
    larg_v = float(params.get("larg_rua_vert_m", 8))
    larg_h = float(params.get("larg_rua_horiz_m", 8))
    comp_max = float(params.get("compr_max_quarteirao_m", 120))
    orient_opt = params.get("orientacao_graus")
    calcada_w = float(params.get("calcada_largura_m", 2.5))
    forcar_quart_ext = bool(params.get(
        "forcar_quarteirao_nas_extremidades", True))

    ruas_mask_fc = params.get("ruas_mask_fc")
    ruas_eixo_fc = params.get("ruas_eixo_fc")
    has_ruas_mask = bool(params.get("has_ruas_mask_fc"))
    has_ruas_eixo = bool(params.get("has_ruas_eixo_fc"))

    # união de ruas reais (pavimento)
    roads_union_m = None
    if ruas_mask_fc:
        roads_union_m = _geom_from_fc(ruas_mask_fc, tf_in_to_m)

    roads_axis_buffer = _buffer_centerlines_with_attr(
        ruas_eixo_fc, tf_in_to_m, larg_v)
    if roads_axis_buffer:
        roads_union_m = roads_axis_buffer if roads_union_m is None else unary_union(
            [roads_union_m, roads_axis_buffer])

    # ------------------------------------------------------------
    # CASO 0: nenhuma malha de ruas informada
    # ------------------------------------------------------------
    if not (roads_union_m and not roads_union_m.is_empty) and not (has_ruas_mask or has_ruas_eixo):
        angle = float(
            orient_opt) if orient_opt is not None else estimate_orientation_deg(al_m)
        origin = (al_m.centroid.x, al_m.centroid.y)

        al_al = affinity.rotate(al_m, -angle, origin=origin, use_radians=False)
        axmin, aymin, axmax, aymax = al_al.bounds

        # Ruas "paralelas" (família principal): espaçamento = 2*prof + via + 2*calcada
        spacing_vias = 2 * prof_min + larg_v + 2 * calcada_w
        fam_paral = _gen_parallel_lines_covering_bbox(
            al_m.bounds, spacing_vias, angle, origin)

        # corredores pavimento (pav) e calçadas por via
        paral_pav: list = []
        paral_sidewalks: list = []
        paral_lines_clipped: list = []

        for ln in fam_paral:
            cl = ln.intersection(al_m)
            if cl.is_empty:
                continue
            pav = cl.buffer(max(larg_v, 0) / 2.0, cap_style=2,
                            join_style=2).intersection(al_m)
            if pav.is_empty:
                continue

            paral_lines_clipped.append(cl)
            paral_pav.append(pav)

            sw = _corridor_to_sidewalk(pav, calcada_w, al_m)
            if sw and not sw.is_empty:
                paral_sidewalks.append(sw)
            else:
                paral_sidewalks.append(None)

        # não começa/termina com rua (remove extremos) — aplicado nos pavimentos
        if forcar_quart_ext and paral_pav:
            kept = _remover_corridores_extremos(al_m, paral_pav, angle, origin)
            kept_set = set(id(x) for x in kept)
            new_lines = []
            new_pav = []
            new_sw = []
            for cl, pv, sw in zip(paral_lines_clipped, paral_pav, paral_sidewalks):
                if id(pv) in kept_set:
                    new_lines.append(cl)
                    new_pav.append(pv)
                    new_sw.append(sw)
            paral_lines_clipped, paral_pav, paral_sidewalks = new_lines, new_pav, new_sw

        # Travessas (perpendiculares): espaçadas por comp_max (com sobra centralizada)
        trav_lines_al: List[LineString] = []
        span_x = max(0.0, axmax - axmin)
        n = int(math.floor(span_x / comp_max)) if comp_max > 0 else 0
        leftover = max(span_x - n * comp_max, 0.0)
        margin = leftover / 2.0

        for k in range(1, n + 1):
            xk = axmin + margin + k * comp_max
            if axmin < xk < axmax:
                trav_lines_al.append(LineString(
                    [(xk, aymin - 2 * comp_max), (xk, aymax + 2 * comp_max)]))

        fam_trav_world = [affinity.rotate(
            l, angle, origin=origin, use_radians=False) for l in trav_lines_al]

        trav_pav: list = []
        trav_sidewalks: list = []
        trav_lines_clipped: list = []
        for ln in fam_trav_world:
            cl = ln.intersection(al_m)
            if cl.is_empty:
                continue
            pav = cl.buffer(max(larg_h, 0) / 2.0, cap_style=2,
                            join_style=2).intersection(al_m)
            if pav.is_empty:
                continue

            trav_lines_clipped.append(cl)
            trav_pav.append(pav)

            sw = _corridor_to_sidewalk(pav, calcada_w, al_m)
            if sw and not sw.is_empty:
                trav_sidewalks.append(sw)
            else:
                trav_sidewalks.append(None)

        if forcar_quart_ext and trav_pav:
            kept = _remover_corridores_extremos(
                al_m, trav_pav, angle + 90.0, origin)
            kept_set = set(id(x) for x in kept)
            new_lines = []
            new_pav = []
            new_sw = []
            for cl, pv, sw in zip(trav_lines_clipped, trav_pav, trav_sidewalks):
                if id(pv) in kept_set:
                    new_lines.append(cl)
                    new_pav.append(pv)
                    new_sw.append(sw)
            trav_lines_clipped, trav_pav, trav_sidewalks = new_lines, new_pav, new_sw

        # união de pavimentos e calçadas
        pav_parts = [p for p in (paral_pav + trav_pav) if p and not p.is_empty]
        vias_pav_m = unary_union(pav_parts).intersection(
            al_m) if pav_parts else None

        sw_parts = [s for s in (
            paral_sidewalks + trav_sidewalks) if s and not s.is_empty]
        calcadas_union_m = unary_union(
            sw_parts).intersection(al_m) if sw_parts else None

        # quarteirões = AL - (pav + calcadas)
        sub_parts = []
        if vias_pav_m and not vias_pav_m.is_empty:
            sub_parts.append(vias_pav_m)
        if calcadas_union_m and not calcadas_union_m.is_empty:
            sub_parts.append(calcadas_union_m)

        if sub_parts:
            quarteiroes_raw = _ensure_multipolygon(
                al_m.difference(unary_union(sub_parts)))
        else:
            quarteiroes_raw = _ensure_multipolygon(al_m)

        validos_mp, vazios_mp, motivos = _classificar_quarteiroes_e_vazios(
            quarteiroes_raw, params)

        # montar FC de vias (linhas) e via_idx
        vias_fc = {"type": "FeatureCollection", "features": []}
        calcadas_fc = {"type": "FeatureCollection", "features": []}

        via_idx = 0

        def _emit_via_and_calcada(cl_geom, largura_m, tipo, orient_deg, sidewalk_geom):
            nonlocal via_idx
            # via
            vias_fc["features"].append(
                {
                    "type": "Feature",
                    "properties": {
                        "via_id": f"via_{via_idx+1}",
                        "tipo": tipo,
                        "largura_m": float(largura_m),
                        "categoria": "local",
                        "orientacao_graus": round(float(orient_deg) % 180.0, 2),
                        "origem": "heuristica",
                        "ia_metadata": {},
                    },
                    "geometry": mapping(_to_in(cl_geom)),
                }
            )
            # calcada (vinculada)
            if sidewalk_geom and not sidewalk_geom.is_empty:
                geoms = [sidewalk_geom] if not hasattr(
                    sidewalk_geom, "geoms") else list(sidewalk_geom.geoms)
                for g in geoms:
                    if g.is_empty:
                        continue
                    if not str(getattr(g, "geom_type", "")).endswith("Polygon"):
                        continue
                    calcadas_fc["features"].append(
                        {
                            "type": "Feature",
                            "properties": {
                                "via_idx": via_idx,
                                "largura_m": float(calcada_w),
                                "origem": "heuristica",
                                "ia_metadata": {},
                            },
                            "geometry": mapping(_to_in(g)),
                        }
                    )

            via_idx += 1

        # travessas primeiro (horizontal)
        for cl, sw in zip(trav_lines_clipped, trav_sidewalks):
            _emit_via_and_calcada(cl, larg_h, "horizontal",
                                  (angle + 90.0) % 180.0, sw)

        # paralelas (vertical)
        for cl, sw in zip(paral_lines_clipped, paral_sidewalks):
            _emit_via_and_calcada(cl, larg_v, "vertical", angle % 180.0, sw)

        # FC pavimento
        vias_area_fc = {"type": "FeatureCollection", "features": []}
        if vias_pav_m and not vias_pav_m.is_empty:
            vs = [vias_pav_m] if not hasattr(
                vias_pav_m, "geoms") else list(vias_pav_m.geoms)
            for g in vs:
                if g.is_empty:
                    continue
                if not str(getattr(g, "geom_type", "")).endswith("Polygon"):
                    continue
                vias_area_fc["features"].append(
                    {"type": "Feature", "properties": {}, "geometry": mapping(_to_in(g))})

        # FC quarteiroes validos
        quarteiroes_fc = {"type": "FeatureCollection", "features": []}
        if validos_mp and not validos_mp.is_empty:
            quarteiroes_fc["features"] = [
                {"type": "Feature", "properties": {"origem": "heuristica",
                                                   "ia_metadata": {}}, "geometry": mapping(_to_in(q))}
                for q in validos_mp.geoms
                if not q.is_empty
            ]

        # FC areas vazias
        areas_vazias_fc = {"type": "FeatureCollection", "features": []}
        if vazios_mp and not vazios_mp.is_empty:
            feats = []
            for i, g in enumerate(vazios_mp.geoms):
                if g.is_empty:
                    continue
                motivo = motivos[i] if i < len(motivos) else ""
                feats.append(
                    {
                        "type": "Feature",
                        "properties": {"motivo": motivo, "origem": "heuristica", "ia_metadata": {}},
                        "geometry": mapping(_to_in(g)),
                    }
                )
            areas_vazias_fc["features"] = feats

        return vias_fc, quarteiroes_fc, calcadas_fc, vias_area_fc, areas_vazias_fc

    # ------------------------------------------------------------
    # CASO 1: há ruas reais (roads_union_m)
    # ------------------------------------------------------------
    if roads_union_m and not roads_union_m.is_empty:
        # tira o pavimento existente da AL antes de gerar novas vias
        al_clean = al_m.difference(roads_union_m.buffer(0))
        if al_clean.is_empty:
            empty_fc = {"type": "FeatureCollection", "features": []}
            return empty_fc, empty_fc, empty_fc, empty_fc, empty_fc

        angle_roads = estimate_orientation_deg(roads_union_m)
        origin = (al_m.centroid.x, al_m.centroid.y)

        # travessas: tenta respeitar eixos existentes
        trav_lines_al: List[LineString] = []
        existing_cross_positions: List[float] = []
        if has_ruas_eixo and isinstance(ruas_eixo_fc, dict):
            axis_lines_m = _extract_centerlines_m(ruas_eixo_fc, tf_in_to_m)
            for ln in axis_lines_m:
                ang = _angle_deg_of_line(ln)
                if _angle_diff(ang, (angle_roads + 90.0) % 180.0) <= 20.0:
                    ln_al = affinity.rotate(
                        ln, -angle_roads, origin=origin, use_radians=False)
                    existing_cross_positions.append(ln_al.centroid.x)

        al_al = affinity.rotate(al_clean, -angle_roads,
                                origin=origin, use_radians=False)
        axmin, aymin, axmax, aymax = al_al.bounds

        if existing_cross_positions:
            xs = sorted(
                set(round(x, 3) for x in existing_cross_positions if axmin -
                    1e-6 <= x <= axmax + 1e-6)
            )
            anchors = [axmin] + xs + [axmax]
            for i in range(len(anchors) - 1):
                x0, x1 = anchors[i], anchors[i + 1]
                gap = max(0.0, x1 - x0)
                if gap > comp_max + 1e-6:
                    k = 1
                    while x0 + k * comp_max < x1 - 1e-6:
                        xk = x0 + k * comp_max
                        trav_lines_al.append(LineString(
                            [(xk, aymin - 2 * comp_max), (xk, aymax + 2 * comp_max)]))
                        k += 1
        else:
            # fallback: espaça por comp_max com sobra central
            span_x = max(0.0, axmax - axmin)
            if comp_max > 0 and span_x > comp_max * 1.2:
                n_blocos = int(math.floor(span_x / comp_max))
                n_blocos = max(1, n_blocos)
                leftover = max(span_x - n_blocos * comp_max, 0.0)
                larguras = [comp_max] * n_blocos
                larguras[n_blocos // 2] += leftover
                x_atual = axmin
                for i in range(n_blocos - 1):
                    x_atual += larguras[i]
                    xk = x_atual
                    trav_lines_al.append(LineString(
                        [(xk, aymin - 2 * comp_max), (xk, aymax + 2 * comp_max)]))

        fam_trav_world = [affinity.rotate(
            l, angle_roads, origin=origin, use_radians=False) for l in trav_lines_al]

        # paralelas às ruas existentes
        spacing_vias = 2 * prof_min + larg_v + 2 * calcada_w
        fam_paral = _gen_parallel_lines_covering_bbox(
            al_m.bounds, spacing_vias, angle_roads, origin)

        # pavimentos gerados (paral/trav) + calçadas por via
        trav_pav, trav_sw, trav_cl = [], [], []
        for ln in fam_trav_world:
            cl = ln.intersection(al_m).difference(roads_union_m)
            if cl.is_empty:
                continue
            pav = cl.buffer(max(larg_h, 0) / 2.0, cap_style=2,
                            join_style=2).intersection(al_clean)
            pav = pav.difference(roads_union_m)
            if pav.is_empty:
                continue
            trav_cl.append(cl)
            trav_pav.append(pav)
            trav_sw.append(_corridor_to_sidewalk(pav, calcada_w, al_m))

        if forcar_quart_ext and trav_pav:
            kept = _remover_corridores_extremos(
                al_m, trav_pav, angle_roads + 90.0, origin)
            kept_set = set(id(x) for x in kept)
            new_cl, new_pv, new_sw = [], [], []
            for cl, pv, sw in zip(trav_cl, trav_pav, trav_sw):
                if id(pv) in kept_set:
                    new_cl.append(cl)
                    new_pv.append(pv)
                    new_sw.append(sw)
            trav_cl, trav_pav, trav_sw = new_cl, new_pv, new_sw

        paral_pav, paral_sw, paral_cl = [], [], []
        for ln in fam_paral:
            cl = ln.intersection(al_m).difference(roads_union_m)
            if cl.is_empty:
                continue
            pav = cl.buffer(max(larg_v, 0) / 2.0, cap_style=2,
                            join_style=2).intersection(al_clean)
            pav = pav.difference(roads_union_m)
            if pav.is_empty:
                continue
            paral_cl.append(cl)
            paral_pav.append(pav)
            paral_sw.append(_corridor_to_sidewalk(pav, calcada_w, al_m))

        if forcar_quart_ext and paral_pav:
            kept = _remover_corridores_extremos(
                al_m, paral_pav, angle_roads, origin)
            kept_set = set(id(x) for x in kept)
            new_cl, new_pv, new_sw = [], [], []
            for cl, pv, sw in zip(paral_cl, paral_pav, paral_sw):
                if id(pv) in kept_set:
                    new_cl.append(cl)
                    new_pv.append(pv)
                    new_sw.append(sw)
            paral_cl, paral_pav, paral_sw = new_cl, new_pv, new_sw

        # pavimento total inclui EXISTENTES
        pav_parts = []
        if roads_union_m and not roads_union_m.is_empty:
            pav_parts.append(roads_union_m)
        pav_parts += [p for p in trav_pav + paral_pav if p and not p.is_empty]

        vias_pav_m = unary_union(pav_parts).intersection(
            al_m) if pav_parts else None

        # calçadas das vias geradas (não criamos para o pavimento existente por falta de eixo/idx)
        sw_parts = [s for s in (trav_sw + paral_sw) if s and not s.is_empty]
        calcadas_union_m = unary_union(
            sw_parts).intersection(al_m) if sw_parts else None

        sub_parts = []
        if vias_pav_m and not vias_pav_m.is_empty:
            sub_parts.append(vias_pav_m)
        if calcadas_union_m and not calcadas_union_m.is_empty:
            sub_parts.append(calcadas_union_m)

        quarteiroes_raw = _ensure_multipolygon(al_m.difference(
            unary_union(sub_parts))) if sub_parts else _ensure_multipolygon(al_m)
        validos_mp, vazios_mp, motivos = _classificar_quarteiroes_e_vazios(
            quarteiroes_raw, params)

        # montar FCs
        vias_fc = {"type": "FeatureCollection", "features": []}
        calcadas_fc = {"type": "FeatureCollection", "features": []}
        via_idx = 0

        def _emit(cl_geom, largura_m, tipo, orient_deg, sw_geom):
            nonlocal via_idx
            vias_fc["features"].append(
                {
                    "type": "Feature",
                    "properties": {
                        "via_id": f"via_{via_idx+1}",
                        "tipo": tipo,
                        "largura_m": float(largura_m),
                        "categoria": "local",
                        "orientacao_graus": round(float(orient_deg) % 180.0, 2),
                        "origem": "heuristica",
                        "ia_metadata": {},
                    },
                    "geometry": mapping(_to_in(cl_geom)),
                }
            )
            if sw_geom and not sw_geom.is_empty:
                geoms = [sw_geom] if not hasattr(
                    sw_geom, "geoms") else list(sw_geom.geoms)
                for g in geoms:
                    if g.is_empty:
                        continue
                    if not str(getattr(g, "geom_type", "")).endswith("Polygon"):
                        continue
                    calcadas_fc["features"].append(
                        {
                            "type": "Feature",
                            "properties": {
                                "via_idx": via_idx,
                                "largura_m": float(calcada_w),
                                "origem": "heuristica",
                                "ia_metadata": {},
                            },
                            "geometry": mapping(_to_in(g)),
                        }
                    )
            via_idx += 1

        for cl, sw in zip(trav_cl, trav_sw):
            _emit(cl, larg_h, "horizontal", (angle_roads + 90.0) % 180.0, sw)
        for cl, sw in zip(paral_cl, paral_sw):
            _emit(cl, larg_v, "vertical", angle_roads % 180.0, sw)

        vias_area_fc = {"type": "FeatureCollection", "features": []}
        if vias_pav_m and not vias_pav_m.is_empty:
            vs = [vias_pav_m] if not hasattr(
                vias_pav_m, "geoms") else list(vias_pav_m.geoms)
            for g in vs:
                if g.is_empty:
                    continue
                if not str(getattr(g, "geom_type", "")).endswith("Polygon"):
                    continue
                vias_area_fc["features"].append(
                    {"type": "Feature", "properties": {}, "geometry": mapping(_to_in(g))})

        quarteiroes_fc = {"type": "FeatureCollection", "features": []}
        if validos_mp and not validos_mp.is_empty:
            quarteiroes_fc["features"] = [
                {"type": "Feature", "properties": {"origem": "heuristica",
                                                   "ia_metadata": {}}, "geometry": mapping(_to_in(q))}
                for q in validos_mp.geoms
                if not q.is_empty
            ]

        areas_vazias_fc = {"type": "FeatureCollection", "features": []}
        if vazios_mp and not vazios_mp.is_empty:
            feats = []
            for i, g in enumerate(vazios_mp.geoms):
                if g.is_empty:
                    continue
                motivo = motivos[i] if i < len(motivos) else ""
                feats.append(
                    {"type": "Feature", "properties": {"motivo": motivo, "origem": "heuristica",
                                                       "ia_metadata": {}}, "geometry": mapping(_to_in(g))}
                )
            areas_vazias_fc["features"] = feats

        return vias_fc, quarteiroes_fc, calcadas_fc, vias_area_fc, areas_vazias_fc

    # ------------------------------------------------------------
    # CASO 2: flags indicam ruas, mas geometrias não vieram
    # (mantém comportamento: só gera travessas)
    # ------------------------------------------------------------
    if has_ruas_mask or has_ruas_eixo:
        angle = float(
            orient_opt) if orient_opt is not None else estimate_orientation_deg(al_m)
        origin = (al_m.centroid.x, al_m.centroid.y)
        al_al = affinity.rotate(al_m, -angle, origin=origin, use_radians=False)

        axmin, aymin, axmax, aymax = al_al.bounds
        span_x = max(0.0, axmax - axmin)
        n = int(math.floor(span_x / max(comp_max, 1.0)))
        leftover = max(span_x - n * comp_max, 0.0)
        margin = leftover / 2.0

        trav_lines_al: List[LineString] = []
        for k in range(1, n + 1):
            xk = axmin + margin + k * comp_max
            trav_lines_al.append(LineString(
                [(xk, aymin - 2 * comp_max), (xk, aymax + 2 * comp_max)]))

        fam_trav_world = [affinity.rotate(
            l, angle, origin=origin, use_radians=False) for l in trav_lines_al]

        trav_pav, trav_sw, trav_cl = [], [], []
        for ln in fam_trav_world:
            cl = ln.intersection(al_m)
            if cl.is_empty:
                continue
            pav = cl.buffer(max(larg_h, 0) / 2.0, cap_style=2,
                            join_style=2).intersection(al_m)
            if pav.is_empty:
                continue
            trav_cl.append(cl)
            trav_pav.append(pav)
            trav_sw.append(_corridor_to_sidewalk(pav, calcada_w, al_m))

        if forcar_quart_ext and trav_pav:
            kept = _remover_corridores_extremos(
                al_m, trav_pav, angle + 90.0, origin)
            kept_set = set(id(x) for x in kept)
            new_cl, new_pv, new_sw = [], [], []
            for cl, pv, sw in zip(trav_cl, trav_pav, trav_sw):
                if id(pv) in kept_set:
                    new_cl.append(cl)
                    new_pv.append(pv)
                    new_sw.append(sw)
            trav_cl, trav_pav, trav_sw = new_cl, new_pv, new_sw

        # ✅ Regra: não permitir vias muito próximas da borda (quarteirão < prof_min)
        if params.get("evitar_vias_borda", True) and paral_pav:
            min_gap = _get_min_gap_to_edge_param(params, prof_min)
            paral_pav, paral_cl, paral_sw = _filter_corridors_min_edge_gap(
                al_clean, paral_pav, paral_cl, paral_sw,
                angle_roads, origin,
                axis="y",           # paral: horizontal no frame rotacionado
                min_gap=min_gap
            )

            # ✅ Regra: não permitir vias muito próximas da borda (quarteirão < prof_min)
            if params.get("evitar_vias_borda", True) and trav_pav:
                min_gap = _get_min_gap_to_edge_param(params, prof_min)
                trav_pav, trav_cl, trav_sw = _filter_corridors_min_edge_gap(
                    al_clean, trav_pav, trav_cl, trav_sw,
                    angle_roads, origin,
                    axis="x",           # trav: vertical no frame rotacionado
                    min_gap=min_gap
                )

        vias_pav_m = unary_union(trav_pav).intersection(
            al_m) if trav_pav else None
        calcadas_union_m = unary_union(
            [s for s in trav_sw if s and not s.is_empty]).intersection(al_m) if trav_sw else None

        sub_parts = []
        if vias_pav_m and not vias_pav_m.is_empty:
            sub_parts.append(vias_pav_m)
        if calcadas_union_m and not calcadas_union_m.is_empty:
            sub_parts.append(calcadas_union_m)

        quarteiroes_raw = _ensure_multipolygon(al_m.difference(
            unary_union(sub_parts))) if sub_parts else _ensure_multipolygon(al_m)
        validos_mp, vazios_mp, motivos = _classificar_quarteiroes_e_vazios(
            quarteiroes_raw, params)

        vias_fc = {"type": "FeatureCollection", "features": []}
        calcadas_fc = {"type": "FeatureCollection", "features": []}
        via_idx = 0

        for cl, sw in zip(trav_cl, trav_sw):
            vias_fc["features"].append(
                {
                    "type": "Feature",
                    "properties": {
                        "via_id": f"via_{via_idx+1}",
                        "tipo": "horizontal",
                        "largura_m": float(larg_h),
                        "categoria": "local",
                        "orientacao_graus": round(float((angle + 90.0) % 180.0), 2),
                        "origem": "heuristica",
                        "ia_metadata": {},
                    },
                    "geometry": mapping(_to_in(cl)),
                }
            )
            if sw and not sw.is_empty:
                geoms = [sw] if not hasattr(sw, "geoms") else list(sw.geoms)
                for g in geoms:
                    if g.is_empty:
                        continue
                    if not str(getattr(g, "geom_type", "")).endswith("Polygon"):
                        continue
                    calcadas_fc["features"].append(
                        {
                            "type": "Feature",
                            "properties": {"via_idx": via_idx, "largura_m": float(calcada_w), "origem": "heuristica", "ia_metadata": {}},
                            "geometry": mapping(_to_in(g)),
                        }
                    )

            via_idx += 1

        vias_area_fc = {"type": "FeatureCollection", "features": []}
        if vias_pav_m and not vias_pav_m.is_empty:
            vs = [vias_pav_m] if not hasattr(
                vias_pav_m, "geoms") else list(vias_pav_m.geoms)
            for g in vs:
                if g.is_empty:
                    continue
                if not str(getattr(g, "geom_type", "")).endswith("Polygon"):
                    continue
                vias_area_fc["features"].append(
                    {"type": "Feature", "properties": {}, "geometry": mapping(_to_in(g))})

        quarteiroes_fc = {"type": "FeatureCollection", "features": []}
        if validos_mp and not validos_mp.is_empty:
            quarteiroes_fc["features"] = [
                {"type": "Feature", "properties": {"origem": "heuristica",
                                                   "ia_metadata": {}}, "geometry": mapping(_to_in(q))}
                for q in validos_mp.geoms
                if not q.is_empty
            ]

        areas_vazias_fc = {"type": "FeatureCollection", "features": []}
        if vazios_mp and not vazios_mp.is_empty:
            feats = []
            for i, g in enumerate(vazios_mp.geoms):
                if g.is_empty:
                    continue
                motivo = motivos[i] if i < len(motivos) else ""
                feats.append(
                    {"type": "Feature", "properties": {"motivo": motivo, "origem": "heuristica",
                                                       "ia_metadata": {}}, "geometry": mapping(_to_in(g))}
                )
            areas_vazias_fc["features"] = feats

        return vias_fc, quarteiroes_fc, calcadas_fc, vias_area_fc, areas_vazias_fc

    # ------------------------------------------------------------
    # CASO 3: fallback (sem nada) -> devolve vazio
    # ------------------------------------------------------------
    empty_fc = {"type": "FeatureCollection", "features": []}
    return empty_fc, empty_fc, empty_fc, empty_fc, empty_fc


# ------------------------------------------------------------------------------
# Preview (retorna tudo que o front precisa)
# ------------------------------------------------------------------------------
def compute_preview(al_geojson: dict, params: dict) -> Dict:
    """
    Retorna dicionário com:
      - vias (LINHAS)
      - vias_area (POLÍGONOS pavimento)
      - calcadas (POLÍGONOS derivados das vias, com via_idx)
      - quarteiroes (POLÍGONOS válidos)
      - areas_vazias (POLÍGONOS irregulares/sobras)
      - lotes (vazio)
      - areas_publicas (vazio)
      - metrics
    """
    params = (params or {}).copy()
    srid_calc = int(params.get("srid_calc", 3857))

    # se orientacao_graus não veio e não há ruas, alinha pela AL
    try:
        has_ruas_mask = bool(params.get("has_ruas_mask_fc"))
        has_ruas_eixo = bool(params.get("has_ruas_eixo_fc"))
    except Exception:
        has_ruas_mask = has_ruas_eixo = False

    if params.get("orientacao_graus") is None and not (has_ruas_mask or has_ruas_eixo):
        try:
            tf_in_to_m = Transformer.from_crs(
                SRID_INPUT, srid_calc, always_xy=True)
            geom_mapping = al_geojson
            if isinstance(geom_mapping, dict) and geom_mapping.get("type") == "Feature":
                geom_mapping = geom_mapping.get("geometry") or geom_mapping
            al_m = shapely_transform(shape(geom_mapping), tf_in_to_m)
            if not al_m.is_empty:
                params["orientacao_graus"] = estimate_orientation_deg(al_m)
        except Exception:
            pass

    vias_fc, quarteiroes_fc, calcadas_fc, vias_area_fc, areas_vazias_fc = build_road_and_blocks(
        al_geojson, params, srid_calc
    )

    # Numeração simples das vias
    for idx, feat in enumerate(vias_fc.get("features", []), start=1):
        props = feat.get("properties") or {}
        props.setdefault("via_id", f"via_{idx}")
        props["numero"] = idx
        feat["properties"] = props

    # Numeração dos quarteirões (válidos)
    for idx, feat in enumerate(quarteiroes_fc.get("features", []), start=1):
        props = feat.get("properties") or {}
        props.setdefault("quadra_id", f"quadra_{idx}")
        props["numero"] = idx
        feat["properties"] = props

    metrics = {
        "n_vias": len(vias_fc.get("features", [])),
        "n_quarteiroes": len(quarteiroes_fc.get("features", [])),
        "n_calcadas": len(calcadas_fc.get("features", [])),
        "n_vias_area": len(vias_area_fc.get("features", [])),
        "n_areas_vazias": len(areas_vazias_fc.get("features", [])),
        "has_areas_publicas": False,
    }

    return {
        "vias": vias_fc,
        "vias_area": vias_area_fc,
        "calcadas": calcadas_fc,
        "quarteiroes": quarteiroes_fc,
        "areas_vazias": areas_vazias_fc,
        # por enquanto vazio (você pediu ignorar lotes/agora)
        "lotes": {"type": "FeatureCollection", "features": []},
        "areas_publicas": {"type": "FeatureCollection", "features": []},
        "metrics": metrics,
    }


def compute_preview_com_comandos(al_geom, params, comandos):
    """
    Versão de compute_preview que aplica comandos PRE na área loteável
    antes de chamar o algoritmo normal.

    Agora opera em SRID_INPUT=4674 (compatível com restricoes).
    """
    if not comandos:
        return compute_preview(al_geom, params)

    geom_obj = al_geom
    if isinstance(geom_obj, dict) and geom_obj.get("type") == "Feature":
        geom_obj = geom_obj.get("geometry")

    try:
        al_geos = GEOSGeometry(json.dumps(geom_obj), srid=SRID_INPUT)
    except Exception:
        return compute_preview(al_geom, params)

    al_modificada, areas_publicas_novas = executar_comandos_pre(
        al_geos, comandos)

    al_mod_geojson = json.loads(al_modificada.geojson)

    preview = compute_preview(al_mod_geojson, params)

    # Mantém no contrato (mas você pediu para ignorar por enquanto)
    ap_fc = preview.get("areas_publicas")
    if not isinstance(ap_fc, dict) or ap_fc.get("type") != "FeatureCollection":
        ap_fc = {"type": "FeatureCollection", "features": []}

    features = ap_fc["features"]

    for ap in areas_publicas_novas:
        geom = ap["geometry"]
        props = {
            "tipo": ap.get("tipo", "praca"),
            "nome": ap.get("nome", ""),
            "descricao": ap.get("descricao", "Criada pela IA"),
            "origem": ap.get("origem", "ia"),
        }
        features.append(
            {
                "type": "Feature",
                "properties": props,
                "geometry": json.loads(geom.geojson),
            }
        )

    preview["areas_publicas"] = ap_fc
    preview["metrics"] = preview.get("metrics") or {}
    preview["metrics"]["has_areas_publicas"] = bool(ap_fc.get("features"))

    return preview
