# parcelamento/commands/executor.py

from typing import Any, Dict, List, Tuple

from django.contrib.gis.geos import GEOSGeometry

from .locators import localizar_geometria
from .pracas import aplicar_comando_criar_praca


def executar_comandos_pre(
    area_loteavel: GEOSGeometry,
    comandos: List[Dict[str, Any]],
) -> Tuple[GEOSGeometry, List[Dict[str, Any]]]:
    """
    Executa comandos que agem ANTES do parcelamento gerar vias/quarteirões/lotes.

    - area_loteavel: GEOSGeometry (Polygon ou MultiPolygon)
    - comandos: lista de comandos do campo "comandos" da IA

    Retorna:
      - nova área loteável (com recortes/aplicações de comandos)
      - lista de áreas públicas (cada item: {"tipo": ..., "geometry": GEOSGeometry, ...})
    """

    if area_loteavel is None or area_loteavel.empty:
        return area_loteavel, []

    areas_publicas: List[Dict[str, Any]] = []
    area_modificada: GEOSGeometry = area_loteavel

    for cmd in comandos or []:
        if not isinstance(cmd, dict):
            continue

        # Normaliza acao e momento
        acao = (cmd.get("acao") or "").strip().lower()
        momento = (cmd.get("momento") or "pre").strip().lower()

        # Só tratamos comandos "pre" aqui
        if momento != "pre":
            continue

        tipo_cmd = (cmd.get("tipo") or "").strip().lower()

        # --- Comando: criar praça ---
        # Aceitamos tanto "criar_praca" (contrato oficial) quanto "criar_area_publica" com tipo "praca"
        eh_praca = (
            acao == "criar_praca"
            or (acao == "criar_area_publica" and tipo_cmd == "praca")
        )

        if eh_praca:
            loc = cmd.get("localizacao") or {}
            tamanho = cmd.get("tamanho") or {}
            forma = cmd.get("forma") or {}

            # Descobrir onde aplicar (centro_da_area_loteavel, etc.)
            ponto_ou_area = localizar_geometria(area_modificada, loc)
            if ponto_ou_area is None or ponto_ou_area.empty:
                continue

            nova_area, praca_geom = aplicar_comando_criar_praca(
                area_modificada,
                ponto_ou_area,
                tamanho=tamanho,
                forma=forma,
                cmd=cmd,
            )

            if praca_geom is not None and not praca_geom.empty:
                area_modificada = nova_area

                areas_publicas.append(
                    {
                        "id": cmd.get("id"),
                        "tipo": "praca",
                        "geometry": praca_geom,
                        "nome": cmd.get("nome", ""),
                        "descricao": cmd.get(
                            "descricao",
                            "Praça criada a partir de comando da IA.",
                        ),
                        "origem": "ia",
                    }
                )

        # FUTURO:
        # elif acao == "unir_lotes":
        #     ...

    return area_modificada, areas_publicas
