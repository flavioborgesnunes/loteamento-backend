from __future__ import annotations

import json
import logging
from typing import Any, Dict

from django.conf import settings
from django.shortcuts import get_object_or_404
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from shapely.geometry import shape

from parcelamento.models import ParcelamentoPlano
from parcelamento.services import compute_preview
from .rag import load_rag_context
from .openai_client import get_default_model_name
from openai import OpenAI

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OpenAI client
# ---------------------------------------------------------------------------

client = OpenAI(api_key=settings.OPENAI_API_KEY)


# ---------------------------------------------------------------------------
# Helpers de parâmetros
# ---------------------------------------------------------------------------

def _merge_plan_params(plano: ParcelamentoPlano, params_iniciais: Dict[str, Any] | None) -> Dict[str, Any]:
    """
    Junta os parâmetros do plano com overrides opcionais do front.
    """
    params_iniciais = params_iniciais or {}
    base = {
        "frente_min_m": float(params_iniciais.get("frente_min_m", plano.frente_min_m)),
        "prof_min_m": float(params_iniciais.get("prof_min_m", plano.prof_min_m)),
        "larg_rua_vert_m": float(
            params_iniciais.get("larg_rua_vert_m", plano.larg_rua_vert_m)
        ),
        "larg_rua_horiz_m": float(
            params_iniciais.get("larg_rua_horiz_m", plano.larg_rua_horiz_m)
        ),
        "compr_max_quarteirao_m": float(
            params_iniciais.get(
                "compr_max_quarteirao_m", plano.compr_max_quarteirao_m
            )
        ),
        "orientacao_graus": params_iniciais.get(
            "orientacao_graus",
            float(plano.orientacao_graus) if plano.orientacao_graus is not None else None,
        ),
        "srid_calc": int(params_iniciais.get("srid_calc", plano.srid_calc)),
        "has_ruas_mask_fc": bool(params_iniciais.get("has_ruas_mask_fc", False)),
        "has_ruas_eixo_fc": bool(params_iniciais.get("has_ruas_eixo_fc", False)),
        "ruas_mask_fc": params_iniciais.get("ruas_mask_fc"),
        "ruas_eixo_fc": params_iniciais.get("ruas_eixo_fc"),
        "guia_linha_fc": params_iniciais.get("guia_linha_fc"),
        "dist_min_rua_quarteirao_m": float(
            params_iniciais.get("dist_min_rua_quarteirao_m", 0.0)
        )
        if params_iniciais.get("dist_min_rua_quarteirao_m") is not None
        else None,
        "tolerancia_frac": float(params_iniciais.get("tolerancia_frac", 0.05)),
        "calcada_largura_m": float(
            params_iniciais.get("calcada_largura_m", 2.5)
        ),
    }
    return base


def _summarize_al(al_geom: dict | None) -> Dict[str, Any]:
    """
    Faz um resumo simples da área loteável para mandar no prompt.
    """
    if not al_geom:
        return {"area_m2_aprox": None, "bbox": None}

    try:
        g = shape(al_geom)
        if g.is_empty:
            return {"area_m2_aprox": 0, "bbox": None}
        minx, miny, maxx, maxy = g.bounds
        area = abs(g.area)
        return {
            "area_m2_aprox": area,
            "bbox": [minx, miny, maxx, maxy],
        }
    except Exception:
        return {"area_m2_aprox": None, "bbox": None}


# ---------------------------------------------------------------------------
# Chamada de IA
# ---------------------------------------------------------------------------

