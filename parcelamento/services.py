# services.py
import math
from typing import Dict, List, Tuple

from pyproj import Transformer
from shapely import affinity
from shapely.geometry import (LineString, MultiLineString, MultiPolygon,
                              Polygon, box, mapping, shape)
from shapely.ops import split
from shapely.ops import transform as shp_transform
from shapely.ops import unary_union

# ------------------ util básicos ------------------


def _ensure_multipolygon(g):
    if isinstance(g, Polygon):
        return MultiPolygon([g])
    if isinstance(g, MultiPolygon):
        return g
    raise ValueError("AL deve ser Polygon/MultiPolygon")


def shapely_transform(geom, transformer: Transformer):
    """Aplicar transformação de coordenadas em geometria shapely."""
    def _tx_xy(x, y, z=None):
        x2, y2 = transformer.transform(x, y)
        return (x2, y2) if z is None else (x2, y2, z)
    return shp_transform(_tx_xy, geom)


def estimate_orientation_deg(geom_m):
    """Estimativa simples pelo retângulo mínimo (OBB)."""
    try:
        minrect = geom_m.minimum_rotated_rectangle
        coords = list(minrect.exterior.coords)
        max_d, ang = 0, 0
        for i in range(len(coords)-1):
            x1, y1 = coords[i]
            x2, y2 = coords[i+1]
            dx, dy = (x2-x1, y2-y1)
            d = math.hypot(dx, dy)
            if d > max_d:
                max_d = d
                ang = math.degrees(math.atan2(dy, dx))
        return ang % 180.0
    except Exception:
        return 0.0


def _gen_parallel_lines_covering_bbox(bbox, spacing, angle_deg, center):
    """Gera uma família de linhas paralelas cobrindo o bbox, com espaçamento (m)."""
    minx, miny, maxx, maxy = bbox
    W, H = (maxx-minx, maxy-miny)
    diag = math.hypot(W, H) + spacing*2
    cx, cy = center
    base = LineString([(cx - diag/2, cy), (cx + diag/2, cy)])
    base = affinity.rotate(base, angle_deg, origin=(cx, cy), use_radians=False)
    ortho = angle_deg + 90
    lines = []
    n = int((max(W, H) + diag) / spacing) + 4
    for k in range(-n, n+1):
        l = affinity.translate(
            base,
            xoff=math.cos(math.radians(ortho))*k*spacing,
            yoff=math.sin(math.radians(ortho))*k*spacing
        )
        lines.append(l)
    return lines


def buffer_lines_as_corridors(lines: List[LineString], width_m: float):
    half = width_m/2.0
    return [l.buffer(half, cap_style=2, join_style=2) for l in lines]


# ------------------ malha viária + quarteirões ------------------

