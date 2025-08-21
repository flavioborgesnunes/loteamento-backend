# rios/serializers.py
from rest_framework import serializers


class ExportMapaSerializer(serializers.Serializer):
    # Aceita GeoJSON Geometry/Feature/FeatureCollection ou WKT (POLYGON/MULTIPOLYGON)
    aoi = serializers.JSONField(required=True)

    # Quais camadas recortar (todas opcionais; se nenhuma vier true, retornamos apenas AOI?)
    layers = serializers.DictField(
        child=serializers.BooleanField(),
        required=False
    )

    # Parâmetros auxiliares
    uf = serializers.CharField(
        required=False, allow_blank=True, allow_null=True)

    # Tolerâncias de simplificação por camada (opcional)
    simplify = serializers.DictField(
        child=serializers.FloatField(),
        required=False
    )

    # Força KML puro (senão KMZ)
    format = serializers.ChoiceField(choices=["kml", "kmz"], required=False)
    overlays = serializers.JSONField(required=False)

    overlays_raw = serializers.JSONField(required=False)
