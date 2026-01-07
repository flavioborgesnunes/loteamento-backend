from __future__ import annotations

import json
import math
from typing import Dict, List, Optional, Tuple

from django.contrib.gis.geos import GEOSGeometry
from parcelamento.commands.executor import executar_comandos_pre
from pyproj import Transformer
from shapely import affinity
from shapely.geometry import (LineString, MultiLineString, MultiPolygon, Point,
                              Polygon, mapping, shape)
from shapely.ops import split
from shapely.ops import transform as shp_transform
from shapely.ops import unary_union

from .commands.executor import executar_comandos_pre

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
    al_m: BaseGeometry,
    corridors: list[BaseGeometry],
    angle_deg: float,
    origin: tuple[float, float],
) -> list[BaseGeometry]:
    """
    Remove no máximo 1 corredor de rua em cada extremidade ao longo da
    direção perpendicular a 'angle_deg', para que as bordas da AL
    fiquem encostadas em quarteirões (e não em ruas).

    - al_m: Polygon/MultiPolygon da área loteável, no SRID de cálculo
    - corridors: lista de polígonos de corredor (vias já buffereadas e
      cortadas pela AL)
    - angle_deg: ângulo em graus que foi usado para gerar essa família
      de vias (_gen_parallel_lines_covering_bbox)
    - origin: (cx, cy) usado como origem da rotação
    """
    if not corridors:
        return corridors

    # Rotaciona a AL para um sistema alinhado ao eixo da via
    try:
        al_al = affinity.rotate(
            al_m, -angle_deg, origin=origin, use_radians=False)
    except Exception:
        return corridors

    if al_al.is_empty:
        return corridors

    infos: list[tuple[BaseGeometry, float]] = []
    for c in corridors:
        if c is None or c.is_empty:
            continue
        cen = c.centroid
        cen_al = affinity.rotate(
            cen, -angle_deg, origin=origin, use_radians=False)
        infos.append((c, cen_al.y))

    if len(infos) <= 2:
        # Com 0, 1 ou 2 corredores, não faz sentido remover extremos:
        # já teremos quarteirões grandes nas bordas.
        return [c for c, _ in infos]

    ys = [y for _, y in infos]
    min_y = min(ys)
    max_y = max(ys)
    span = max_y - min_y
    eps = max(span * 0.01, 1e-6)

    # Ordena por posição perpendicular (y) e remove o mais extremo em cada lado
    kept: list[BaseGeometry] = []
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

    # Garante que nunca volte lista vazia
    if not kept:
        return [c for c, _ in infos]

    return kept


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