def build_road_and_blocks(al_wgs_geojson: dict, params: dict, srid_calc: int = 3857) -> Tuple[dict, dict]:
    """
    Retorna (vias_fc, quarteiroes_fc) em WGS84.
    Heurística: vias 'verticais' espaçadas por (2*prof_min + larg_rua_vert).
                travessas horizontais a cada 'compr_max_quarteirao'.
    """
    al_wgs = _ensure_multipolygon(shape(al_wgs_geojson))
    # reprojeta para métrico
    tf_wgs_to_m = Transformer.from_crs(4326, srid_calc, always_xy=True)
    tf_m_to_wgs = Transformer.from_crs(srid_calc, 4326, always_xy=True)
    al_m = shapely_transform(al_wgs, tf_wgs_to_m)

    frente_min = float(params["frente_min_m"])
    prof_min = float(params["prof_min_m"])
    larg_v = float(params["larg_rua_vert_m"])
    larg_h = float(params["larg_rua_horiz_m"])
    comp_max = float(params["compr_max_quarteirao_m"])
    orient = params.get("orientacao_graus")
    angle = float(
        orient) if orient is not None else estimate_orientation_deg(al_m)

    # --------- Vias "verticais" (familia paralela ao ângulo 'angle') ----------
    spacing_vias = 2*prof_min + larg_v  # => largura do quarteirão ≈ 2*prof_min
    cx = al_m.bounds[0] + (al_m.bounds[2]-al_m.bounds[0])/2
    cy = al_m.bounds[1] + (al_m.bounds[3]-al_m.bounds[1])/2
    fam_vert = _gen_parallel_lines_covering_bbox(
        al_m.bounds, spacing_vias, angle, (cx, cy))

    # Corredores das vias verticais
    vias_vert_corr = buffer_lines_as_corridors(fam_vert, larg_v)
    vias_vert_corr = [poly.intersection(al_m) for poly in vias_vert_corr]
    vias_vert_corr = [p for p in vias_vert_corr if not p.is_empty]

    # --------- Travessas horizontais com passo 'comp_max' ----------
    fam_horiz = _gen_parallel_lines_covering_bbox(
        al_m.bounds, comp_max, angle+90.0, (cx, cy))
    trav_corr = buffer_lines_as_corridors(fam_horiz, larg_h)
    trav_corr = [poly.intersection(al_m) for poly in trav_corr]
    trav_corr = [p for p in trav_corr if not p.is_empty]

    # Quarteirões = AL - (vias vert + travessas)
    all_corridors = unary_union(vias_vert_corr + trav_corr)
    quarteiroes = al_m.difference(all_corridors)
    quarteiroes = _ensure_multipolygon(quarteiroes)

    # Linhas centrais das vias (visual)
    vias_lines = []
    for l in fam_vert:
        cl = l.intersection(al_m)
        if not cl.is_empty:
            vias_lines.append(
                {"tipo": "vertical", "largura_m": larg_v, "geom": cl})
    for l in fam_horiz:
        cl = l.intersection(al_m)
        if not cl.is_empty:
            vias_lines.append(
                {"tipo": "horizontal", "largura_m": larg_h, "geom": cl})

    # reprojeta de volta
    def to_wgs(geom): return shapely_transform(geom, tf_m_to_wgs)

    vias_fc = {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "properties": {"tipo": v["tipo"], "largura_m": v["largura_m"]},
             "geometry": mapping(to_wgs(v["geom"]))}
            for v in vias_lines
        ]
    }
    quarteiroes_fc = {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "properties": {},
                "geometry": mapping(to_wgs(q))}
            for q in quarteiroes.geoms
        ]
    }
    return vias_fc, quarteiroes_fc


# ------------------ lotes: 2 faixas + frentes com sobra no último ------------------

def _rotate_align(g, angle_deg: float, origin):
    """Roda geometria em -angle para alinhar eixo longo no X."""
    return affinity.rotate(g, -angle_deg, origin=origin, use_radians=False)


def _rotate_back(g, angle_deg: float, origin):
    """Desfaz rotação (+angle)."""
    return affinity.rotate(g, angle_deg, origin=origin, use_radians=False)


def _split_once(target_geom, cutters):
    """Split único com união das linhas de corte (robusto)."""
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


