from rest_framework import serializers
from django.contrib.gis.geos import GEOSGeometry
from .models import ParcelamentoPlano, ParcelamentoVersao, Via, Quarteirao, Lote

class ParametrosSerializer(serializers.Serializer):
    frente_min_m = serializers.FloatField()
    prof_min_m = serializers.FloatField()
    larg_rua_vert_m = serializers.FloatField()
    larg_rua_horiz_m = serializers.FloatField()
    compr_max_quarteirao_m = serializers.FloatField()
    orientacao_graus = serializers.FloatField(required=False, allow_null=True)
    srid_calc = serializers.IntegerField(default=3857)

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
        fields = ("id","versao","geom","largura_m","tipo","nome")

class QuarteiraoSerializer(serializers.ModelSerializer):
    geom = serializers.JSONField()
    class Meta:
        model = Quarteirao
        fields = ("id","versao","geom")

class LoteSerializer(serializers.ModelSerializer):
    geom = serializers.JSONField()
    class Meta:
        model = Lote
        fields = ("id","versao","geom","area_m2","frente_m","prof_media_m","score_qualidade",
                  "frente_min_m","prof_min_m")

# Payloads de API
class PreviewRequestSerializer(serializers.Serializer):
    al_geom = serializers.JSONField(help_text="Área Loteável em GeoJSON (Polygon/MultiPolygon)")
    params = ParametrosSerializer()
    user_edits = serializers.DictField(required=False)  # opcional: vias/lotes editados no front

class PreviewResponseSerializer(serializers.Serializer):
    vias = serializers.JSONField()          # FC
    quarteiroes = serializers.JSONField()   # FC
    lotes = serializers.JSONField()         # FC
    metrics = serializers.DictField()

class MaterializarRequestSerializer(PreviewRequestSerializer):
    nota = serializers.CharField(required=False, allow_blank=True, default="")
    is_oficial = serializers.BooleanField(required=False, default=False)
