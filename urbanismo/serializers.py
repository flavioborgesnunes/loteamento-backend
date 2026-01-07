from rest_framework import serializers


class RoadsPreviewRequestSerializer(serializers.Serializer):
    al_geojson = serializers.JSONField()
    params = serializers.JSONField(required=False, default=dict)
    targets = serializers.JSONField(required=False, default=dict)
    srid_calc = serializers.IntegerField(required=False, default=3857)
    linha_base = serializers.JSONField(
        required=False, allow_null=True, default=None)
    restricoes_id = serializers.IntegerField(
        required=False, allow_null=True, default=None)


class RoadsPreviewResponseSerializer(serializers.Serializer):
    best = serializers.JSONField(allow_null=True)
    ranked = serializers.JSONField()