def _call_openai_sugerir(
    *,
    al_geom: dict | None,
    params_base: Dict[str, Any],
    restricoes_resumo: Dict[str, Any],
    preferencias_usuario: str = "",
) -> dict:
    """
    Chama a IA para sugerir parâmetros de parcelamento.

    Espera resposta em JSON no formato:

    {
      "params_sugeridos": {...},
      "observacoes": "texto",
      "elementos_especiais": [...]
    }
    """

    model = getattr(settings, "OPENAI_PARCELAMENTO_MODEL", None) or get_default_model_name()

    al_resumo = _summarize_al(al_geom)
    rag_ctx = load_rag_context()  # texto com normas/boas práticas

    prompt_text = f"""
Você é uma IA urbanista especializada em parcelamento do solo (Brasil).

Você deve SUGERIR parâmetros numéricos para o cálculo automático de:
- malha viária (ruas verticais / horizontais),
- quarteirões,
- lotes (frente e profundidade mínimas),
- largura de calçadas,

sempre respeitando o mínimo de DOIS LOTES (profundidade+profundidade) entre vias paralelas,
e buscando frentes próximas de 10m e áreas próximas de 250m², a não ser que as restrições impeçam.

Responda **APENAS** com um JSON válido UTF-8, SEM texto extra fora do JSON, no formato:

{{
  "params_sugeridos": {{
    "frente_min_m": float,
    "prof_min_m": float,
    "larg_rua_vert_m": float,
    "larg_rua_horiz_m": float,
    "compr_max_quarteirao_m": float,
    "orientacao_graus": float ou null,
    "srid_calc": 3857,
    "has_ruas_mask_fc": bool,
    "has_ruas_eixo_fc": bool,
    "calcada_largura_m": float
  }},
  "observacoes": "string explicando as escolhas em linguagem simples",
  "elementos_especiais": [
    "descrições de elementos especiais sugeridos, como praça circular, rotatória, acessos, etc."
  ]
}}

-------------------------------
CONHECIMENTO BASE (RAG):
{rag_ctx}

-------------------------------
RESUMO DA ÁREA LOTEÁVEL (WGS84):
{json.dumps(al_resumo, ensure_ascii=False, indent=2)}

-------------------------------
PARÂMETROS BASE (defaults calculados no backend):
{json.dumps(params_base, ensure_ascii=False, indent=2)}

-------------------------------
RESTRIÇÕES E CONTEXTO (rios, áreas verdes, LT, etc.):
{json.dumps(restricoes_resumo or {{}}, ensure_ascii=False, indent=2)}

-------------------------------
PREFERÊNCIAS DO USUÁRIO:
\"\"\"{preferencias_usuario or ""}\"\"\"
"""

    logger.info("[IA] Chamando modelo %s para sugerir parâmetros de parcelamento", model)

    resp = client.responses.create(
        model=model,
        input=prompt_text,
        max_output_tokens=800,
    )

    # ---- extrair texto da resposta ----
    text = ""
    try:
        first_output = resp.output[0]
        for item in getattr(first_output, "content", []):
            if hasattr(item, "text") and item.text:
                if isinstance(item.text, str):
                    text += item.text
                elif hasattr(item.text, "value"):
                    text += item.text.value
    except Exception as e:
        logger.warning("[IA] Falha ao extrair texto de resp.output: %s", e)
        try:
            text = getattr(resp, "output_text", "") or ""
        except Exception:
            text = ""

    text = (text or "").strip()
    logger.info("[IA] Texto bruto da resposta (primeiros 400 chars): %s", text[:400])

    if not text:
        raise ValueError("Resposta vazia da IA para sugerir parâmetros")

    # ---- tentar converter para JSON ----
    try:
        data = json.loads(text)
        return data
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            snippet = text[start : end + 1]
            data = json.loads(snippet)
            return data
        logger.error("[IA] Não foi possível parsear JSON da resposta: %s", text)
        raise


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