def _extract_centerlines_m(
    ruas_eixo_fc: Optional[dict], tf_wgs_to_m: Transformer
) -> List[LineString]:
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
                lines.extend(
                    [seg for seg in g.geoms if isinstance(
                        seg, LineString) and not seg.is_empty]
                )
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
    """
    tf_wgs_to_m = Transformer.from_crs(4326, srid_calc, always_xy=True)
    tf_m_to_wgs = Transformer.from_crs(srid_calc, 4326, always_xy=True)

    def _to_wgs(g):
        return shapely_transform(g, tf_m_to_wgs)

    al_m = shapely_transform(_ensure_multipolygon(
        shape(al_wgs_geojson)), tf_wgs_to_m)

    frente_min = float(params["frente_min_m"])
    prof_min = float(params["prof_min_m"])

    # Correntes de segurança para não dar UnboundLocalError em pav_parts
    trav_corr: list = []
    paral_corr: list = []

    larg_v = float(params["larg_rua_vert_m"])
    larg_h = float(params["larg_rua_horiz_m"])
    comp_max = float(params["compr_max_quarteirao_m"])
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
        roads_union_m = _geom_from_fc_wgs(ruas_mask_fc, tf_wgs_to_m)
    roads_axis_buffer = _buffer_centerlines_with_attr(
        ruas_eixo_fc, tf_wgs_to_m, larg_v)
    if roads_axis_buffer:
        roads_union_m = roads_axis_buffer if roads_union_m is None else unary_union(
            [roads_union_m, roads_axis_buffer]
        )

        # =========================
    # CASO 0: nenhuma malha de ruas informada
    # → grade regular referenciada à AL, sem começar/terminar com rua
    # =========================
    if not (roads_union_m and not roads_union_m.is_empty) and not (has_ruas_mask or has_ruas_eixo):
        # ângulo base: usa orientacao_graus se vier, senão estima pela AL
        angle = float(
            orient_opt) if orient_opt is not None else estimate_orientation_deg(al_m)
        origin = (al_m.centroid.x, al_m.centroid.y)

        # AL rotacionada para trabalhar alinhado ao eixo principal
        al_al = affinity.rotate(
            al_m, -angle, origin=origin, use_radians=False
        )
        axmin, aymin, axmax, aymax = al_al.bounds

        # 1) Ruas "verticais" (paralelas ao eixo principal) pela regra 2 profundidades
        spacing_vias = 2 * prof_min + larg_v + 2 * calcada_w
        fam_paral = _gen_parallel_lines_covering_bbox(
            al_m.bounds, spacing_vias, angle, origin
        )
        paral_corr_raw = buffer_lines_as_corridors(fam_paral, larg_v)
        paral_corr: list = []
        for poly in paral_corr_raw:
            inter = poly.intersection(al_m)
            if not inter.is_empty:
                # garante só polígonos válidos
                if hasattr(inter, "geoms"):
                    for g in inter.geoms:
                        if getattr(g, "geom_type", "") == "Polygon" and not g.is_empty:
                            paral_corr.append(g)
                elif getattr(inter, "geom_type", "") == "Polygon":
                    paral_corr.append(inter)

        # 🔹 remove o corredor mais extremo de cada lado (não começa/termina com rua)
        if forcar_quart_ext and paral_corr:
            paral_corr = _remover_corridores_extremos(
                al_m=al_m,
                corridors=paral_corr,
                angle_deg=angle,
                origin=origin,
            )

        # 2) Travessas "horizontais" (perpendiculares ao eixo principal), espaçadas por comp_max
        trav_lines_al: List[LineString] = []
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
                trav_lines_al.append(
                    LineString(
                        [(xk, aymin - 2 * comp_max),
                         (xk, aymax + 2 * comp_max)]
                    )
                )

        # volta para o sistema "mundo"
        fam_trav_world = [
            affinity.rotate(l, angle, origin=origin, use_radians=False)
            for l in trav_lines_al
        ]

        trav_corr_raw = buffer_lines_as_corridors(fam_trav_world, larg_h)
        trav_corr: list = []
        for poly in trav_corr_raw:
            inter = poly.intersection(al_m)
            if not inter.is_empty:
                if hasattr(inter, "geoms"):
                    for g in inter.geoms:
                        if getattr(g, "geom_type", "") == "Polygon" and not g.is_empty:
                            trav_corr.append(g)
                elif getattr(inter, "geom_type", "") == "Polygon":
                    trav_corr.append(inter)

        # 🔹 idem para travessas: tira as extremas para não começar/terminar com rua
        if forcar_quart_ext and trav_corr:
            trav_corr = _remover_corridores_extremos(
                al_m=al_m,
                corridors=trav_corr,
                angle_deg=angle + 90.0,
                origin=origin,
            )

        # 3) Monta pavimento de via, via+calçada e calçadas exclusivas
        pav_parts = []
        pav_parts += [c for c in paral_corr if c and not c.is_empty]
        pav_parts += [c for c in trav_corr if c and not c.is_empty]

        def _vias_pav_e_total(parts_corridors):
            if not parts_corridors:
                return None, None, None
            base = [p for p in parts_corridors if p and not p.is_empty]
            if not base:
                return None, None, None
            pav = unary_union(base).intersection(al_m)
            if pav.is_empty:
                return None, None, None
            expandidas = [
                p.buffer(max(calcada_w, 0.0), cap_style=2, join_style=2) for p in base
            ]
            total = unary_union(expandidas).intersection(al_m)
            calc = None
            if total and not total.is_empty:
                diff = total.difference(pav)
                if diff and not diff.is_empty and getattr(diff, "geom_type", "").endswith("Polygon"):
                    calc = diff
            return (
                pav if (pav and not pav.is_empty) else None,
                total if (total and not total.is_empty) else None,
                calc if (calc and not calc.is_empty) else None,
            )

        vias_pav_m, vias_total_m, calcadas_union = _vias_pav_e_total(pav_parts)

        # 4) Quarteirões = AL − (via + calçada)
        if vias_total_m and not vias_total_m.is_empty:
            quarteiroes = _ensure_multipolygon(al_m.difference(vias_total_m))
        else:
            quarteiroes = _ensure_multipolygon(al_m)

        def _to_wgs(g):
            return shapely_transform(g, tf_m_to_wgs)

        # 5) Eixos de vias (linhas) para exibição
        vias_lines = []
        # travessas (horizontais)
        for l in fam_trav_world:
            cl = l.intersection(al_m)
            if not cl.is_empty:
                vias_lines.append(
                    {
                        "tipo": "horizontal",
                        "largura_m": larg_h,
                        "geom": cl,
                        "orientacao_graus": (angle + 90.0) % 180.0,
                    }
                )
        # paralelas (verticais)
        for l in fam_paral:
            cl = l.intersection(al_m)
            if not cl.is_empty:
                vias_lines.append(
                    {
                        "tipo": "vertical",
                        "largura_m": larg_v,
                        "geom": cl,
                        "orientacao_graus": angle % 180.0,
                    }
                )

        vias_fc = {
            "type": "FeatureCollection",
            "features": [],
        }
        for idx, v in enumerate(vias_lines, start=1):
            geom_wgs = _to_wgs(v["geom"])
            props = {
                "via_id": f"via_{idx}",
                "tipo": v["tipo"],
                "largura_m": v["largura_m"],
                "categoria": "local",
                "orientacao_graus": round(float(v["orientacao_graus"]) % 180.0, 2),
                "origem": "heuristica",
                "ia_metadata": {},
            }
            vias_fc["features"].append(
                {
                    "type": "Feature",
                    "properties": props,
                    "geometry": mapping(geom_wgs),
                }
            )

        quarteiroes_fc = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {
                        "origem": "heuristica",
                        "ia_metadata": {},
                    },
                    "geometry": mapping(_to_wgs(q)),
                }
                for q in quarteiroes.geoms
            ],
        }

        calcadas_fc = {"type": "FeatureCollection", "features": []}
        if calcadas_union and not calcadas_union.is_empty:
            geoms = (
                [calcadas_union]
                if not hasattr(calcadas_union, "geoms")
                else list(calcadas_union.geoms)
            )
            calcadas_fc["features"] = [
                {
                    "type": "Feature",
                    "properties": {
                        "largura_m": calcada_w,
                        "origem": "heuristica",
                        "ia_metadata": {},
                    },
                    "geometry": mapping(_to_wgs(g)),
                }
                for g in geoms
            ]

        vias_area_fc = {"type": "FeatureCollection", "features": []}
        if vias_pav_m and not vias_pav_m.is_empty:
            vs = (
                [vias_pav_m]
                if not hasattr(vias_pav_m, "geoms")
                else list(vias_pav_m.geoms)
            )
            vias_area_fc["features"] = [
                {
                    "type": "Feature",
                    "properties": {},
                    "geometry": mapping(_to_wgs(g)),
                }
                for g in vs
            ]

        # 👈 IMPORTANTE: já retorna aqui, não deixa cair no CASO 1/2/3 antigos
        return vias_fc, quarteiroes_fc, calcadas_fc, vias_area_fc

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
                        ln, -angle_roads, origin=origin, use_radians=False
                    )
                    existing_cross_positions.append(ln_al.centroid.x)

        # Rotaciona AL e RUAS para medir ao longo do eixo X (comprimento)
        al_al = affinity.rotate(al_clean, -angle_roads,
                                origin=origin, use_radians=False)
        axmin, aymin, axmax, aymax = al_al.bounds

        # Limpa/ordena as posições conhecidas de travessas
        if existing_cross_positions:
            # inclui limites da AL para fatiar os vãos nas bordas
            xs = sorted(
                set(
                    round(x, 3)
                    for x in existing_cross_positions
                    if axmin - 1e-6 <= x <= axmax + 1e-6
                )
            )
            anchors = [axmin] + xs + [axmax]
            # Para cada vão entre travessas existentes, inserir novas a cada comp_max
            for i in range(len(anchors) - 1):
                x0, x1 = anchors[i], anchors[i + 1]
                gap = max(0.0, x1 - x0)
                if gap > comp_max + 1e-6:
                    k = 1
                    while x0 + k * comp_max < x1 - 1e-6:
                        xk = x0 + k * comp_max
                        trav_lines_al.append(
                            LineString(
                                [(xk, aymin - 2 * comp_max),
                                 (xk, aymax + 2 * comp_max)]
                            )
                        )
                        k += 1
                # se gap ≤ comp_max, não cria travessa (respeita existentes)
                else:
                    # Sem eixos perpendiculares informados → fallback: grade regular,
                    # ainda referenciada aos limites da AL, MAS:
                    #  - não começa/termina com rua
                    #  - ajusta o "miolo" para absorver a sobra (quarteirão mais largo)
                    span_x = max(0.0, axmax - axmin)
                    trav_lines_al: List[LineString] = []

                    if comp_max > 0 and span_x > comp_max * 1.2:
                        # número de QUARTEIRÕES ao longo de X
                        n_blocos = int(math.floor(span_x / comp_max))
                        if n_blocos < 1:
                            n_blocos = 1

                        # sobra total de comprimento depois de encaixar n_blocos * comp_max
                        leftover = max(span_x - n_blocos * comp_max, 0.0)

                        # cada bloco começa com comp_max, e a "sobra" vai TODA para o bloco do meio
                        larguras = [comp_max] * n_blocos
                        bloco_sobra = n_blocos // 2  # 👉 no futuro vira parâmetro do front/IA
                        larguras[bloco_sobra] += leftover

                        # andando de axmin até axmax, sempre:
                        # [BLOCO_0] | rua | [BLOCO_1] | rua | ... | [BLOCO_(n_blocos-1)]
                        x_atual = axmin
                        for i in range(n_blocos - 1):
                            x_atual += larguras[i]
                            xk = x_atual
                            # cria travessa no limite entre bloco i e i+1
                            trav_lines_al.append(
                                LineString(
                                    [(xk, aymin - 2 * comp_max),
                                     (xk, aymax + 2 * comp_max)]
                                )
                            )
                        # Em teoria: x_atual + larguras[-1] == axmax (dentro de pequenas tolerâncias)
                    else:
                        # área pequena ou comp_max inválido → nenhuma travessa
                        trav_lines_al = []

        # Desfaz rotação para o mundo

        def _unrot(g):
            return affinity.rotate(g, angle_roads, origin=origin, use_radians=False)

        fam_trav_world = [_unrot(l) for l in trav_lines_al]

        # ---------- Ruas paralelas às existentes (regra 2 profundidades) ----------
        spacing_vias = 2 * prof_min + larg_v + 2 * calcada_w

        # família de linhas paralelas ao eixo principal das ruas
        fam_paral = _gen_parallel_lines_covering_bbox(
            al_m.bounds, spacing_vias, angle_roads, origin
        )

        # corredores de pavimento dessas vias paralelas, cortados pela AL limpa
        paral_corr = buffer_lines_as_corridors(fam_paral, larg_v)
        paral_corr = [
            c.intersection(al_clean)
            for c in paral_corr
            if c is not None and not c.is_empty
        ]
        paral_corr = [
            c.difference(roads_union_m)
            for c in paral_corr
            if c is not None and not c.is_empty
        ]
        paral_corr = [
            c for c in paral_corr if c is not None and not c.is_empty]

        # 🔹 NOVO:
        # remove, no máximo, 1 corredor paralelo em cada extremidade
        # ao longo da direção PERPENDICULAR às vias principais.
        #
        # Efeito:
        #   - a AL deixa de começar/terminar com rua nessa direção de profundidade
        #   - as bordas passam a encostar em QUARTEIRÕES (e portanto em LOTES)
        #   - a profundidade dos quarteirões de borda fica maior (absorve a "sobra")
        if forcar_quart_ext and paral_corr:
            paral_corr = _remover_corridores_extremos(
                al_m=al_m,
                corridors=paral_corr,
                angle_deg=angle_roads,
                origin=origin,
            )

        # 🔹 NOVO:
        # remove no máximo 1 corredor de rua em cada extremidade
        # ao longo da direção PERPENDICULAR às vias principais.
        #
        # Resultado:
        #   - a AL deixa de começar/terminar com rua nesse eixo
        #   - as bordas sempre ficam encostadas em QUARTEIRÕES
        #   - profundidade dos lotes de borda aumenta (absorvendo a "sobra")
        if forcar_quart_ext and paral_corr:
            paral_corr = _remover_corridores_extremos(
                al_m=al_m,
                corridors=paral_corr,
                angle_deg=angle_roads,  # direção das vias paralelas
                origin=origin,
            )

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
                p.buffer(max(calcada_w, 0.0), cap_style=2, join_style=2) for p in base
            ]
            total = unary_union(expandidas).intersection(al_m)
            calc = None
            if total and not total.is_empty:
                diff = total.difference(pav)
                if diff and not diff.is_empty:
                    if isinstance(diff, (Polygon, MultiPolygon)) or getattr(
                        diff, "geom_type", ""
                    ).endswith("Polygon"):
                        calc = diff
            return (
                pav if (pav and not pav.is_empty) else None,
                total if (total and not total.is_empty) else None,
                calc if (calc and not calc.is_empty) else None,
            )

        vias_pav_m, vias_total_m, calcadas_union = _vias_pav_e_total(
            pav_parts, al_m, calcada_w
        )

        # Quarteirões = AL - (rua + calçada)
        if vias_total_m and not vias_total_m.is_empty:
            quarteiroes = _ensure_multipolygon(al_m.difference(vias_total_m))
        else:
            quarteiroes = _ensure_multipolygon(al_m)

        # Eixos de vias (linhas) para exibição
        vias_lines = []
        # Travessas (aprox. horizontais, perpendiculares às principais)
        for l in fam_trav_world:
            cl = l.intersection(al_m).difference(roads_union_m)
            if not cl.is_empty:
                vias_lines.append(
                    {
                        "tipo": "horizontal",
                        "largura_m": larg_h,
                        "geom": cl,
                        "orientacao_graus": (angle_roads + 90.0) % 180.0,
                    }
                )
        # Paralelas às principais (aprox. verticais)
        for l in fam_paral:
            cl = l.intersection(al_m).difference(roads_union_m)
            if not cl.is_empty:
                vias_lines.append(
                    {
                        "tipo": "vertical",
                        "largura_m": larg_v,
                        "geom": cl,
                        "orientacao_graus": angle_roads % 180.0,
                    }
                )

        # FCs
        vias_fc = {
            "type": "FeatureCollection",
            "features": [],
        }
        for idx, v in enumerate(vias_lines, start=1):
            geom_wgs = _to_wgs(v["geom"])
            props = {
                "via_id": f"via_{idx}",
                "tipo": v["tipo"],
                "largura_m": v["largura_m"],
                # categoria padrão - depois pode ser ajustada por IA/usuário
                "categoria": "local",
                "orientacao_graus": round(float(v["orientacao_graus"]) % 180.0, 2),
                # origem de geração para IA/backend
                "origem": "heuristica",
                "ia_metadata": {},
            }
            vias_fc["features"].append(
                {
                    "type": "Feature",
                    "properties": props,
                    "geometry": mapping(geom_wgs),
                }
            )

        quarteiroes_fc = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {
                        "origem": "heuristica",
                        "ia_metadata": {},
                    },
                    "geometry": mapping(_to_wgs(q)),
                }
                for q in quarteiroes.geoms
            ],
        }

        calcadas_fc = {"type": "FeatureCollection", "features": []}
        if calcadas_union and not calcadas_union.is_empty:
            geoms = (
                [calcadas_union]
                if not hasattr(calcadas_union, "geoms")
                else list(calcadas_union.geoms)
            )
            calcadas_fc["features"] = [
                {
                    "type": "Feature",
                    "properties": {
                        "largura_m": calcada_w,
                        "origem": "heuristica",
                        "ia_metadata": {},
                    },
                    "geometry": mapping(_to_wgs(g)),
                }
                for g in geoms
            ]

        vias_area_fc = {"type": "FeatureCollection", "features": []}
        if vias_pav_m and not vias_pav_m.is_empty:
            vs = [vias_pav_m] if not hasattr(vias_pav_m, "geoms") else list(
                vias_pav_m.geoms
            )
            vias_area_fc["features"] = [
                {
                    "type": "Feature",
                    "properties": {},
                    "geometry": mapping(_to_wgs(g)),
                }
                for g in vs
            ]

        return vias_fc, quarteiroes_fc, calcadas_fc, vias_area_fc

    # =========================
    # CASO 2: flags indicam ruas, mas geometrias não vieram
    # =========================
    if has_ruas_mask or has_ruas_eixo:
        angle = float(orient_opt) if orient_opt is not None else estimate_orientation_deg(
            al_m
        )
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
            trav_lines_al.append(
                LineString([(xk, aymin - 2 * comp_max),
                           (xk, aymax + 2 * comp_max)])
            )

        fam_horiz = [
            affinity.rotate(l, angle, origin=origin, use_radians=False)
            for l in trav_lines_al
        ]
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
                p.buffer(max(calcada_w, 0.0), cap_style=2, join_style=2) for p in base
            ]
            total = unary_union(expandidas).intersection(al_m)
            calc = None
            if total and not total.is_empty:
                diff = total.difference(pav)
                if diff and not diff.is_empty:
                    if isinstance(diff, (Polygon, MultiPolygon)) or getattr(
                        diff, "geom_type", ""
                    ).endswith("Polygon"):
                        calc = diff
            return (
                pav if (pav and not pav.is_empty) else None,
                total if (total and not total.is_empty) else None,
                calc if (calc and not calc.is_empty) else None,
            )

        vias_pav_m, vias_total_m, calcadas_union = _vias_pav_e_total(
            pav_parts, al_m, calcada_w
        )

        if vias_total_m and not vias_total_m.is_empty:
            quarteiroes = _ensure_multipolygon(al_m.difference(vias_total_m))
        else:
            quarteiroes = _ensure_multipolygon(al_m)

        vias_lines = []
        for l in fam_horiz:
            cl = l.intersection(al_m)
            if not cl.is_empty:
                vias_lines.append(
                    {
                        "tipo": "horizontal",
                        "largura_m": larg_h,
                        "geom": cl,
                        "orientacao_graus": (angle + 90.0) % 180.0,
                    }
                )

        vias_fc = {
            "type": "FeatureCollection",
            "features": [],
        }
        for idx, v in enumerate(vias_lines, start=1):
            geom_wgs = _to_wgs(v["geom"])
            props = {
                "via_id": f"via_{idx}",
                "tipo": v["tipo"],
                "largura_m": v["largura_m"],
                "categoria": "local",
                "orientacao_graus": round(float(v["orientacao_graus"]) % 180.0, 2),
                "origem": "heuristica",
                "ia_metadata": {},
            }
            vias_fc["features"].append(
                {
                    "type": "Feature",
                    "properties": props,
                    "geometry": mapping(geom_wgs),
                }
            )

        quarteiroes_fc = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {
                        "origem": "heuristica",
                        "ia_metadata": {},
                    },
                    "geometry": mapping(_to_wgs(q)),
                }
                for q in quarteiroes.geoms
            ],
        }
        calcadas_fc = {"type": "FeatureCollection", "features": []}
        if calcadas_union and not calcadas_union.is_empty:
            geoms = (
                [calcadas_union]
                if not hasattr(calcadas_union, "geoms")
                else list(calcadas_union.geoms)
            )
            calcadas_fc["features"] = [
                {
                    "type": "Feature",
                    "properties": {
                        "largura_m": calcada_w,
                        "origem": "heuristica",
                        "ia_metadata": {},
                    },
                    "geometry": mapping(_to_wgs(g)),
                }
                for g in geoms
            ]
        vias_area_fc = {"type": "FeatureCollection", "features": []}
        if vias_pav_m and not vias_pav_m.is_empty:
            vs = [vias_pav_m] if not hasattr(vias_pav_m, "geoms") else list(
                vias_pav_m.geoms
            )
            vias_area_fc["features"] = [
                {
                    "type": "Feature",
                    "properties": {},
                    "geometry": mapping(_to_wgs(g)),
                }
                for g in vs
            ]

        return vias_fc, quarteiroes_fc, calcadas_fc, vias_area_fc

       # ----------------------------------------------------------------------
    # CASO 3 – Sem malha de ruas existente → grade regular referenciada à AL
    # ----------------------------------------------------------------------
    else:
        # Já temos: al_m, frente_min, prof_min, larg_rua_vert, larg_rua_horiz, comp_max
        # e o "angle" definido pela direção dos quarteirões.

        # 1) Calcula um bounding box da AL no sistema rotacionado
        cx, cy = al_m.centroid.x, al_m.centroid.y
        angle = angle_roads  # já calculado mais acima
        al_rot = affinity.rotate(
            al_m, -angle, origin=(cx, cy), use_radians=False)
        axmin, aymin, axmax, aymax = al_rot.bounds

        # 2) Família de vias "verticais" (paralelas ao eixo principal)
        fam_vert = _gen_parallel_lines_covering_bbox(
            al_m.bounds,
            spacing=2 * prof_min + larg_rua_vert,
            angle_deg=angle,
            origin=(cx, cy),
        )
        vias_vert_corr_raw = buffer_lines_as_corridors(
            fam_vert,
            largura=larg_rua_vert,
        )
        # Corta pela AL
        vias_vert_corr = []
        for poly in vias_vert_corr_raw:
            inter = poly.intersection(al_m)
            if not inter.is_empty:
                if isinstance(inter, (MultiPolygon, GeometryCollection)):
                    for g in inter.geoms:
                        if isinstance(g, Polygon) and not g.is_empty:
                            vias_vert_corr.append(g)
                elif isinstance(inter, Polygon):
                    vias_vert_corr.append(inter)

        # 🔹 NOVO: remove o corredor mais extremo de cada lado (não começa/termina com rua)
        if forcar_quart_ext:
            vias_vert_corr = _remover_corridores_extremos(
                al_m=al_m,
                corridors=vias_vert_corr,
                angle_deg=angle,
                origin=(cx, cy),
            )

        # 3) Família de travessas "horizontais" (perpendiculares ao eixo principal)
        fam_trav = []
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
                fam_trav.append(
                    affinity.rotate(
                        LineString(
                            [(xk, aymin - 2 * comp_max),
                             (xk, aymax + 2 * comp_max)]
                        ),
                        angle,
                        origin=(cx, cy),
                        use_radians=False,
                    )
                )

        trav_corr_raw = buffer_lines_as_corridors(
            fam_trav,
            largura=larg_rua_horiz,
        )
        trav_corr = []
        for poly in trav_corr_raw:
            inter = poly.intersection(al_m)
            if not inter.is_empty:
                if isinstance(inter, (MultiPolygon, GeometryCollection)):
                    for g in inter.geoms:
                        if isinstance(g, Polygon) and not g.is_empty:
                            trav_corr.append(g)
                elif isinstance(inter, Polygon):
                    trav_corr.append(inter)

        # 🔹 NOVO: idem para as travessas
        if forcar_quart_ext:
            trav_corr = _remover_corridores_extremos(
                al_m=al_m,
                corridors=trav_corr,
                angle_deg=angle + 90.0,
                origin=(cx, cy),
            )

        # 4) Monta a lista final de vias de pavimento e total
        def _vias_pav_e_total(
            vias_corr_list: list[BaseGeometry],
        ) -> tuple[list[BaseGeometry], list[BaseGeometry]]:
            vias_pav = []
            vias_total = []
            for corr in vias_corr_list:
                if corr.is_empty:
                    continue
                pav = corr.buffer(-calcada_largura, join_style=2)
                if not pav.is_empty:
                    if isinstance(pav, (MultiPolygon, GeometryCollection)):
                        for g in pav.geoms:
                            if isinstance(g, Polygon) and not g.is_empty:
                                vias_pav.append(g)
                    elif isinstance(pav, Polygon):
                        vias_pav.append(pav)
                vias_total.append(corr)
            return vias_pav, vias_total

        vias_vert_pav, vias_vert_total = _vias_pav_e_total(vias_vert_corr)
        trav_pav, trav_total = _vias_pav_e_total(trav_corr)

        vias_pav_parts = vias_vert_pav + trav_pav
        vias_total_parts = vias_vert_total + trav_total


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
            center_m_al, angle, origin=origin, use_radians=False
        )
        corner_m_unrot = affinity.rotate(
            corner_al_pt, angle, origin=origin, use_radians=False
        )

        geom_wgs = shapely_transform(p_m, tf_m_to_wgs)
        center_wgs = shapely_transform(center_m_unrot, tf_m_to_wgs)
        corner_wgs = shapely_transform(corner_m_unrot, tf_m_to_wgs)

        features.append(
            {
                "type": "Feature",
                "properties": {
                    # número do lote dentro da versão (casa com Lote.numero)
                    "numero": lot_counter,
                    "frente_m": round(float(frente_aprox), 2),
                    "prof_media_m": round(float(prof_aprox), 2),
                    "area_m2": round(area_m2, 2),
                    "lot_number": lot_counter,
                    # orientação do lote (0..180)
                    "orientacao_graus": round(float(angle) % 180.0, 2),
                    # snapshot de regras
                    "frente_min_m": frente_min,
                    "prof_min_m": prof_min,
                    # dados auxiliares para label no front
                    "label_center": [center_wgs.x, center_wgs.y],
                    "label_corner": [corner_wgs.x, corner_wgs.y],
                    "score_qualidade": 1.0,
                    # flags para IA/backend
                    "origem": "heuristica",
                    "ia_metadata": {},
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
                cuts.append(
                    LineString(
                        [(xk, fminy - 10 * prof_min), (xk, fmaxy + 10 * prof_min)]
                    )
                )

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
                if (
                    area_m2_aligned > area_double_limit
                    and frente_aprox >= 2.0 * frente_req
                ):
                    xmid = (pxmin + pxmax) / 2.0
                    cut = LineString(
                        [(xmid, pymin - 10 * prof_min),
                         (xmid, pymax + 10 * prof_min)]
                    )
                    subparts = _split_once(p_al, [cut])
                    subparts = [
                        pp.buffer(0) for pp in subparts if not pp.is_empty
                    ]
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
      - vias (LINHAS, com via_id, tipo, largura_m, categoria, orientacao_graus, numero)
      - vias_area (POLÍGONOS cinza SEM calçada)
      - quarteiroes (POLÍGONOS, com quadra_id/numero)
      - lotes (POLÍGONOS, com numero, métricas e vínculo opcional à quadra)
      - calcadas (POLÍGONOS, faixa exclusiva)
      - areas_publicas (POLÍGONOS, por enquanto vazio no heurístico)
      - metrics (contagens + métricas gerais para IA)
    """
    # Trabalha em uma cópia local para não surpreender quem chamou
    params = (params or {}).copy()
    srid_calc = params.get("srid_calc", 3857)

    # mínimos para validar lote FINAL (sem pedacinhos)
    try:
        frente_min = float(params.get("frente_min_m") or 0.0)
        prof_min = float(params.get("prof_min_m") or 0.0)
    except Exception:
        frente_min = prof_min = 0.0
    area_min = frente_min * \
        prof_min if (frente_min > 0 and prof_min > 0) else 0.0

    tf_wgs_to_m = Transformer.from_crs(4326, srid_calc, always_xy=True)

    def _lote_minimo_ok(feat: dict) -> bool:
        """
        Garante que o lote final:
          - tenha frente >= frente_min
          - profundidade >= prof_min
          - área >= area_min

        Tudo medido no SRID de cálculo.
        """
        if not (frente_min > 0 and prof_min > 0 and area_min > 0):
            # se não tiver parâmetros, não filtramos
            return True

        geom = feat.get("geometry")
        if not geom:
            return False
        try:
            g_wgs = shape(geom)
            g_m = shapely_transform(g_wgs, tf_wgs_to_m)
        except Exception:
            return False

        if g_m.is_empty:
            return False

        xmin, ymin, xmax, ymax = g_m.bounds
        frente = max(xmax - xmin, 0.0)
        prof = max(ymax - ymin, 0.0)
        area = float(abs(g_m.area))

        if frente + 1e-9 < frente_min:
            return False
        if prof + 1e-9 < prof_min:
            return False
        if area + 1e-9 < area_min:
            return False
        return True

    # ------------------------------------------------------------------
    # 1) Se não há ruas pré-existentes e nenhuma orientação veio,
    #    tentamos alinhar tudo ao maior lado da AL (lado da AOI).
    #    Isso ajuda a manter lotes paralelos à AOI "sempre que possível".
    # ------------------------------------------------------------------
    try:
        has_ruas_mask = bool(params.get("has_ruas_mask_fc"))
        has_ruas_eixo = bool(params.get("has_ruas_eixo_fc"))
    except Exception:
        has_ruas_mask = has_ruas_eixo = False

    if params.get("orientacao_graus") is None and not (has_ruas_mask or has_ruas_eixo):
        try:
            geom_mapping = al_geojson
            if isinstance(geom_mapping, dict) and geom_mapping.get("type") == "Feature":
                geom_mapping = geom_mapping.get("geometry") or geom_mapping

            al_m = shapely_transform(shape(geom_mapping), tf_wgs_to_m)
            if not al_m.is_empty:
                params["orientacao_graus"] = estimate_orientation_deg(al_m)
        except Exception:
            # Se der algo errado aqui, seguimos com o fluxo normal
            pass

    # ------------------------------------------------------------------
    # 2) Gera vias, quarteirões, calçadas e áreas de via
    # ------------------------------------------------------------------
    vias_fc, quarteiroes_fc, calcadas_fc, vias_area_fc = build_road_and_blocks(
        al_geojson, params, srid_calc
    )

    # Numeração simples das VIAS (ruas)
    for idx, feat in enumerate(vias_fc.get("features", []), start=1):
        props = feat.get("properties") or {}
        # garante via_id e adiciona numero
        props.setdefault("via_id", f"via_{idx}")
        props["numero"] = idx
        feat["properties"] = props

    # Numeração das QUADRAS (quarteirões)
    for idx, feat in enumerate(quarteiroes_fc.get("features", []), start=1):
        props = feat.get("properties") or {}
        props.setdefault("quadra_id", f"quadra_{idx}")
        props["numero"] = idx
        feat["properties"] = props

    # ------------------------------------------------------------------
    # 3) Gera lotes dentro dos quarteirões (aqui já com orientacao_graus,
    #    se tivermos definido acima). O slice_lots já preenche numero/metrics.
    # ------------------------------------------------------------------
    lotes_fc = slice_lots(quarteiroes_fc, params, srid_calc)

    # ------------------------------------------------------------------
    # 4) Garante que NENHUM LOTE SOBREPOS RUA/CALÇADA:
    #    recortamos os lotes pela união de vias_area + calcadas.
    # ------------------------------------------------------------------
    recorte_geoms = []

    # Áreas pavimentadas das vias
    for feat in vias_area_fc.get("features", []):
        try:
            recorte_geoms.append(shape(feat.get("geometry")))
        except Exception:
            continue

    # Faixas de calçada também não devem virar lote
    for feat in calcadas_fc.get("features", []):
        try:
            recorte_geoms.append(shape(feat.get("geometry")))
        except Exception:
            continue

    recorte_union = None
    if recorte_geoms:
        try:
            recorte_union = unary_union(recorte_geoms)
        except Exception:
            recorte_union = None

    if recorte_union and not recorte_union.is_empty:
        novos_lotes = []
        for feat in lotes_fc.get("features", []):
            geom = feat.get("geometry")
            if not geom:
                continue
            try:
                g = shape(geom)
            except Exception:
                continue
            if g.is_empty:
                continue

            g_recortado = g.difference(recorte_union)
            if g_recortado.is_empty:
                # Lote totalmente dentro de rua/calcada → descartado
                continue

            # Se sobrar MultiPolygon, fica só com o maior pedaço
            if isinstance(g_recortado, MultiPolygon):
                geoms = [gg for gg in g_recortado.geoms if not gg.is_empty]
                if not geoms:
                    continue
                g_recortado = max(geoms, key=lambda gg: gg.area)

            # Se não for polígono, melhor descartar para evitar glitches
            if not isinstance(g_recortado, Polygon):
                continue

            feat["geometry"] = mapping(g_recortado)

            # 🔹 novo filtro: garante que não virou “pedacinho”
            if not _lote_minimo_ok(feat):
                continue

            novos_lotes.append(feat)

        lotes_fc["features"] = novos_lotes

    # ------------------------------------------------------------------
    # 5) Re-numera lotes de forma sequencial e opcionalmente vincula
    #    cada lote a uma quadra (usando o ponto de label ou centróide).
    # ------------------------------------------------------------------
    # Prepara quarteirões em shapely para mapear quadra_id
    quadras = []
    for q_idx, feat_q in enumerate(quarteiroes_fc.get("features", []), start=1):
        geom_q = feat_q.get("geometry")
        if not geom_q:
            continue
        try:
            gq = shape(geom_q)
        except Exception:
            continue
        if gq.is_empty:
            continue
        quadras.append((q_idx, gq))

    # Re-numera e vincula
    lot_counter = 0
    for feat in lotes_fc.get("features", []):
        geom = feat.get("geometry")
        if not geom:
            continue
        try:
            g = shape(geom)
        except Exception:
            continue
        if g.is_empty:
            continue

        lot_counter += 1
        props = feat.get("properties") or {}
        props["numero"] = lot_counter
        props["lot_number"] = lot_counter

        # tenta usar label_center para localizar quadra; se não tiver, usa centróide
        centro = props.get("label_center")
        if (
            isinstance(centro, (list, tuple))
            and len(centro) == 2
            and isinstance(centro[0], (int, float))
            and isinstance(centro[1], (int, float))
        ):
            pt = Point(centro[0], centro[1])
        else:
            pt = g.centroid

        quadra_id = None
        for q_idx, gq in quadras:
            try:
                if gq.contains(pt):
                    quadra_id = q_idx
                    break
            except Exception:
                continue

        if quadra_id is not None:
            props["quadra_id"] = quadra_id

        feat["properties"] = props

    # ------------------------------------------------------------------
    # 6) Métricas para IA / frontend
    # ------------------------------------------------------------------
    n_lotes = len(lotes_fc.get("features", []))
    area_total_lotes = 0.0
    if n_lotes:
        for f in lotes_fc["features"]:
            props = f.get("properties") or {}
            try:
                area_total_lotes += float(props.get("area_m2") or 0.0)
            except Exception:
                pass

    metrics = {
        "n_vias": len(vias_fc.get("features", [])),
        "n_quarteiroes": len(quarteiroes_fc.get("features", [])),
        "n_lotes": n_lotes,
        "n_calcadas": len(calcadas_fc.get("features", [])),
        "n_vias_area": len(vias_area_fc.get("features", [])),
        "area_total_lotes_m2": round(area_total_lotes, 2),
        "area_media_lote_m2": round(area_total_lotes / n_lotes, 2) if n_lotes else 0.0,
        "has_areas_publicas": False,
    }

    return {
        # LINHAS (eixos)
        "vias": vias_fc,
        # POLÍGONOS cinza (pavimento)
        "vias_area": vias_area_fc,
        # QUADRAS
        "quarteiroes": quarteiroes_fc,
        # LOTES
        "lotes": lotes_fc,
        # CALÇADAS
        "calcadas": calcadas_fc,
        # por enquanto vazio no heurístico
        "areas_publicas": {
            "type": "FeatureCollection",
            "features": [],
        },
        "metrics": metrics,
    }


