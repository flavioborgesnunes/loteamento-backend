from rest_framework import serializers
from parcelamento.serializers import ParametrosSerializer, PreviewResponseSerializer


class SugerirParametrosRequestSerializer(serializers.Serializer):
    al_geom = serializers.JSONField(
        help_text="Área Loteável em GeoJSON (Polygon/MultiPolygon, WGS84)."
    )
    params_iniciais = ParametrosSerializer(required=False)
    restricoes_resumo = serializers.JSONField(required=False)
    preferencias_usuario = serializers.CharField(
        required=False, allow_blank=True, default=""
    )


class ElementoEspecialSerializer(serializers.Serializer):
    tipo = serializers.CharField()
    center_rel = serializers.ListField(
        child=serializers.FloatField(), min_length=2, max_length=2, required=False
    )
    raio_rel = serializers.FloatField(required=False)
    # você pode ir adicionando campos aqui depois


class SugerirParametrosResponseSerializer(serializers.Serializer):
    params_sugeridos = ParametrosSerializer()
    observacoes = serializers.CharField()
    elementos_especiais = ElementoEspecialSerializer(many=True, required=False)


class PreviewIaRequestSerializer(SugerirParametrosRequestSerializer):
    modo = serializers.ChoiceField(
        choices=("full",),
        required=False,
        default="full",
        help_text="Por enquanto só 'full' (IA + geração determinística).",
    )


class PreviewIaResponseSerializer(PreviewResponseSerializer):
    params_usados = ParametrosSerializer()
    ia_metadata = serializers.DictField()
