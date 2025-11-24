from __future__ import annotations

import json
import logging
from typing import Any, Dict

from django.conf import settings
from django.shortcuts import get_object_or_404
from openai import OpenAI
from parcelamento.models import ParcelamentoPlano
from parcelamento.services import compute_preview, compute_preview_com_comandos
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from shapely.geometry import shape

from .openai_client import get_default_model_name
from .rag import load_rag_context
from .serializers import (PreviewIaRequestSerializer,
                          PreviewIaResponseSerializer,
                          SugerirParametrosRequestSerializer,
                          SugerirParametrosResponseSerializer)

logger = logging.getLogger(__name__)

IA_PARCELAMENTO_SYSTEM_PROMPT = """
Você é um assistente de planejamento de parcelamento urbano.

SEMPRE produza uma resposta em JSON ESTRITO, sem comentários, sem texto antes ou depois, exatamente neste formato:

{
  "versao_esquema": "1.0",
  "parametros": { ... },
  "comandos": [ ... ],
  "observacoes_urbanisticas": "..."
}

NUNCA inclua explicações em linguagem natural fora do JSON.
NUNCA inclua campos além dos especificados abaixo, exceto dentro de "parametros", onde você pode adicionar novos campos numéricos se forem úteis.

------------------------------
CONTRATO DO CAMPO "parametros"
------------------------------

O campo "parametros" deve conter os parâmetros numéricos que o backend usará para gerar o parcelamento automático. Use SEMPRE um objeto JSON com chaves em snake_case. Exemplos de campos:

{
  "frente_min_m": number,
  "prof_min_m": number,
  "largura_ruas_verticais_m": number,
  "largura_ruas_horizontais_m": number,
  "comprimento_max_quarteirao_m": number,
  "largura_calcada_m": number,
  "orientacao_graus": number | null
}

Regras:
- Use valores em METROS.
- Se o usuário falar "lotes 10x25", isso significa frente_min_m = 10, prof_min_m = 25.
- Se o usuário falar "ruas de 12 metros", isso vale para ruas verticais e horizontais, a não ser que ele diferencie.
- Se não houver orientação específica, use "orientacao_graus": null.

Você pode adicionar outros parâmetros numéricos se forem coerentes, mas mantenha SEMPRE esse objeto como um simples JSON com chaves numéricas.

-----------------------------
CONTRATO DO CAMPO "comandos"
-----------------------------

O campo "comandos" é SEMPRE uma lista (array) de comandos geométricos.

Cada comando é um objeto com o formato:

{
  "id": "string",
  "acao": "string",
  "momento": "string",
  "localizacao": {
    "estrategia": "string",
    "params": { ... }   // opcional
  },
  "tamanho": {
    "tipo": "string",
    "valor": number
  },
  "forma": {
    "tipo": "string"
  },
  "restricoes": {
    // opcional, pode estar vazio
  },
  "descricao": "string opcional"
}

Campos obrigatórios por enquanto:
- "id": um identificador de comando, por exemplo "cmd_praca_central_1".
- "acao": para a primeira versão, use "criar_praca" quando o usuário pedir uma praça.
- "momento": "pre" ou "pos".
  - Use "pre" quando a ação deve acontecer ANTES do parcelamento (ex.: abrir um buraco na área loteável para uma praça central).
  - Use "pos" quando a ação deve acontecer DEPOIS (ex.: transformar lotes existentes em praça).
- "localizacao": SEMPRE um objeto com:
  - "estrategia": string que define a lógica de localização.
  - "params": objeto opcional com parâmetros extras.

Para a PRIMEIRA VERSÃO (v1), implemente APENAS este comando:

1) Criar praça centralizada:
- Use:
  - "acao": "criar_praca"
  - "momento": "pre"
  - "localizacao.estrategia": "centro_da_area_loteavel"
- Exemplo de comando completo:

{
  "id": "cmd_praca_central",
  "acao": "criar_praca",
  "momento": "pre",
  "localizacao": {
    "estrategia": "centro_da_area_loteavel"
  },
  "tamanho": {
    "tipo": "raio_relativo",
    "valor": 0.15
  },
  "forma": {
    "tipo": "circulo"
  },
  "restricoes": {
    "max_fracao_area_loteavel": 0.2
  },
  "descricao": "Criar uma praça circular pequena no centro da área loteável."
}

Regras para "tamanho":
- "tamanho" é um objeto com:
  - "tipo": um dos:
    - "raio_relativo"  (fração do raio base da área)
    - "raio_absoluto_m" (raio em metros)
    - "area_alvo_m2"    (área alvo em m²)
  - "valor": número
- Para a praça centralizada, prefira:
  - "tipo": "raio_relativo"
  - "valor": um número entre 0.05 e 0.3, dependendo do pedido do usuário (pequena, média, grande).

Regras para "forma":
- "forma" deve ter pelo menos:
  {
    "tipo": "circulo"
  }
- Se o usuário não especificar, use sempre "circulo".

Regras para "restricoes":
- Pode estar vazio ou ausente.
- Quando fizer sentido, use:
  {
    "max_fracao_area_loteavel": number
  }

Você pode adicionar outros comandos no futuro (como "unir_lotes"), mas por enquanto PRIORIZE:
- "acao": "criar_praca"
- "localizacao.estrategia": "centro_da_area_loteavel" quando o usuário pedir uma praça "central", "no meio", "praça centralizada" etc.

Se o usuário NÃO pedir praça nem área pública, você pode retornar "comandos": [] (lista vazia).


---------------------------------------
CONTRATO DO CAMPO "observacoes_urbanisticas"
---------------------------------------

- "observacoes_urbanisticas" é SEMPRE uma string.
- Use linguagem natural, explicando de forma clara as decisões tomadas:
  - porque escolheu frente/profundidade,
  - porque escolheu larguras de vias,
  - como a praça foi planejada (quando existir),
  - outras recomendações urbanísticas.
- Não coloque JSON aqui, é texto livre.

-------------------------
FORMATO FINAL OBRIGATÓRIO
-------------------------

Resuma tudo isso gerando SEMPRE um JSON VÁLIDO com esta estrutura:

{
  "versao_esquema": "1.0",
  "parametros": {
    ...parametros numericos...
  },
  "comandos": [
    ...lista de comandos geométricos conforme especificado...
  ],
  "observacoes_urbanisticas": "texto explicando as decisões urbanísticas"
}

NÃO use comentários.
NÃO use vírgula sobrando no final.
NÃO escreva nada fora do JSON.
"""


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
            params_iniciais.get("compr_max_quarteirao_m",
                                plano.compr_max_quarteirao_m)
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
        "calcada_largura_m": float(params_iniciais.get("calcada_largura_m", 2.5)),
    }
    return base


