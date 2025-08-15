from rest_framework import serializers


class AOISerializer(serializers.Serializer):
    # Espera um GeoJSON Polygon/MultiPolygon
    aoi = serializers.JSONField()
    aoi = serializers.JSONField()