def compute_preview_com_comandos(al_geom, params, comandos):
    """
    Versão de compute_preview que aplica comandos PRE (ex.: criar_praca)
    na área loteável antes de chamar o algoritmo normal.

    al_geom: GeoJSON Geometry ou Feature (como já vem hoje do front)
    params: dict de parâmetros numéricos JÁ no formato esperado pelo backend
    comandos: lista de comandos vindos da IA (campo "comandos")
    """
    forcar_quart_ext = bool(params.get(
        "forcar_quarteirao_nas_extremidades", False))

    # Se não há comandos, cai direto no fluxo antigo
    if not comandos:
        return compute_preview(al_geom, params)

    # Normaliza: se veio Feature, pega só a geometry
    geom_obj = al_geom
    if isinstance(geom_obj, dict) and geom_obj.get("type") == "Feature":
        geom_obj = geom_obj.get("geometry")

    # Tenta converter para GEOSGeometry
    try:
        al_geos = GEOSGeometry(json.dumps(geom_obj), srid=4326)
    except Exception:
        # Se algo der errado, não quebra: usa o fluxo antigo
        return compute_preview(al_geom, params)

    # Executa comandos PRE (por enquanto: criar praça centralizada)
    al_modificada, areas_publicas_novas = executar_comandos_pre(
        al_geos,
        comandos,
    )

    # Converte a nova área loteável para GeoJSON geometry
    al_mod_geojson = json.loads(al_modificada.geojson)

    # Chama o algoritmo normal de parcelamento usando a área já modificada
    preview = compute_preview(al_mod_geojson, params)

    # Garante que exista um FeatureCollection de áreas públicas
    ap_fc = preview.get("areas_publicas")
    if not isinstance(ap_fc, dict) or ap_fc.get("type") != "FeatureCollection":
        ap_fc = {"type": "FeatureCollection", "features": []}

    features = ap_fc["features"]

    # Anexa as novas áreas públicas (praças) criadas pelos comandos
    for ap in areas_publicas_novas:
        geom = ap["geometry"]
        tipo = ap.get("tipo", "praca")

        props = {
            "tipo": tipo,
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

    return preview