class SugerirParametrosView(APIView):
    """
    IA sugere parâmetros de parcelamento, sem gerar geometria.

    Endpoint: POST /api/ia-parcelamento/planos/<plano_id>/sugerir-parametros/

    Payload esperado (mesmo do front):

    {
      "al_geom": { ...GeoJSON geometry... },
      "params_iniciais": { ... },
      "restricoes_resumo": { ... },
      "preferencias_usuario": "texto livre"
    }
    """

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, plano_id: int, *args, **kwargs):
        plano = get_object_or_404(ParcelamentoPlano, pk=plano_id)

        data = request.data if isinstance(request.data, dict) else {}
        logger.info("[IA Sugerir] request.data = %s", data)

        # Área loteável (opcional para a IA; se faltar, ela só tem menos contexto)
        al_geom = data.get("al_geom") or data.get("alFeature") or data.get("al_geojson")
        if isinstance(al_geom, dict) and al_geom.get("type") == "Feature":
            al_geom = al_geom.get("geometry")

        params_iniciais = data.get("params_iniciais") or data.get("params") or {}
        restricoes_resumo = data.get("restricoes_resumo") or {}
        preferencias_usuario = (
            data.get("preferencias_usuario")
            or data.get("prompt_usuario")
            or data.get("prompt")
            or ""
        )

        base_params = _merge_plan_params(plano, params_iniciais)

        try:
            ia_out = _call_openai_sugerir(
                al_geom=al_geom,
                params_base=base_params,
                restricoes_resumo=restricoes_resumo,
                preferencias_usuario=preferencias_usuario,
            )
        except Exception as e:
            logger.exception("[IA Sugerir] Erro ao chamar IA: %s", e)
            return Response(
                {
                    "detail": "Erro ao chamar o modelo de IA para sugerir parâmetros.",
                    "error": str(e),
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        # Garante chaves mínimas e devolve direto, sem serializer
        params_sugeridos = ia_out.get("params_sugeridos") or {}
        observacoes = ia_out.get("observacoes") or ""
        elementos_especiais = ia_out.get("elementos_especiais") or []

        resp_data = {
            "params_sugeridos": params_sugeridos,
            "observacoes": observacoes,
            "elementos_especiais": elementos_especiais,
            # opcional, mas útil pra debug:
            "debug": {
                "base_params": base_params,
                "al_resumo": _summarize_al(al_geom),
            },
        }

        return Response(resp_data, status=status.HTTP_200_OK)


class PreviewIaView(APIView):
    """
    IA sugere parâmetros e já chama compute_preview,
    retornando a prévia completa + metadados da IA.
    """

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, plano_id: int, *args, **kwargs):
        plano = get_object_or_404(ParcelamentoPlano, pk=plano_id)

        data = request.data if isinstance(request.data, dict) else {}
        logger.info("[IA Preview] request.data = %s", data)

        al_geom = data.get("al_geom") or data.get("alFeature") or data.get("al_geojson")
        if isinstance(al_geom, dict) and al_geom.get("type") == "Feature":
            al_geom = al_geom.get("geometry")

        if not al_geom:
            return Response(
                {"detail": "Campo 'al_geom' é obrigatório (GeoJSON Polygon/MultiPolygon)."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        params_iniciais = data.get("params_iniciais") or data.get("params") or {}
        restricoes_resumo = data.get("restricoes_resumo") or {}
        preferencias_usuario = (
            data.get("preferencias_usuario")
            or data.get("prompt_usuario")
            or data.get("prompt")
            or ""
        )

        base_params = _merge_plan_params(plano, params_iniciais)

        try:
            ia_out = _call_openai_sugerir(
                al_geom=al_geom,
                params_base=base_params,
                restricoes_resumo=restricoes_resumo,
                preferencias_usuario=preferencias_usuario,
            )
        except Exception as e:
            logger.exception("[IA Preview] Erro ao chamar IA: %s", e)
            return Response(
                {
                    "detail": "Erro ao chamar o modelo de IA para sugerir parâmetros.",
                    "error": str(e),
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        params_final = ia_out.get("params_sugeridos") or base_params
        preview = compute_preview(al_geom, params_final)

        resp_data = {
            **preview,
            "params_usados": params_final,
            "ia_metadata": {
                "observacoes": ia_out.get("observacoes") or "",
                "elementos_especiais": ia_out.get("elementos_especiais") or [],
                "params_sugeridos": ia_out.get("params_sugeridos") or {},
            },
        }

        return Response(resp_data, status=status.HTTP_200_OK)


class SvgPreviewIaView(APIView):
    """
    Gera um SVG simples da prévia baseada na IA.
    (Usa compute_preview por baixo, depois transforma em SVG.)
    """

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, plano_id: int, *args, **kwargs):
        plano = get_object_or_404(ParcelamentoPlano, pk=plano_id)

        data = request.data if isinstance(request.data, dict) else {}
        logger.info("[IA SVG] request.data = %s", data)

        al_geom = data.get("al_geom") or data.get("alFeature") or data.get("al_geojson")
        if isinstance(al_geom, dict) and al_geom.get("type") == "Feature":
            al_geom = al_geom.get("geometry")

        if not al_geom:
            return Response(
                {
                    "svg": "<svg xmlns='http://www.w3.org/2000/svg'></svg>",
                    "detail": "Campo 'al_geom' é obrigatório (GeoJSON Polygon/MultiPolygon).",
                },
                status=status.HTTP_200_OK,
            )

        params_iniciais = data.get("params_iniciais") or data.get("params") or {}
        restricoes_resumo = data.get("restricoes_resumo") or {}
        preferencias_usuario = (
            data.get("preferencias_usuario")
            or data.get("prompt_usuario")
            or data.get("prompt")
            or ""
        )

        base_params = _merge_plan_params(plano, params_iniciais)

        try:
            ia_out = _call_openai_sugerir(
                al_geom=al_geom,
                params_base=base_params,
                restricoes_resumo=restricoes_resumo,
                preferencias_usuario=preferencias_usuario,
            )
        except Exception as e:
            logger.exception("[IA SVG] Erro ao chamar IA: %s", e)
            return Response(
                {
                    "svg": "<svg xmlns='http://www.w3.org/2000/svg'></svg>",
                    "detail": "Erro ao chamar o modelo de IA para sugerir parâmetros.",
                    "error": str(e),
                },
                status=status.HTTP_200_OK,
            )

        params_final = ia_out.get("params_sugeridos") or base_params
        preview = compute_preview(al_geom, params_final)
        lotes_fc = preview["lotes"]
        vias_area_fc = preview["vias_area"]

        # --- converte FCs em um SVG bem simples em coordenadas WGS84 (lon/lat) ---
        from shapely.geometry import shape as shp_shape, Polygon, MultiPolygon

        xs, ys = [], []
        for fc in (lotes_fc, vias_area_fc):
            for f in fc.get("features", []):
                g = shp_shape(f["geometry"])
                if g.is_empty:
                    continue
                if isinstance(g, (Polygon, MultiPolygon)):
                    geoms = [g] if isinstance(g, Polygon) else list(g.geoms)
                    for gg in geoms:
                        for x, y in gg.exterior.coords:
                            xs.append(x)
                            ys.append(y)
                        for ring in gg.interiors:
                            for x, y in ring.coords:
                                xs.append(x)
                                ys.append(y)

        if not xs or not ys:
            return Response(
                {"svg": "<svg xmlns='http://www.w3.org/2000/svg'></svg>"},
                status=status.HTTP_200_OK,
            )

        minx, maxx = min(xs), max(xs)
        miny, maxy = min(ys), max(ys)
        width = maxx - minx
        height = maxy - miny
        if width <= 0:
            width = 1
        if height <= 0:
            height = 1

        def _to_svg_xy(x, y):
            nx = (x - minx) / width
            ny = 1.0 - (y - miny) / height
            return nx * 1000, ny * 1000

        def _poly_to_path(geom, stroke, fill):
            if geom.is_empty:
                return ""
            if isinstance(geom, Polygon):
                geoms = [geom]
            elif isinstance(geom, MultiPolygon):
                geoms = list(geom.geoms)
            else:
                geoms = [geom]

            parts = []
            for g in geoms:
                if g.is_empty:
                    continue
                coords = list(g.exterior.coords)
                if not coords:
                    continue
                d_cmds = []
                for i, (x, y) in enumerate(coords):
                    sx, sy = _to_svg_xy(x, y)
                    cmd = "M" if i == 0 else "L"
                    d_cmds.append(f"{cmd}{sx:.2f},{sy:.2f}")
                d_cmds.append("Z")
                d = " ".join(d_cmds)
                parts.append(
                    f"<path d='{d}' stroke='{stroke}' stroke-width='1.5' fill='{fill}' />"
                )
            return "\n".join(parts)

        lotes_paths = []
        vias_paths = []
        for f in lotes_fc.get("features", []):
            g = shp_shape(f["geometry"])
            lotes_paths.append(
                _poly_to_path(g, stroke="#f59e0b", fill="rgba(255,213,79,0.35)")
            )
        for f in vias_area_fc.get("features", []):
            g = shp_shape(f["geometry"])
            vias_paths.append(
                _poly_to_path(g, stroke="#9ca3af", fill="rgba(156,163,175,0.8)")
            )

        svg = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1000 1000">
<g id="vias">
{''.join(vias_paths)}
</g>
<g id="lotes">
{''.join(lotes_paths)}
</g>
</svg>"""

        return Response({"svg": svg}, status=status.HTTP_200_OK)
