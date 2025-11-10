# services.py
from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

from pyproj import Transformer
from shapely import affinity
from shapely.geometry import (LineString, MultiLineString, MultiPolygon, Point,
                              Polygon, mapping, shape)
from shapely.ops import split
from shapely.ops import transform as shp_transform
from shapely.ops import unary_union

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


def _geom_from_fc_wgs(fc: Optional[dict], to_m: Transformer):
    """
    Converte um FeatureCollection (WGS84) para união (unary_union) em metros (SRID alvo).
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
    ruas_eixo_fc: Optional[dict], tf_wgs_to_m: Transformer, fallback_width: float
):
    """
    Recebe eixos de rua (LineString) com possível propriedade de largura.
    Faz buffer por (largura/2) para virar corredor de via (PAVIMENTO).
    """
    if not isinstance(ruas_eixo_fc, dict) or ruas_eixo_fc.get("type") != "FeatureCollection":
        return None
    polys = []
    for f in ruas_eixo_fc.get("features", []):
        try:
            g = shapely_transform(shape(f.get("geometry")), tf_wgs_to_m)
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


def _extract_centerlines_m(ruas_eixo_fc: Optional[dict], tf_wgs_to_m: Transformer) -> List[LineString]:
    """
    Extrai eixos (LineString) em metros a partir do FC de eixos.
    Ignora features não-lineares silenciosamente.
    """
    lines: List[LineString] = []
    if not isinstance(ruas_eixo_fc, dict) or ruas_eixo_fc.get("type") != "FeatureCollection":
        return lines
    for f in ruas_eixo_fc.get("features", []):
        try:
            g = shapely_transform(shape(f.get("geometry")), tf_wgs_to_m)
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


# ------------------------------------------------------------------------------
# Malha viária + quarteirões + calçadas
# ------------------------------------------------------------------------------

def _vias_area_from_components(al_m, roads_union_m, trav_corr, paral_corr):
    """
    Monta a área de vias a partir dos corredores/travessas/ruas reais.
    Restrita à AL para evitar “vazamento”.
    """
    parts = []
    if roads_union_m and not roads_union_m.is_empty:
        parts.append(roads_union_m)
    for c in trav_corr or []:
        if not c.is_empty:
            parts.append(c)
    for c in paral_corr or []:
        if not c.is_empty:
            parts.append(c)
    if not parts:
        return None
    ua = unary_union(parts)
    if ua.is_empty:
        return None
    return ua.intersection(al_m)


def _calcadas_ao_redor_dos_quarteiroes(
    quarteiroes_mp: MultiPolygon, vias_area_m, largura_calcada_m: float
):
    """
    Calçada = (buffer EXTERNO do quarteirão − o próprio quarteirão) ∩ área de via.
    Resultado: faixa exclusiva, sem ocupar nem o quarteirão nem a via pavimentada.
    """
    if not quarteiroes_mp or quarteiroes_mp.is_empty or not vias_area_m or vias_area_m.is_empty:
        return None
    lw = max(float(largura_calcada_m), 0.0)
    if lw <= 0:
        return None
    rings = []
    for q in quarteiroes_mp.geoms:
        anel = q.buffer(lw, cap_style=2, join_style=2)
        faixa = anel.difference(q)
        if not faixa.is_empty:
            faixa = faixa.intersection(vias_area_m)
        if faixa and not faixa.is_empty:
            rings.append(faixa)
    if not rings:
        return None
    u = unary_union(rings)
    return _ensure_multipoly_from_any(u)


# ------------------------------------------------------------------------------
# Lógica principal (vias/quarteirões/calçadas) em 3 cenários
# ------------------------------------------------------------------------------

def build_road_and_blocks(
    al_wgs_geojson: dict, params: dict, srid_calc: int = 3857
) -> Tuple[dict, dict, dict, dict]:
    """
    Retorna (vias_fc, quarteiroes_fc, calcadas_fc, vias_area_fc)

    - vias_fc: eixos de vias (linhas) — para exibição (cor branca no front)
    - quarteiroes_fc: polígonos (já afastados pela calçada)
    - calcadas_fc: polígonos (faixa exclusiva), NÃO ocupa rua nem quarteirão
    - vias_area_fc: áreas de PAVIMENTO da via (cinza) — **sem** calçada

    REGRAS:
    - Distância entre quarteirões = largura_rua + 2 * calcada_largura_m
    - vias_area_fc mostra apenas o pavimento (ex.: 5 m), sem a calçada
    - calcadas_fc representa a faixa entre o pavimento e o quarteirão (ex.: 2 m de cada lado)
    - **Com ruas existentes**: o comprimento dos quarteirões é medido **entre travessas existentes**;
      só se cria nova travessa se o vão exceder `compr_max_quarteirao_m`.
    """
    tf_wgs_to_m = Transformer.from_crs(4326, srid_calc, always_xy=True)
    tf_m_to_wgs = Transformer.from_crs(srid_calc, 4326, always_xy=True)

    def _to_wgs(g):
        return shapely_transform(g, tf_m_to_wgs)

    al_m = shapely_transform(_ensure_multipolygon(
        shape(al_wgs_geojson)), tf_wgs_to_m)

    frente_min = float(params["frente_min_m"])
    prof_min = float(params["prof_min_m"])
    larg_v = float(params["larg_rua_vert_m"])
    larg_h = float(params["larg_rua_horiz_m"])
    comp_max = float(params["compr_max_quarteirao_m"])
    orient_opt = params.get("orientacao_graus")
    calcada_w = float(params.get("calcada_largura_m", 2.5))

    ruas_mask_fc = params.get("ruas_mask_fc")
    ruas_eixo_fc = params.get("ruas_eixo_fc")
    has_ruas_mask = bool(params.get("has_ruas_mask_fc"))
    has_ruas_eixo = bool(params.get("has_ruas_eixo_fc"))

    # união de ruas reais (pavimento)
    roads_union_m = None
    if ruas_mask_fc:
        roads_union_m = _geom_from_fc_wgs(ruas_mask_fc, tf_wgs_to_m)
    roads_axis_buffer = _buffer_centerlines_with_attr(
        ruas_eixo_fc, tf_wgs_to_m, larg_v)
    if roads_axis_buffer:
        roads_union_m = roads_axis_buffer if roads_union_m is None else unary_union(
            [roads_union_m, roads_axis_buffer])

    # =========================
    # CASO 1: há ruas reais
    # =========================
    if roads_union_m and not roads_union_m.is_empty:
        # Remove o pavimento da AL (sem calçadas) antes de gerar novas vias
        al_clean = al_m.difference(roads_union_m.buffer(0))
        if al_clean.is_empty:
            empty_fc = {"type": "FeatureCollection", "features": []}
            return empty_fc, empty_fc, empty_fc, empty_fc

        # Orientação dominante das ruas principais
        angle_roads = estimate_orientation_deg(roads_union_m)
        origin = (al_m.centroid.x, al_m.centroid.y)

        # ---------- Travessas (perpendiculares) respeitando vãos existentes ----------
        trav_lines_al: List[LineString] = []

        # Se tiver eixos, usamos eles para identificar travessas já existentes
        existing_cross_positions: List[float] = []
        if has_ruas_eixo and isinstance(ruas_eixo_fc, dict):
            axis_lines_m = _extract_centerlines_m(ruas_eixo_fc, tf_wgs_to_m)
            # Classifica por ângulo em relação à orientação dominante
            for ln in axis_lines_m:
                ang = _angle_deg_of_line(ln)
                # Perpendicular às "principais"
                if _angle_diff(ang, (angle_roads + 90.0) % 180.0) <= 20.0:
                    # Posição X no sistema alinhado às "principais"
                    ln_al = affinity.rotate(
                        ln, -angle_roads, origin=origin, use_radians=False)
                    existing_cross_positions.append(ln_al.centroid.x)

        # Rotaciona AL e RUAS para medir ao longo do eixo X (comprimento)
        al_al = affinity.rotate(al_clean, -angle_roads,
                                origin=origin, use_radians=False)
        axmin, aymin, axmax, aymax = al_al.bounds

        # Limpa/ordena as posições conhecidas de travessas
        if existing_cross_positions:
            # inclui limites da AL para fatiar os vãos nas bordas
            xs = sorted(set(round(
                x, 3) for x in existing_cross_positions if axmin - 1e-6 <= x <= axmax + 1e-6))
            anchors = [axmin] + xs + [axmax]
            # Para cada vão entre travessas existentes, inserir novas a cada comp_max
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
                # se gap ≤ comp_max, não cria travessa (respeita existentes)
        else:
            # Sem eixos perpendiculares informados → fallback: grade regular,
            # mas ainda **referenciada** aos limites da AL (não ao bounding de ruas),
            # o que evita travessas super densas dentro de vãos curtos.
            span_x = max(0.0, axmax - axmin)
            if comp_max > 0:
                n = int(math.floor(span_x / comp_max))
            else:
                n = 0
            leftover = max(span_x - n * comp_max, 0.0)
            margin = leftover / 2.0
            for k in range(1, n + 1):
                xk = axmin + margin + k * comp_max
                if axmin < xk < axmax:
                    trav_lines_al.append(LineString(
                        [(xk, aymin - 2 * comp_max), (xk, aymax + 2 * comp_max)]))

        # Desfaz rotação para o mundo
        def _unrot(g):
            return affinity.rotate(g, angle_roads, origin=origin, use_radians=False)

        fam_trav_world = [_unrot(l) for l in trav_lines_al]

        # Corredores das travessas: dentro da AL limpa e sem sobrepor ruas existentes
        trav_corr = buffer_lines_as_corridors(fam_trav_world, larg_h)
        trav_corr = [c.intersection(al_clean)
                     for c in trav_corr if not c.is_empty]
        trav_corr = [c.difference(roads_union_m)
                     for c in trav_corr if not c.is_empty]
        trav_corr = [c for c in trav_corr if not c.is_empty]

        # ---------- Ruas paralelas às existentes (regra 2 profundidades) ----------
        spacing_vias = 2 * prof_min + larg_v + 2 * calcada_w
        fam_paral = _gen_parallel_lines_covering_bbox(
            al_m.bounds, spacing_vias, angle_roads, origin)
        paral_corr = buffer_lines_as_corridors(fam_paral, larg_v)
        paral_corr = [c.intersection(al_clean)
                      for c in paral_corr if not c.is_empty]
        paral_corr = [c.difference(roads_union_m)
                      for c in paral_corr if not c.is_empty]
        paral_corr = [c for c in paral_corr if not c.is_empty]

        # ---------- Pavimento, total (pav + calçada) e calçadas exclusivas ----------
        pav_parts = []
        if roads_union_m and not roads_union_m.is_empty:
            pav_parts.append(roads_union_m)
        pav_parts += [c for c in trav_corr if c and not c.is_empty]
        pav_parts += [c for c in paral_corr if c and not c.is_empty]

        def _vias_pav_e_total(parts_corridors, al_m, calcada_w: float):
            if not parts_corridors:
                return None, None, None
            base = [p for p in parts_corridors if p and not p.is_empty]
            if not base:
                return None, None, None
            pav = unary_union(base).intersection(al_m)
            if pav.is_empty:
                return None, None, None
            expandidas = [
                p.buffer(max(calcada_w, 0.0), cap_style=2, join_style=2) for p in base]
            total = unary_union(expandidas).intersection(al_m)
            calc = None
            if total and not total.is_empty:
                diff = total.difference(pav)
                if diff and not diff.is_empty:
                    if isinstance(diff, (Polygon, MultiPolygon)) or getattr(diff, "geom_type", "").endswith("Polygon"):
                        calc = diff
            return (pav if (pav and not pav.is_empty) else None,
                    total if (total and not total.is_empty) else None,
                    calc if (calc and not calc.is_empty) else None)

        vias_pav_m, vias_total_m, calcadas_union = _vias_pav_e_total(
            pav_parts, al_m, calcada_w)

        # Quarteirões = AL - (rua + calçada)
        if vias_total_m and not vias_total_m.is_empty:
            quarteiroes = _ensure_multipolygon(al_m.difference(vias_total_m))
        else:
            quarteiroes = _ensure_multipolygon(al_m)

        # Eixos de vias (linhas) para exibição
        vias_lines = []
        for l in fam_trav_world:
            cl = l.intersection(al_m).difference(roads_union_m)
            if not cl.is_empty:
                vias_lines.append(
                    {"tipo": "horizontal", "largura_m": larg_h, "geom": cl})
        for l in fam_paral:
            cl = l.intersection(al_m).difference(roads_union_m)
            if not cl.is_empty:
                vias_lines.append(
                    {"tipo": "vertical", "largura_m": larg_v, "geom": cl})

        # FCs
        vias_fc = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {"tipo": v["tipo"], "largura_m": v["largura_m"]},
                    "geometry": mapping(_to_wgs(v["geom"])),
                }
                for v in vias_lines
            ],
        }

        quarteiroes_fc = {
            "type": "FeatureCollection",
            "features": [{"type": "Feature", "properties": {}, "geometry": mapping(_to_wgs(q))} for q in quarteiroes.geoms],
        }

        calcadas_fc = {"type": "FeatureCollection", "features": []}
        if calcadas_union and not calcadas_union.is_empty:
            geoms = [calcadas_union] if not hasattr(
                calcadas_union, "geoms") else list(calcadas_union.geoms)
            calcadas_fc["features"] = [
                {"type": "Feature", "properties": {"largura_m": calcada_w},
                    "geometry": mapping(_to_wgs(g))}
                for g in geoms
            ]

        vias_area_fc = {"type": "FeatureCollection", "features": []}
        if vias_pav_m and not vias_pav_m.is_empty:
            vs = [vias_pav_m] if not hasattr(
                vias_pav_m, "geoms") else list(vias_pav_m.geoms)
            vias_area_fc["features"] = [{"type": "Feature", "properties": {
            }, "geometry": mapping(_to_wgs(g))} for g in vs]

        return vias_fc, quarteiroes_fc, calcadas_fc, vias_area_fc

    # =========================
    # CASO 2: flags indicam ruas, mas geometrias não vieram
    # =========================
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

        fam_horiz = [affinity.rotate(
            l, angle, origin=origin, use_radians=False) for l in trav_lines_al]
        trav_corr = buffer_lines_as_corridors(fam_horiz, larg_h)
        trav_corr = [c.intersection(al_m) for c in trav_corr if not c.is_empty]

        pav_parts = [c for c in trav_corr if c and not c.is_empty]

        def _vias_pav_e_total(parts_corridors, al_m, calcada_w: float):
            if not parts_corridors:
                return None, None, None
            base = [p for p in parts_corridors if p and not p.is_empty]
            if not base:
                return None, None, None
            pav = unary_union(base).intersection(al_m)
            if pav.is_empty:
                return None, None, None
            expandidas = [
                p.buffer(max(calcada_w, 0.0), cap_style=2, join_style=2) for p in base]
            total = unary_union(expandidas).intersection(al_m)
            calc = None
            if total and not total.is_empty:
                diff = total.difference(pav)
                if diff and not diff.is_empty:
                    if isinstance(diff, (Polygon, MultiPolygon)) or getattr(diff, "geom_type", "").endswith("Polygon"):
                        calc = diff
            return (pav if (pav and not pav.is_empty) else None,
                    total if (total and not total.is_empty) else None,
                    calc if (calc and not calc.is_empty) else None)

        vias_pav_m, vias_total_m, calcadas_union = _vias_pav_e_total(
            pav_parts, al_m, calcada_w)

        if vias_total_m and not vias_total_m.is_empty:
            quarteiroes = _ensure_multipolygon(al_m.difference(vias_total_m))
        else:
            quarteiroes = _ensure_multipolygon(al_m)

        vias_lines = []
        for l in fam_horiz:
            cl = l.intersection(al_m)
            if not cl.is_empty:
                vias_lines.append(
                    {"tipo": "horizontal", "largura_m": larg_h, "geom": cl})

        vias_fc = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {"tipo": v["tipo"], "largura_m": v["largura_m"]},
                    "geometry": mapping(_to_wgs(v["geom"])),
                }
                for v in vias_lines
            ],
        }
        quarteiroes_fc = {
            "type": "FeatureCollection",
            "features": [{"type": "Feature", "properties": {}, "geometry": mapping(_to_wgs(q))} for q in quarteiroes.geoms],
        }
        calcadas_fc = {"type": "FeatureCollection", "features": []}
        if calcadas_union and not calcadas_union.is_empty:
            geoms = [calcadas_union] if not hasattr(
                calcadas_union, "geoms") else list(calcadas_union.geoms)
            calcadas_fc["features"] = [
                {"type": "Feature", "properties": {"largura_m": calcada_w},
                    "geometry": mapping(_to_wgs(g))}
                for g in geoms
            ]
        vias_area_fc = {"type": "FeatureCollection", "features": []}
        if vias_pav_m and not vias_pav_m.is_empty:
            vs = [vias_pav_m] if not hasattr(
                vias_pav_m, "geoms") else list(vias_pav_m.geoms)
            vias_area_fc["features"] = [{"type": "Feature", "properties": {
            }, "geometry": mapping(_to_wgs(g))} for g in vs]

        return vias_fc, quarteiroes_fc, calcadas_fc, vias_area_fc

    # =========================
    # CASO 3: sem ruas → grelha gerada
    # =========================
    angle = float(
        orient_opt) if orient_opt is not None else estimate_orientation_deg(al_m)
    spacing_vias = 2 * prof_min + larg_v + 2 * calcada_w
    cx = (al_m.bounds[0] + al_m.bounds[2]) / 2.0
    cy = (al_m.bounds[1] + al_m.bounds[3]) / 2.0

    fam_vert = _gen_parallel_lines_covering_bbox(
        al_m.bounds, spacing_vias, angle, (cx, cy))
    vias_vert_corr = buffer_lines_as_corridors(fam_vert, larg_v)
    vias_vert_corr = [poly.intersection(al_m) for poly in vias_vert_corr]
    vias_vert_corr = [p for p in vias_vert_corr if not p.is_empty]

    fam_horiz = _gen_parallel_lines_covering_bbox(
        al_m.bounds, comp_max, angle + 90.0, (cx, cy))
    trav_corr = buffer_lines_as_corridors(fam_horiz, larg_h)
    trav_corr = [poly.intersection(al_m) for poly in trav_corr]
    trav_corr = [p for p in trav_corr if not p.is_empty]

    def _vias_pav_e_total(parts_corridors, al_m, calcada_w: float):
        if not parts_corridors:
            return None, None, None
        base = [p for p in parts_corridors if p and not p.is_empty]
        if not base:
            return None, None, None
        pav = unary_union(base).intersection(al_m)
        if pav.is_empty:
            return None, None, None
        expandidas = [
            p.buffer(max(calcada_w, 0.0), cap_style=2, join_style=2) for p in base]
        total = unary_union(expandidas).intersection(al_m)
        calc = None
        if total and not total.is_empty:
            diff = total.difference(pav)
            if diff and not diff.is_empty:
                if isinstance(diff, (Polygon, MultiPolygon)) or getattr(diff, "geom_type", "").endswith("Polygon"):
                    calc = diff
        return (pav if (pav and not pav.is_empty) else None,
                total if (total and not total.is_empty) else None,
                calc if (calc and not calc.is_empty) else None)

    pav_parts = []
    pav_parts += [p for p in vias_vert_corr if p and not p.is_empty]
    pav_parts += [p for p in trav_corr if p and not p.is_empty]

    vias_pav_m, vias_total_m, calcadas_union = _vias_pav_e_total(
        pav_parts, al_m, calcada_w)

    if vias_total_m and not vias_total_m.is_empty:
        quarteiroes = _ensure_multipolygon(al_m.difference(vias_total_m))
    else:
        quarteiroes = _ensure_multipolygon(al_m)

    vias_lines = []
    for l in _gen_parallel_lines_covering_bbox(al_m.bounds, spacing_vias, angle, (cx, cy)) + \
            _gen_parallel_lines_covering_bbox(al_m.bounds, comp_max, angle + 90.0, (cx, cy)):
        cl = l.intersection(al_m)
        if not cl.is_empty:
            tipo = "vertical" if _angle_diff(
                estimate_orientation_deg(cl), angle) < 10.0 else "horizontal"
            vias_lines.append({"tipo": tipo, "largura_m": (
                larg_v if tipo == "vertical" else larg_h), "geom": cl})

    vias_fc = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"tipo": v["tipo"], "largura_m": v["largura_m"]},
                "geometry": mapping(_to_wgs(v["geom"])),
            }
            for v in vias_lines
        ],
    }
    quarteiroes_fc = {
        "type": "FeatureCollection",
        "features": [{"type": "Feature", "properties": {}, "geometry": mapping(_to_wgs(q))} for q in quarteiroes.geoms],
    }
    calcadas_fc = {"type": "FeatureCollection", "features": []}
    if calcadas_union and not calcadas_union.is_empty:
        geoms = [calcadas_union] if not hasattr(
            calcadas_union, "geoms") else list(calcadas_union.geoms)
        calcadas_fc["features"] = [
            {"type": "Feature", "properties": {"largura_m": calcada_w},
                "geometry": mapping(_to_wgs(g))}
            for g in geoms
        ]
    vias_area_fc = {"type": "FeatureCollection", "features": []}
    if vias_pav_m and not vias_pav_m.is_empty:
        vs = [vias_pav_m] if not hasattr(
            vias_pav_m, "geoms") else list(vias_pav_m.geoms)
        vias_area_fc["features"] = [{"type": "Feature", "properties": {
        }, "geometry": mapping(_to_wgs(g))} for g in vs]

    return vias_fc, quarteiroes_fc, calcadas_fc, vias_area_fc


# ------------------------------------------------------------------------------
# Lotes (fatias)
# ------------------------------------------------------------------------------

def _rotate_align(g, angle_deg: float, origin):
    return affinity.rotate(g, -angle_deg, origin=origin, use_radians=False)


def _rotate_back(g, angle_deg: float, origin):
    return affinity.rotate(g, angle_deg, origin=origin, use_radians=False)


def _split_once(target_geom, cutters):
    if not cutters:
        return [target_geom]
    lw = unary_union(cutters)
    if lw.is_empty:
        return [target_geom]
    try:
        parts = split(target_geom, lw)
        return list(parts.geoms) if hasattr(parts, "geoms") else [parts]
    except Exception:
        return [target_geom]


def _pick_corner_point_aligned(poly_al: Polygon):
    coords = list(poly_al.exterior.coords)
    return min(coords, key=lambda c: (c[1], c[0]))


def slice_lots(quarteiroes_fc: dict, params: dict, srid_calc: int = 3857) -> dict:
    frente_min = float(params["frente_min_m"])
    prof_min = float(params["prof_min_m"])
    orient = params.get("orientacao_graus")
    tol = float(params.get("tolerancia_frac", 0.05))

    frente_req = frente_min * (1.0 - tol)
    prof_req = prof_min * (1.0 - tol)
    area_min = frente_min * prof_min
    area_req = area_min * (1.0 - tol)
    area_double_limit = 2.0 * area_min * (1.0 + tol)

    tf_wgs_to_m = Transformer.from_crs(4326, srid_calc, always_xy=True)
    tf_m_to_wgs = Transformer.from_crs(srid_calc, 4326, always_xy=True)

    features = []
    lot_counter = 1

    def _emit_piece(p_al, angle, origin):
        nonlocal lot_counter
        pxmin, pymin, pxmax, pymax = p_al.bounds
        frente_aprox = max(pxmax - pxmin, 0.0)
        prof_aprox = max(pymax - pymin, 0.0)

        p_m = affinity.rotate(p_al, angle, origin=origin,
                              use_radians=False).buffer(0)
        if p_m.is_empty:
            return False
        area_m2 = float(abs(p_m.area))

        center_m_al: Point = p_al.representative_point()
        cx_al, cy_al = min(list(p_al.exterior.coords),
                           key=lambda c: (c[1], c[0]))
        corner_al_pt = Point(cx_al, cy_al)

        center_m_unrot = affinity.rotate(
            center_m_al, angle, origin=origin, use_radians=False)
        corner_m_unrot = affinity.rotate(
            corner_al_pt, angle, origin=origin, use_radians=False)

        geom_wgs = shapely_transform(p_m, tf_m_to_wgs)
        center_wgs = shapely_transform(center_m_unrot, tf_m_to_wgs)
        corner_wgs = shapely_transform(corner_m_unrot, tf_m_to_wgs)

        features.append(
            {
                "type": "Feature",
                "properties": {
                    "lot_number": lot_counter,
                    "frente_aprox_m": round(float(frente_aprox), 2),
                    "prof_aprox_m": round(float(prof_aprox), 2),
                    "area_m2": round(area_m2, 2),
                    "label_center": [center_wgs.x, center_wgs.y],
                    "label_corner": [corner_wgs.x, corner_wgs.y],
                    "score_qualidade": 1.0,
                },
                "geometry": mapping(geom_wgs),
            }
        )
        lot_counter += 1
        return True

    def _piece_is_valid(p_al):
        pxmin, pymin, pxmax, pymax = p_al.bounds
        frente_aprox = max(pxmax - pxmin, 0.0)
        prof_aprox = max(pymax - pymin, 0.0)
        if frente_aprox + tol * frente_min < frente_min:
            return False
        if prof_aprox + tol * prof_min < prof_min:
            return False
        area_m2 = float(abs(p_al.area))
        if area_m2 + tol * area_min < area_min:
            return False
        return True

    for feat in quarteiroes_fc.get("features", []):
        q_wgs = shape(feat.get("geometry"))
        q_m = shapely_transform(q_wgs, tf_wgs_to_m)
        if q_m.is_empty:
            continue

        angle = float(
            orient) if orient is not None else estimate_orientation_deg(q_m)
        origin = (q_m.centroid.x, q_m.centroid.y)
        q_al = affinity.rotate(q_m, -angle, origin=origin,
                               use_radians=False).buffer(0)
        if q_al.is_empty:
            continue

        minx, miny, maxx, maxy = q_al.bounds
        span_x = maxx - minx
        span_y = maxy - miny
        if span_x <= 0 or span_y <= 0:
            continue

        # se muito pequeno, emite como está
        if (span_x + tol * frente_min) < frente_min or (span_y + tol * prof_min) < prof_min:
            _emit_piece(q_al, angle, origin)
            continue

        # 2 faixas na profundidade quando possível
        if span_y >= 2.0 * prof_req:
            ymid = (miny + maxy) / 2.0
            center_line = LineString(
                [(minx - span_x, ymid), (maxx + span_x, ymid)])
            faixas_al = _split_once(q_al, [center_line])
            faixas_al = [f.buffer(0) for f in faixas_al if not f.is_empty]
            if len(faixas_al) >= 2:
                faixas_al.sort(key=lambda g: g.bounds[1])
                faixas_al = faixas_al[:2]
            else:
                faixas_al = [q_al]
        else:
            faixas_al = [q_al]

        for faixa_al in faixas_al:
            if faixa_al.is_empty:
                continue
            fminx, fminy, fmaxx, fmaxy = faixa_al.bounds
            f_len = fmaxx - fminx
            if f_len <= 0:
                continue

            if f_len < (2.0 * frente_req):
                _emit_piece(faixa_al, angle, origin)
                continue

            n = int(math.floor((f_len + tol * frente_min) / frente_min))
            n = max(2, n)
            base_total = n * frente_min
            leftover = max(f_len - base_total, 0.0)
            offset = leftover / 2.0

            cuts = []
            for k in range(1, n):
                xk = fminx + offset + k * frente_min
                cuts.append(LineString(
                    [(xk, fminy - 10 * prof_min), (xk, fmaxy + 10 * prof_min)]))

            pedacos_al = _split_once(faixa_al, cuts)
            pedacos_al = [p.buffer(0) for p in pedacos_al if not p.is_empty]
            pedacos_al.sort(key=lambda g: g.bounds[0])

            stack = pedacos_al[:]
            while stack:
                p_al = stack.pop(0)
                if p_al.is_empty:
                    continue

                pxmin, pymin, pxmax, pymax = p_al.bounds
                frente_aprox = max(pxmax - pxmin, 0.0)
                prof_aprox = max(pymax - pymin, 0.0)
                if frente_aprox <= 0.0 or prof_aprox <= 0.0:
                    continue

                area_m2_aligned = float(abs(p_al.area))

                if (
                    frente_aprox + tol * frente_min < frente_min
                    or prof_aprox + tol * prof_min < prof_min
                    or area_m2_aligned + tol * area_min < area_min
                ):
                    continue

                # lote muito grande e frente comporta divisão ao meio
                if area_m2_aligned > area_double_limit and frente_aprox >= 2.0 * frente_req:
                    xmid = (pxmin + pxmax) / 2.0
                    cut = LineString(
                        [(xmid, pymin - 10 * prof_min), (xmid, pymax + 10 * prof_min)])
                    subparts = _split_once(p_al, [cut])
                    subparts = [pp.buffer(0)
                                for pp in subparts if not pp.is_empty]
                    if len(subparts) == 2:
                        sp1, sp2 = subparts
                        if _piece_is_valid(sp1) and _piece_is_valid(sp2):
                            subparts.sort(key=lambda g: g.bounds[0])
                            stack = subparts + stack
                            continue

                _emit_piece(p_al, angle, origin)

    return {"type": "FeatureCollection", "features": features}


# ------------------------------------------------------------------------------
# Preview (retorna tudo que o front precisa)
# ------------------------------------------------------------------------------

def compute_preview(al_geojson: dict, params: dict) -> Dict:
    """
    Retorna dicionário com:
      - vias (LINHAS)
      - vias_area (POLÍGONOS cinza SEM calçada)
      - quarteiroes (POLÍGONOS)
      - lotes (POLÍGONOS, com labels e métricas)
      - calcadas (POLÍGONOS, faixa exclusiva)
      - metrics
    """
    vias_fc, quarteiroes_fc, calcadas_fc, vias_area_fc = build_road_and_blocks(
        al_geojson, params, params.get("srid_calc", 3857)
    )
    lotes_fc = slice_lots(quarteiroes_fc, params,
                          params.get("srid_calc", 3857))

    metrics = {
        "n_vias": len(vias_fc["features"]),
        "n_quarteiroes": len(quarteiroes_fc["features"]),
        "n_lotes": len(lotes_fc["features"]),
        "n_calcadas": len(calcadas_fc["features"]),
        "n_vias_area": len(vias_area_fc["features"]),
    }

    return {
        "vias": vias_fc,            # LINHAS (eixos) → Branco no front
        "vias_area": vias_area_fc,  # POLÍGONOS cinza já sem a faixa de calçada
        "quarteiroes": quarteiroes_fc,
        "lotes": lotes_fc,
        "calcadas": calcadas_fc,    # POLÍGONOS (faixa branca), exclusivos
        "metrics": metrics,
    }
