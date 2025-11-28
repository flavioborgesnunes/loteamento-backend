# restricoes/serializers.py
# restricoes/serializers.py
from rest_framework import serializers

from .models import Restricoes


class RestricoesSerializer(serializers.ModelSerializer):
    # flag leve dizendo se já existe geometria de área loteável
    has_area_loteavel = serializers.SerializerMethodField()

    # contadores anotados nas views
    areas_verdes_count = serializers.IntegerField(read_only=True)
    cortes_av_count = serializers.IntegerField(read_only=True)
    margens_rio_count = serializers.IntegerField(read_only=True)
    margens_lt_count = serializers.IntegerField(read_only=True)
    margens_ferrovia_count = serializers.IntegerField(read_only=True)
    ruas_count = serializers.IntegerField(read_only=True)

    # 🔹 novos campos para o card de restrições
    project_name = serializers.CharField(source="project.name", read_only=True)
    created_by_nome = serializers.SerializerMethodField()
    created_by_email = serializers.SerializerMethodField()

    class Meta:
        model = Restricoes
        fields = [
            "id",
            "project",
            "project_name",
            "version",
            "label",
            "notes",
            "created_at",
            "percent_permitido",
            "corte_pct_cache",
            "source",
            "is_active",

            # contadores (vêm por annotate nas views)
            "areas_verdes_count",
            "cortes_av_count",
            "margens_rio_count",
            "margens_lt_count",
            "margens_ferrovia_count",
            "ruas_count",

            # flag calculada aqui
            "has_area_loteavel",

            # campos de autor
            "created_by_nome",
            "created_by_email",
        ]

    def get_has_area_loteavel(self, obj):
        try:
            return bool(getattr(obj, "area_loteavel", None) and not obj.area_loteavel.empty)
        except Exception:
            return False

    def get_created_by_nome(self, obj):
        u = getattr(obj, "created_by", None)
        return getattr(u, "nome", None) if u else None

    def get_created_by_email(self, obj):
        u = getattr(obj, "created_by", None)
        return getattr(u, "email", None) if u else None
