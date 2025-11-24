# parcelamento/commands/locators.py

from django.contrib.gis.geos import GEOSGeometry


def localizar_geometria(area_loteavel: GEOSGeometry, loc: dict | None):
    """
    Retorna um ponto ou geometria-alvo com base na estratégia de localização.

    Por enquanto, implementamos:
      - "centro_da_area_loteavel"
      - default (quando não vier nada): centro da área loteável
    """

    if area_loteavel is None or area_loteavel.empty:
        return None

    loc = loc or {}
    estrategia = (loc.get("estrategia") or "").strip(
    ) or "centro_da_area_loteavel"
    estrategia = estrategia.lower()

    # Centro da área loteável (caso principal)
    if estrategia == "centro_da_area_loteavel":
        return area_loteavel.centroid

    # FUTURO:
    # if estrategia == "maior_quarteirao":
    #    ...
    # if estrategia == "em_quarteirao_id":
    #    ...

    # fallback: centro também
    return area_loteavel.centroid
