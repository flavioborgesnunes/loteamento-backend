# services.py
import math
from typing import Dict, List, Optional, Tuple

from pyproj import Transformer
from shapely import affinity
from shapely.geometry import (LineString, MultiLineString, MultiPolygon, Point,
                              Polygon, mapping, shape)
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
    half = max(width_m, 0.0)/2.0
    return [l.buffer(half, cap_style=2, join_style=2) for l in lines]


def _geom_from_fc_wgs(fc: Optional[dict], to_m: Transformer):
    """Converte FC (polígonos/linhas) em shapely no SRID métrico (união)."""
    if not fc or fc.get("type") != "FeatureCollection":
        return None
    gs = []
    for f in fc.get("features", []):
        try:
            g = shapely_transform(shape(f.get("geometry")), to_m)
            if not g.is_empty:
                gs.append(g)
        except Exception:
            continue
    if not gs:
        return None
    u = unary_union(gs)
    return u if not u.is_empty else None

# ------------------ malha viária + quarteirões ------------------


def build_road_and_blocks(al_wgs_geojson: dict, params: dict, srid_calc: int = 3857) -> Tuple[dict, dict]:
    """
    Retorna (vias_fc, quarteiroes_fc) em WGS84.
    Heurística: vias 'verticais' espaçadas por (2*prof_min + larg_rua_vert),
                travessas horizontais a cada 'compr_max_quarteirao'.
    Respeita opcionalmente uma máscara de ruas existente (params['ruas_mask_fc']).
    """
    al_wgs = _ensure_multipolygon(shape(al_wgs_geojson))
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

    # grade de vias "sintéticas"
    spacing_vias = 2*prof_min + larg_v  # => largura do quarteirão ≈ 2*prof_min
    cx = al_m.bounds[0] + (al_m.bounds[2]-al_m.bounds[0])/2
    cy = al_m.bounds[1] + (al_m.bounds[3]-al_m.bounds[1])/2
    fam_vert = _gen_parallel_lines_covering_bbox(
        al_m.bounds, spacing_vias, angle, (cx, cy))
    fam_horiz = _gen_parallel_lines_covering_bbox(
        al_m.bounds, comp_max, angle+90.0, (cx, cy))

    # corredores da grade
    vias_vert_corr = buffer_lines_as_corridors(fam_vert,  larg_v)
    trav_corr = buffer_lines_as_corridors(fam_horiz, larg_h)
    corridors = [poly.intersection(al_m)
                 for poly in (vias_vert_corr + trav_corr)]
    corridors = [p for p in corridors if not p.is_empty]

    # --- RUAS EXISTENTES (opcional): entram como corredores proibidos
    ruas_mask_fc = params.get("ruas_mask_fc")
    if ruas_mask_fc:
        roads_m = _geom_from_fc_wgs(ruas_mask_fc, tf_wgs_to_m)
        if roads_m and not roads_m.is_empty:
            corridors.append(roads_m.intersection(al_m))

    # Quarteirões = AL - (corredores)
    all_corridors = unary_union(corridors) if corridors else None
    quarteiroes = al_m.difference(
        all_corridors) if all_corridors is not None else al_m
    quarteiroes = _ensure_multipolygon(quarteiroes)

    # Linhas centrais da grade (visual)
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

    def to_wgs(geom): return shapely_transform(geom, tf_m_to_wgs)

    vias_fc = {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "properties": {"tipo": v["tipo"], "largura_m": v["largura_m"]},
             "geometry": mapping(to_wgs(v["geom"]))}
            for v in vias_lines
        ]
    }

    # (quarteirões são gerados aqui para uso interno no slice; não enviaremos ao front no compute_preview)
    quarteiroes_fc = {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "properties": {},
                "geometry": mapping(to_wgs(q))}
            for q in quarteiroes.geoms
        ]
    }
    return vias_fc, quarteiroes_fc

# ------------------ lotes: 2 faixas + sobra simétrica + salvaguardas ------------------


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


def _pick_corner_point_aligned(poly_al: Polygon):
    """
    Escolhe um 'canto' estável no sistema alinhado:
      - menor Y (sul)
      - em empate, menor X (oeste)
    """
    coords = list(poly_al.exterior.coords)
    return min(coords, key=lambda c: (c[1], c[0]))  # (x,y)