def _normalize_parametros_ia(parametros: Dict[str, Any], base_params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Converte os nomes de campos vindos da IA (parametros) para
    os nomes esperados pelo backend (compute_preview), usando base_params
    como default.

    - parametros: dict vindo da IA ("parametros")
    - base_params: dict já montado pelo _merge_plan_params (com defaults do plano)

    Retorna um dict pronto para passar ao compute_preview / compute_preview_com_comandos.
    """
    # Começa dos params base (já com defaults do plano)
    params = base_params.copy()

    p = parametros or {}

    # 1) Sobrescrever diretamente campos que já estão no padrão do backend
    for key in [
        "frente_min_m",
        "prof_min_m",
        "larg_rua_vert_m",
        "larg_rua_horiz_m",
        "compr_max_quarteirao_m",
        "srid_calc",
        "orientacao_graus",
        "has_ruas_mask_fc",
        "has_ruas_eixo_fc",
        "ruas_mask_fc",
        "ruas_eixo_fc",
        "guia_linha_fc",
        "dist_min_rua_quarteirao_m",
        "tolerancia_frac",
        "calcada_largura_m",
    ]:
        if key in p and p[key] is not None:
            params[key] = p[key]

    # 2) Sinônimos usados no prompt da IA → nomes do backend

    # largura_ruas_verticais_m → larg_rua_vert_m
    if "largura_ruas_verticais_m" in p and "larg_rua_vert_m" not in p:
        params["larg_rua_vert_m"] = p["largura_ruas_verticais_m"]

    # largura_ruas_horizontais_m → larg_rua_horiz_m
    if "largura_ruas_horizontais_m" in p and "larg_rua_horiz_m" not in p:
        params["larg_rua_horiz_m"] = p["largura_ruas_horizontais_m"]

    # comprimento_max_quarteirao_m → compr_max_quarteirao_m
    if "comprimento_max_quarteirao_m" in p and "compr_max_quarteirao_m" not in p:
        params["compr_max_quarteirao_m"] = p["comprimento_max_quarteirao_m"]

    # largura_calcada_m → calcada_largura_m
    if "largura_calcada_m" in p and "calcada_largura_m" not in p:
        params["calcada_largura_m"] = p["largura_calcada_m"]

    return params


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
    Chama a IA para sugerir parâmetros de parcelamento E comandos geométricos.

    Espera resposta em JSON no formato:

    {
      "versao_esquema": "1.0",
      "parametros": { ... },
      "comandos": [ ... ],
      "observacoes_urbanisticas": "..."
    }
    """

    model = getattr(settings, "OPENAI_PARCELAMENTO_MODEL",
                    None) or get_default_model_name()

    al_resumo = _summarize_al(al_geom)
    rag_ctx = load_rag_context()  # texto com normas/boas práticas

    # Prompt do usuário: contexto específico desse plano / chamada
    user_prompt = f"""
Contexto: você vai sugerir parâmetros de parcelamento urbano e, opcionalmente,
comandos geométricos (como criar praça) para um plano de loteamento.

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
{json.dumps(restricoes_resumo or {}, ensure_ascii=False, indent=2)}

-------------------------------
PREFERÊNCIAS DO USUÁRIO (texto livre):
\"\"\"{preferencias_usuario or ""}\"\"\"

Com base nessas informações, preencha os campos "parametros", "comandos"
e "observacoes_urbanisticas" seguindo EXATAMENTE o contrato descrito
na mensagem de sistema. Responda apenas com o JSON final.
"""

    logger.info(
        "[IA] Chamando modelo %s para sugerir parâmetros+comandos de parcelamento", model
    )

    # Usando o novo contrato via system + user
    resp = client.responses.create(
        model=model,
        input=[
            {"role": "system", "content": IA_PARCELAMENTO_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
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
    logger.info(
        "[IA] Texto bruto da resposta (primeiros 400 chars): %s", text[:400])

    if not text:
        raise ValueError(
            "Resposta vazia da IA para sugerir parâmetros+comandos")

    # ---- tentar converter para JSON ----
    try:
        data = json.loads(text)
        return data
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            snippet = text[start: end + 1]
            data = json.loads(snippet)
            return data
        logger.error(
            "[IA] Não foi possível parsear JSON da resposta: %s", text)
        raise


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

class SugerirParametrosView(APIView):
    """
    IA sugere parâmetros de parcelamento, sem gerar geometria.

    Endpoint: POST /api/ia-parcelamento/planos/<plano_id>/sugerir-parametros/
    """

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, plano_id: int, *args, **kwargs):
        plano = get_object_or_404(ParcelamentoPlano, pk=plano_id)

        # Normaliza aliases usados no front (compatibilidade)
        data_in = request.data.copy()
        if "al_geom" not in data_in:
            al_alt = data_in.get("alFeature") or data_in.get("al_geojson")
            if isinstance(al_alt, dict) and al_alt.get("type") == "Feature":
                al_alt = al_alt.get("geometry")
            data_in["al_geom"] = al_alt

        serializer = SugerirParametrosRequestSerializer(data=data_in)
        serializer.is_valid(raise_exception=True)
        v = serializer.validated_data

        al_geom = v.get("al_geom")
        params_iniciais = v.get("params_iniciais") or {}
        restricoes_resumo = v.get("restricoes_resumo") or {}
        preferencias_usuario = v.get("preferencias_usuario") or ""

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

        # Garante chaves mínimas
        versao_esquema = ia_out.get("versao_esquema")
        parametros = ia_out.get("parametros") or {}
        comandos = ia_out.get("comandos") or []
        observacoes_urbanisticas = ia_out.get("observacoes_urbanisticas") or ""

        # Mantém os nomes antigos para o front:
        params_sugeridos = parametros

        # elementos_especiais agora pode ser derivado dos comandos (descrição):
        elementos_especiais = [
            cmd.get("descricao")
            for cmd in comandos
            if isinstance(cmd, dict) and cmd.get("descricao")
        ]

        observacoes = observacoes_urbanisticas

        resp_data = {
            "params_sugeridos": params_sugeridos,
            "observacoes": observacoes,
            "elementos_especiais": elementos_especiais,
            # novos campos de debug/IA, se quiser usar no front depois:
            "ia_esquema": versao_esquema,
            "ia_comandos": comandos,
        }

        resp_data = {
            "params_sugeridos": params_sugeridos,
            "observacoes": observacoes,
            "elementos_especiais": elementos_especiais,
        }

        # Opcional: validar resposta no serializer de saída (ajuda a pegar erro cedo)
        out_ser = SugerirParametrosResponseSerializer(data=resp_data)
        # não vou quebrar a request se vier algo extra
        out_ser.is_valid(raise_exception=False)

        # Adiciona bloco de debug fora do schema oficial (útil pro front, se quiser)
        resp_data["debug"] = {
            "base_params": base_params,
            "al_resumo": _summarize_al(al_geom),
        }

        return Response(resp_data, status=status.HTTP_200_OK)


class PreviewIaView(APIView):
    """
    IA sugere parâmetros e já chama compute_preview_com_comandos,
    retornando a prévia completa + metadados da IA.
    """

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, plano_id: int, *args, **kwargs):
        plano = get_object_or_404(ParcelamentoPlano, pk=plano_id)

        # Normaliza aliases de entrada
        data_in = request.data.copy()
        if "al_geom" not in data_in:
            al_alt = data_in.get("alFeature") or data_in.get("al_geojson")
            if isinstance(al_alt, dict) and al_alt.get("type") == "Feature":
                al_alt = al_alt.get("geometry")
            data_in["al_geom"] = al_alt

        serializer = PreviewIaRequestSerializer(data=data_in)
        serializer.is_valid(raise_exception=True)
        v = serializer.validated_data

        al_geom = v.get("al_geom")
        params_iniciais = v.get("params_iniciais") or v.get("params") or {}
        restricoes_resumo = v.get("restricoes_resumo") or {}
        preferencias_usuario = v.get("preferencias_usuario") or ""

        if not al_geom:
            return Response(
                {"detail": "Campo 'al_geom' é obrigatório (GeoJSON Polygon/MultiPolygon)."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # base_params já vem com defaults do plano, com nomes esperados pelo backend
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
                    "detail": "Erro ao chamar o modelo de IA para sugerir parâmetros e comandos.",
                    "error": str(e),
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        # Novo contrato da IA
        versao_esquema = ia_out.get("versao_esquema")
        parametros_raw = ia_out.get("parametros") or {}
        comandos = ia_out.get("comandos") or []
        observacoes_urbanisticas = ia_out.get("observacoes_urbanisticas") or ""

        # Normaliza os parâmetros da IA para o formato que o backend espera
        params_final = _normalize_parametros_ia(parametros_raw, base_params)

        # Aplica comandos PRE (ex.: criar_praca) e depois gera o parcelamento
        preview = compute_preview_com_comandos(al_geom, params_final, comandos)

        # Deriva elementos_especiais das descrições dos comandos
        elementos_especiais = [
            cmd.get("descricao")
            for cmd in comandos
            if isinstance(cmd, dict) and cmd.get("descricao")
        ]

        resp_data = {
            **preview,
            "params_usados": params_final,
            "ia_metadata": {
                "observacoes": observacoes_urbanisticas,
                "elementos_especiais": elementos_especiais,
                "parametros": parametros_raw,
                "comandos": comandos,
                "versao_esquema": versao_esquema,
            },
        }

        # Opcional: validar só a casca do response com o serializer
        out_ser = PreviewIaResponseSerializer(data=resp_data)
        out_ser.is_valid(raise_exception=False)

        return Response(resp_data, status=status.HTTP_200_OK)


class SvgPreviewIaView(APIView):
    """
    Gera um SVG simples da prévia baseada na IA.
    (Usa compute_preview por baixo, depois transforma em SVG.)
    """

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, plano_id: int, *args, **kwargs):
        plano = get_object_or_404(ParcelamentoPlano, pk=plano_id)

        data_in = request.data.copy()
        if "al_geom" not in data_in:
            al_alt = data_in.get("alFeature") or data_in.get("al_geojson")
            if isinstance(al_alt, dict) and al_alt.get("type") == "Feature":
                al_alt = al_alt.get("geometry")
            data_in["al_geom"] = al_alt

        # Reaproveito o PreviewIaRequestSerializer só para validar entrada
        serializer = PreviewIaRequestSerializer(data=data_in)
        serializer.is_valid(raise_exception=True)
        v = serializer.validated_data

        al_geom = v.get("al_geom")
        params_iniciais = v.get("params_iniciais") or v.get("params") or {}
        restricoes_resumo = v.get("restricoes_resumo") or {}
        preferencias_usuario = v.get("preferencias_usuario") or ""

        if not al_geom:
            return Response(
                {
                    "svg": "<svg xmlns='http://www.w3.org/2000/svg'></svg>",
                    "detail": "Campo 'al_geom' é obrigatório (GeoJSON Polygon/MultiPolygon).",
                },
                status=status.HTTP_200_OK,
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

        parametros = ia_out.get("parametros") or {}
        params_final = parametros or base_params
        preview = compute_preview(al_geom, params_final)
        lotes_fc = preview["lotes"]
        vias_area_fc = preview["vias_area"]

        # --- converte FCs em um SVG bem simples em coordenadas WGS84 (lon/lat) ---
        from shapely.geometry import MultiPolygon, Polygon
        from shapely.geometry import shape as shp_shape

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
                _poly_to_path(g, stroke="#f59e0b",
                              fill="rgba(255,213,79,0.35)")
            )
        for f in vias_area_fc.get("features", []):
            g = shp_shape(f["geometry"])
            vias_paths.append(
                _poly_to_path(g, stroke="#9ca3af",
                              fill="rgba(156,163,175,0.8)")
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
