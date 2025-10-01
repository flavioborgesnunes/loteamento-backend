from __future__ import annotations
from rest_framework import serializers
from .models import Restricoes

class RestricoesSerializer(serializers.ModelSerializer):
    areas_verdes_count = serializers.IntegerField(read_only=True)
    cortes_av_count = serializers.IntegerField(read_only=True)
    margens_rio_count = serializers.IntegerField(read_only=True)
    margens_lt_count = serializers.IntegerField(read_only=True)
    margens_ferrovia_count = serializers.IntegerField(read_only=True)
    ruas_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Restricoes
        fields = [
            "id","project","version","label","notes","percent_permitido","corte_pct_cache",
            "source","is_active","created_at",
            "areas_verdes_count","cortes_av_count","margens_rio_count","margens_lt_count","margens_ferrovia_count","ruas_count",
        ]
        read_only_fields = ["version","created_at"]