def slice_lots(quarteiroes_fc: dict, params: dict, srid_calc: int = 3857) -> dict:
    """
    Regras (com tolerância e anti-lotes gigantes):
      - Tolerância padrão = 5% (params["tolerancia_frac"]).
      - Cada quarteirão vira 2 faixas (costas com costas) se couber (largura >= 2*prof_min*(1-tol)).
        Se não couber 2 faixas mas couber 1 (>= prof_min*(1-tol)), usa 1 faixa.
        Se não couber nem 1 lote-padrão, gera 1 único lote com a área total do quarteirão.
      - Ao longo do comprimento (X), cortes com passo = frente_min.
        A sobra é dividida igualmente nas duas extremidades (offset = leftover/2).
      - Nunca retornar lotes com área < area_min*(1-tol), nem frente < frente_min*(1-tol), nem prof < prof_min*(1-tol),
        EXCETO quando o quarteirão inteiro não comporta um lote-padrão — aí volta 1 lote único.
      - Se um pedaço resultar com área > 2*area_min*(1+tol), tenta dividir em 2 pelo meio do X.
        Só aceita a divisão se AMBOS os resultantes respeitarem os mínimos (com tolerância).
    """
    frente_min = float(params["frente_min_m"])
    prof_min = float(params["prof_min_m"])
    orient = params.get("orientacao_graus")
    tol = float(params.get("tolerancia_frac", 0.05))  # 5% default

    # mínimos com tolerância (aceitamos >= 95% por padrão)
    frente_req = frente_min * (1.0 - tol)
    prof_req = prof_min * (1.0 - tol)
    area_min = frente_min * prof_min
    area_req = area_min * (1.0 - tol)

    # limite superior para decidir “lote grande demais”
    area_double_limit = 2.0 * area_min * (1.0 + tol)

    tf_wgs_to_m = Transformer.from_crs(4326, srid_calc, always_xy=True)
    tf_m_to_wgs = Transformer.from_crs(srid_calc, 4326, always_xy=True)

    features = []
    lot_counter = 1

    def _emit_piece(p_al, angle, origin):
        """Converte p_al (no sistema alinhado) para WGS84 e emite como Feature."""
        nonlocal lot_counter
        # medidas no sistema alinhado
        pxmin, pymin, pxmax, pymax = p_al.bounds
        frente_aprox = max(pxmax - pxmin, 0.0)
        prof_aprox = max(pymax - pymin, 0.0)

        # geometria no CRS original (m)
        p_m = affinity.rotate(p_al, angle, origin=origin,
                              use_radians=False).buffer(0)
        if p_m.is_empty:
            return False
        area_m2 = float(abs(p_m.area))

        # rótulos (um ponto interno e um “canto” consistente SW)
        center_m_al: Point = p_al.representative_point()
        cx_al, cy_al = min(list(p_al.exterior.coords),
                           key=lambda c: (c[1], c[0]))
        corner_al_pt = Point(cx_al, cy_al)

        center_m_unrot = affinity.rotate(
            center_m_al, angle, origin=origin, use_radians=False)
        corner_m_unrot = affinity.rotate(
            corner_al_pt, angle, origin=origin, use_radians=False)

        # reprojeta para WGS84
        geom_wgs = shapely_transform(p_m, tf_m_to_wgs)
        center_wgs = shapely_transform(center_m_unrot, tf_m_to_wgs)
        corner_wgs = shapely_transform(corner_m_unrot, tf_m_to_wgs)

        features.append({
            "type": "Feature",
            "properties": {
                "lot_number": lot_counter,
                "frente_aprox_m": round(float(frente_aprox), 2),
                "prof_aprox_m":   round(float(prof_aprox),   2),
                "area_m2":        round(area_m2,             2),
                "label_center": [center_wgs.x, center_wgs.y],
                "label_corner": [corner_wgs.x, corner_wgs.y],
                "score_qualidade": 1.0
            },
            "geometry": mapping(geom_wgs)
        })
        lot_counter += 1
        return True

    def _piece_is_valid(p_al):
        """Checa mínimos com tolerância no sistema alinhado."""
        pxmin, pymin, pxmax, pymax = p_al.bounds
        frente_aprox = max(pxmax - pxmin, 0.0)
        prof_aprox = max(pymax - pymin, 0.0)
        if frente_aprox + tol*frente_min < frente_min:
            return False
        if prof_aprox + tol*prof_min < prof_min:
            return False
        # área em m² a partir da geometria no CRS alinhado (mesma área do CRS original)
        area_m2 = float(abs(p_al.area))
        # como p_al está no espaço métrico (após rotação), a área é em m²
        if area_m2 + tol*area_min < area_min:
            return False
        return True

    for feat in quarteiroes_fc.get("features", []):
        q_wgs = shape(feat["geometry"])
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

        # Caso especial: não cabe UM lote padrão (com tolerância) → 1 lote único do quarteirão
        if (span_x + tol*frente_min) < frente_min or (span_y + tol*prof_min) < prof_min:
            _emit_piece(q_al, angle, origin)
            continue

        # 1) montar faixas (2 se couber, senão 1)
        faixas_al = []
        if span_y >= 2.0 * prof_req:
            ymid = (miny + maxy) / 2.0
            center_line = LineString(
                [(minx - span_x, ymid), (maxx + span_x, ymid)])
            faixas_al = _split_once(q_al, [center_line])
            faixas_al = [f.buffer(0) for f in faixas_al if not f.is_empty]
            if len(faixas_al) >= 2:
                faixas_al.sort(key=lambda g: g.bounds[1])  # baixo→cima
                faixas_al = faixas_al[:2]
            else:
                faixas_al = [q_al]
        else:
            faixas_al = [q_al]  # 1 faixa

        # 2) fatiar cada faixa em frentes
        for faixa_al in faixas_al:
            if faixa_al.is_empty:
                continue

            fminx, fminy, fmaxx, fmaxy = faixa_al.bounds
            f_len = fmaxx - fminx
            if f_len <= 0:
                continue

            # Faixa muito curta para 2 frentes → lote único da faixa
            if f_len < (2.0 * frente_req):
                _emit_piece(faixa_al, angle, origin)
                continue

            # Cabe >= 2 frentes; “inflamos” um pouco para não perder por milímetros
            n = int(math.floor((f_len + tol * frente_min) / frente_min))
            n = max(2, n)
            base_total = n * frente_min
            leftover = max(f_len - base_total, 0.0)

            # sobra dividida nas pontas
            offset = leftover / 2.0

            # cortes verticais (no sistema alinhado)
            cuts = []
            for k in range(1, n):
                xk = fminx + offset + k * frente_min
                l = LineString([(xk, fminy - 10*prof_min),
                               (xk, fmaxy + 10*prof_min)])
                cuts.append(l)

            pedacos_al = _split_once(faixa_al, cuts)
            pedacos_al = [p.buffer(0) for p in pedacos_al if not p.is_empty]
            pedacos_al.sort(key=lambda g: g.bounds[0])  # esquerda→direita

            # 3) para cada pedaço: valida mínimos e, se área exagerada, tenta dividir em 2
            stack = pedacos_al[:]  # processaremos como pilha
            while stack:
                p_al = stack.pop(0)  # FIFO para manter ordem
                if p_al.is_empty:
                    continue

                # medidas rápidas
                pxmin, pymin, pxmax, pymax = p_al.bounds
                frente_aprox = max(pxmax - pxmin, 0.0)
                prof_aprox = max(pymax - pymin, 0.0)
                if frente_aprox <= 0.0 or prof_aprox <= 0.0:
                    continue

                # área em m² (no alinhado já é métrico)
                area_m2_aligned = float(abs(p_al.area))

                # checa mínimos
                if (frente_aprox + tol*frente_min < frente_min or
                    prof_aprox + tol*prof_min < prof_min or
                        area_m2_aligned + tol*area_min < area_min):
                    # se não atende mínimos, descarta (não gera lote inválido)
                    continue

                # se ficou “grande demais”, tenta dividir em 2 pelo meio do X
                if area_m2_aligned > area_double_limit and frente_aprox >= 2.0 * frente_req:
                    xmid = (pxmin + pxmax) / 2.0
                    cut = LineString(
                        [(xmid, pymin - 10*prof_min), (xmid, pymax + 10*prof_min)])
                    subparts = _split_once(p_al, [cut])
                    subparts = [pp.buffer(0)
                                for pp in subparts if not pp.is_empty]
                    if len(subparts) == 2:
                        sp1, sp2 = subparts
                        # só aceita a divisão se AMBOS forem válidos
                        if _piece_is_valid(sp1) and _piece_is_valid(sp2):
                            # mantém a ordem esquerda→direita
                            subparts.sort(key=lambda g: g.bounds[0])
                            stack = subparts + stack
                            continue  # processará os sublotes no próximo loop
                    # se não conseguiu dividir bem, segue com o pedaço original

                # emite o lote
                _emit_piece(p_al, angle, origin)

    return {"type": "FeatureCollection", "features": features}

# ------------------ preview ------------------


def compute_preview(al_geojson: dict, params: dict) -> Dict:
    vias_fc, quarteiroes_fc = build_road_and_blocks(
        al_geojson, params, params.get("srid_calc", 3857))

    # Gera lotes A PARTIR DOS QUARTEIRÕES, porém não envia os quarteirões ao front
    lotes_fc = slice_lots(quarteiroes_fc, params,
                          params.get("srid_calc", 3857))

    metrics = {
        "n_vias": len(vias_fc["features"]),
        "n_quarteiroes": len(quarteiroes_fc["features"]),
        "n_lotes": len(lotes_fc["features"]),
    }

    # NÃO retornar quarteiroes (somente para métricas internas)
    return {
        "vias": vias_fc,
        "quarteiroes": {"type": "FeatureCollection", "features": []},
        "lotes": lotes_fc,
        "metrics": metrics
    }
