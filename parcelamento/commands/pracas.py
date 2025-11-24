# parcelamento/commands/pracas.py

from math import pi, sqrt

from django.contrib.gis.geos import GEOSGeometry, MultiPolygon


def aplicar_comando_criar_praca(
    area_loteavel: GEOSGeometry,
    centro,
    tamanho: dict,
    forma: dict,
    cmd: dict,
):
    """
    Cria uma praça circular (v1) removendo parte da área_loteavel.

    - area_loteavel: GEOSGeometry (Polygon ou MultiPolygon)
    - centro: GEOSGeometry (Point) onde a praça deve ser criada
    - tamanho: bloco "tamanho" do comando
    - forma: bloco "forma" do comando
    - cmd: comando completo (para debug futuro)
    """

    # Se não tem área loteável, não faz nada
    if area_loteavel is None or area_loteavel.empty:
        return area_loteavel, None

    # Se não tem centro válido, não faz nada
    if centro is None or centro.empty:
        return area_loteavel, None

    # --- definir tamanho ---
    tipo = (tamanho or {}).get("tipo", "raio_relativo")
    try:
        valor = float((tamanho or {}).get("valor", 0.15))
    except (TypeError, ValueError):
        valor = 0.15

    area_m2 = float(area_loteavel.area or 0.0)
    if area_m2 <= 0:
        return area_loteavel, None

    if tipo == "raio_relativo":
        # raio base aproximado em função da área total
        raio_base = sqrt(area_m2 / pi) / 2.0
        raio = max(raio_base * valor, 1.0)

    elif tipo == "raio_absoluto_m":
        raio = max(valor, 1.0)

    elif tipo == "area_alvo_m2":
        # raio equivalente à área desejada
        valor = max(valor, 1.0)
        raio = sqrt(valor / pi)

    else:
        # fallback bem conservador
        raio = 1.0

    # --- forma da praça ---
    forma_tipo = (forma or {}).get("tipo", "circulo")

    # Para v1, independente da forma, usamos buffer circular
    if forma_tipo == "circulo":
        praca_geom = centro.buffer(raio)
    else:
        # futuro: retângulo, formas específicas, etc.
        praca_geom = centro.buffer(raio)

    # Interseção com área loteável (evita vazar para fora)
    praca_geom = praca_geom.intersection(area_loteavel)

    # Aqui a sintaxe correta é .empty (GEOS), não .is_empty (Shapely)
    if praca_geom is None or praca_geom.empty:
        return area_loteavel, None

    # Remover a praça da área loteável
    nova_area = area_loteavel.difference(praca_geom)

    # Em alguns casos, difference pode retornar vazio ou None
    if nova_area is None or nova_area.empty:
        # Nesse caso extremo, melhor não alterar nada
        return area_loteavel, praca_geom

    return nova_area, praca_geom
