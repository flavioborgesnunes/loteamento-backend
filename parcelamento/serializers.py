from django.contrib.gis.geos import GEOSGeometry
from rest_framework import serializers

from .models import (Calcada, Lote, ParcelamentoPlano, ParcelamentoVersao,
                     Quarteirao, Via)


class ParametrosSerializer(serializers.Serializer):
    # obrigatórios (do plano)
    frente_min_m = serializers.FloatField()
    prof_min_m = serializers.FloatField()
    larg_rua_vert_m = serializers.FloatField()
    larg_rua_horiz_m = serializers.FloatField()
    compr_max_quarteirao_m = serializers.FloatField()
    orientacao_graus = serializers.FloatField(required=False, allow_null=True)
    srid_calc = serializers.IntegerField(default=3857)

    # opcionais usados no services.py
    has_ruas_mask_fc = serializers.BooleanField(required=False, default=False)
    has_ruas_eixo_fc = serializers.BooleanField(required=False, default=False)
    ruas_mask_fc = serializers.JSONField(required=False)
    ruas_eixo_fc = serializers.JSONField(required=False)
    guia_linha_fc = serializers.JSONField(required=False)
    dist_min_rua_quarteirao_m = serializers.FloatField(required=False)
    tolerancia_frac = serializers.FloatField(required=False)
    calcada_largura_m = serializers.FloatField(required=False, default=2.5)


# --- payloads auxiliares para o /recalcular ---
class RecalcularRequestSerializer(serializers.Serializer):
    lotes_fc = serializers.JSONField(required=False)
    vias_fc = serializers.JSONField(required=False)
    quarteiroes_fc = serializers.JSONField(required=False)
    calcadas_fc = serializers.JSONField(required=False)
    renumerar = serializers.BooleanField(required=False, default=True)


class PlanoSerializer(serializers.ModelSerializer):
    class Meta:
        model = ParcelamentoPlano
        fields = "__all__"


class VersaoSerializer(serializers.ModelSerializer):
    class Meta:
        model = ParcelamentoVersao
        fields = "__all__"


class ViaSerializer(serializers.ModelSerializer):
    geom = serializers.JSONField()

    class Meta:
        model = Via
        fields = ("id", "versao", "geom", "largura_m", "tipo", "nome")


class QuarteiraoSerializer(serializers.ModelSerializer):
    geom = serializers.JSONField()

    class Meta:
        model = Quarteirao
        fields = ("id", "versao", "geom")


class LoteSerializer(serializers.ModelSerializer):
    geom = serializers.JSONField()

    class Meta:
        model = Lote
        fields = ("id", "versao", "geom", "area_m2", "frente_m", "prof_media_m", "score_qualidade",
                  "frente_min_m", "prof_min_m")


class CalcadaSerializer(serializers.ModelSerializer):
    geom = serializers.JSONField()

    class Meta:
        model = Calcada
        fields = ("id", "versao", "geom", "largura_m")

# Payloads de API


class PreviewRequestSerializer(serializers.Serializer):
    al_geom = serializers.JSONField(
        help_text="Área Loteável em GeoJSON (Polygon/MultiPolygon)")
    params = ParametrosSerializer()
    # opcional: vias/lotes editados no front
    user_edits = serializers.DictField(required=False)


class PreviewResponseSerializer(serializers.Serializer):
    vias = serializers.JSONField()            # FC (linhas - eixos)
    # FC (polígonos - áreas cinza SEM calçada)
    vias_area = serializers.JSONField()
    quarteiroes = serializers.JSONField()     # FC (polígonos)
    lotes = serializers.JSONField()           # FC (polígonos com props)
    calcadas = serializers.JSONField()        # FC (polígonos - faixa exclusiva)
    metrics = serializers.DictField()


class MaterializarRequestSerializer(PreviewRequestSerializer):
    nota = serializers.CharField(required=False, allow_blank=True, default="")
    is_oficial = serializers.BooleanField(required=False, default=False)


class MaterializarRequestSerializer(PreviewRequestSerializer):
    nota = serializers.CharField(required=False, allow_blank=True, default="")
    is_oficial = serializers.BooleanField(required=False, default=False)