def slice_lots(quarteiroes_fc: dict, params: dict, srid_calc: int = 3857) -> dict:
    """
    Regras:
      - Cada quarteirão vira 2 faixas (costas com costas) em profundidade.
      - Ao longo do comprimento, cortes com passo = frente_min.
      - Se sobrar comprimento < frente_min, o ÚLTIMO lote absorve a sobra (fica mais largo).
    """
    frente_min = float(params["frente_min_m"])
    prof_min = float(params["prof_min_m"])
    orient = params.get("orientacao_graus")

    tf_wgs_to_m = Transformer.from_crs(4326, srid_calc, always_xy=True)
    tf_m_to_wgs = Transformer.from_crs(srid_calc, 4326, always_xy=True)

    features = []
    for feat in quarteiroes_fc.get("features", []):
        q_wgs = shape(feat["geometry"])
        q_m = shapely_transform(q_wgs, tf_wgs_to_m)
        if q_m.is_empty:
            continue

        # orientação do quarteirão
        angle = float(
            orient) if orient is not None else estimate_orientation_deg(q_m)

        # alinhar quarteirão: eixo longitudinal => X ; largura (profundidade total) => Y
        origin = (q_m.centroid.x, q_m.centroid.y)
        q_al = _rotate_align(q_m, angle, origin=origin)
        minx, miny, maxx, maxy = q_al.bounds
        span_x = maxx - minx  # comprimento
        span_y = maxy - miny  # largura ≈ 2 * prof_(real)

        if span_x <= 0 or span_y <= 0:
            continue

        # 1) cortar quarteirão ao meio (linha central y = ymid) => duas faixas
        ymid = (miny + maxy) / 2.0
        # um pouco maior que o bbox para garantir interseção
        center_line = LineString(
            [(minx - span_x, ymid), (maxx + span_x, ymid)])
        faixas_al = _split_once(q_al, [center_line])

        # filtra as duas faixas válidas
        faixas_al = [f.buffer(0) for f in faixas_al if not f.is_empty]
        if len(faixas_al) == 1:
            # quarteirão “fininho” – ainda assim tratamos como 1 faixa
            faixas_al = [faixas_al[0]]
        elif len(faixas_al) >= 2:
            # pega as duas com maior área (se o split retornar mais)
            faixas_al.sort(key=lambda g: g.area, reverse=True)
            faixas_al = faixas_al[:2]

        # 2) para CADA faixa, fatiar por frentes ao longo do X
        for faixa_al in faixas_al:
            if faixa_al.is_empty:
                continue
            fminx, fminy, fmaxx, fmaxy = faixa_al.bounds
            f_len = fmaxx - fminx
            if f_len <= 0:
                continue

            # número de lotes “inteiros” com frente_min
            # vamos criar cortes em x = fminx + k*frente_min, k=1..(n-1)
            # onde n = max(1, floor(f_len / frente_min))
            n = max(1, int(math.floor(f_len / frente_min)))
            cuts = []
            if n >= 2:
                # cria (n-1) cortes verticais (x constantes) cobrindo toda a faixa
                for k in range(1, n):
                    xk = fminx + k * frente_min
                    l = LineString([(xk, fminy - 10*prof_min),
                                   (xk, fmaxy + 10*prof_min)])
                    cuts.append(l)

            # split único da faixa
            pedacos_al = _split_once(faixa_al, cuts)

            # Observação: se houve sobra (f_len - n*frente_min),
            # o último pedaço naturalmente fica mais largo. Missão cumprida.

            for p_al in pedacos_al:
                p_al = p_al.buffer(0)
                if p_al.is_empty:
                    continue

                # Frente ≈ extensão no X; Profundidade ≈ extensão no Y (no sistema alinhado)
                pxmin, pymin, pxmax, pymax = p_al.bounds
                frente_aprox = max(pxmax - pxmin, 0.0)
                prof_aprox = max(pymax - pymin, 0.0)

                # filtrar retalhos: exigimos profundidade ~ prof_min (com tolerância)
                if prof_aprox < 0.70 * prof_min:
                    continue

                # volta para o sistema original e depois para WGS84
                p_m = _rotate_back(p_al, angle, origin=origin).buffer(0)
                if p_m.is_empty:
                    continue
                geom_wgs = shapely_transform(p_m, tf_m_to_wgs)

                features.append({
                    "type": "Feature",
                    "properties": {
                        "frente_aprox_m": round(float(frente_aprox), 2),
                        "prof_aprox_m": round(float(prof_aprox), 2),
                        # área no alinhado ~ métrica
                        "area_m2": round(float(abs(p_al.area)), 2),
                        "score_qualidade": 1.0
                    },
                    "geometry": mapping(geom_wgs)
                })

    return {"type": "FeatureCollection", "features": features}


# ------------------ preview ------------------

def compute_preview(al_geojson: dict, params: dict) -> Dict:
    vias_fc, quarteiroes_fc = build_road_and_blocks(
        al_geojson, params, params.get("srid_calc", 3857))
    lotes_fc = slice_lots(quarteiroes_fc, params,
                          params.get("srid_calc", 3857))
    metrics = {
        "n_vias": len(vias_fc["features"]),
        "n_quarteiroes": len(quarteiroes_fc["features"]),
        "n_lotes": len(lotes_fc["features"]),
    }
    return {"vias": vias_fc, "quarteiroes": quarteiroes_fc, "lotes": lotes_fc, "metrics": metrics}
