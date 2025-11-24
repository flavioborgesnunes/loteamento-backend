# iaparcelamento/serializers.py

from parcelamento.serializers import (ParametrosSerializer,
                                      PreviewResponseSerializer)
from rest_framework import serializers


class SugerirParametrosRequestSerializer(serializers.Serializer):
    """
    Request para a IA sugerir parâmetros de parcelamento
    e elementos especiais (praça circular etc.).
    """
    al_geom = serializers.JSONField(
        help_text="Área Loteável em GeoJSON (Polygon/MultiPolygon, WGS84)."
    )
    params_iniciais = ParametrosSerializer(required=False)
    restricoes_resumo = serializers.JSONField(required=False)
    preferencias_usuario = serializers.CharField(
        required=False, allow_blank=True, default=""
    )


class ElementoEspecialSerializer(serializers.Serializer):
    """
    Elementos especiais que a IA pode sugerir dentro da AL:
    - tipo: 'praca_circular_pequena', 'rotatoria', 'equipamento' etc.
    - center_rel: coordenadas relativas (0-1) dentro da AL (opcional).
    - raio_rel: raio relativo (0-1) dentro da AL (opcional).
    """
    tipo = serializers.CharField()
    center_rel = serializers.ListField(
        child=serializers.FloatField(),
        min_length=2,
        max_length=2,
        required=False,
        help_text="Centro relativo (x, y) na AL, valores entre 0 e 1",
    )
    raio_rel = serializers.FloatField(
        required=False,
        help_text="Raio relativo (0-1) em relação à menor dimensão da AL",
    )
    # você pode ir adicionando campos aqui depois:
    # ex.: orientacao, ligado_a_via_stable_id, etc.


class SugerirParametrosResponseSerializer(serializers.Serializer):
    """
    Resposta da IA ao sugerir parâmetros de parcelamento.
    """
    params_sugeridos = ParametrosSerializer()
    observacoes = serializers.CharField()
    elementos_especiais = ElementoEspecialSerializer(many=True, required=False)


class PreviewIaRequestSerializer(SugerirParametrosRequestSerializer):
    """
    Request para gerar pré-visualização com IA + heurística.
    """
    modo = serializers.ChoiceField(
        choices=("full",),
        required=False,
        default="full",
        help_text="Por enquanto só 'full' (IA + geração determinística).",
    )


class PreviewIaResponseSerializer(PreviewResponseSerializer):
    """
    Resposta de preview com IA:
    - herda o PreviewResponseSerializer (vias, lotes, etc.)
    - adiciona params_usados e ia_metadata.
    """
    params_usados = ParametrosSerializer()
    ia_metadata = serializers.DictField()
