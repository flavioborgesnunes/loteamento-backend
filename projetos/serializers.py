from __future__ import annotations

import json

from django.contrib.auth import get_user_model
from django.contrib.gis.geos import GEOSGeometry, MultiPolygon
from rest_framework import serializers

from .models import Project

User = get_user_model()


# projetos/serializers.py


class ProjectSerializer(serializers.ModelSerializer):
    owner = serializers.PrimaryKeyRelatedField(read_only=True)
    dono = serializers.PrimaryKeyRelatedField(read_only=True)

    owner_nome = serializers.SerializerMethodField()
    dono_nome = serializers.SerializerMethodField()
    owner_email = serializers.SerializerMethodField()
    dono_email = serializers.SerializerMethodField()

    def get_owner_nome(self, obj):
        u = getattr(obj, "owner", None)
        if not u:
            return None
        # usa o método que já faz fallback para nome completo ou email
        return u.get_full_name()

    def get_dono_nome(self, obj):
        u = getattr(obj, "dono", None)
        if not u:
            return None
        return u.get_full_name()

    def get_owner_email(self, obj):
        u = getattr(obj, "owner", None)
        return getattr(u, "email", None) if u else None

    def get_dono_email(self, obj):
        u = getattr(obj, "dono", None)
        return getattr(u, "email", None) if u else None

    class Meta:
        model = Project
        fields = [
            "id", "name", "description", "uf", "municipio",
            "owner", "owner_nome", "owner_email",
            "dono",  "dono_nome",  "dono_email",
            "created_at", "updated_at",
            "aoi_geom", "layer_flags",
        ]
        read_only_fields = ["id", "owner", "dono", "created_at", "updated_at"]


class ProjectUpsertExportSerializer(serializers.Serializer):
    """
    Export + persistência de overlays:
      - Se 'project_id' vier, usa o projeto existente.
      - Caso contrário, faz UPSERT por (dono, project_name):
          * se existir, atualiza;
          * se não, cria.
    """
    # Identificação do projeto
    project_id = serializers.IntegerField(required=False, allow_null=True)
    project_name = serializers.CharField(
        required=False, allow_blank=True, default="")
    project_description = serializers.CharField(
        required=False, allow_blank=True, default="")
    uf = serializers.CharField(required=False, allow_blank=True, default="")
    municipio = serializers.CharField(
        required=False, allow_blank=True, default="")

    # Dados do mapa
    aoi = serializers.JSONField()
    layers = serializers.JSONField(required=False, default=dict)
    simplify = serializers.JSONField(required=False, default=dict)
    overlays_raw = serializers.JSONField(required=False, default=dict)
    overlays = serializers.JSONField(required=False, default=dict)

    # Opções
    format = serializers.ChoiceField(
        choices=["kml", "kmz"], required=False, default="kmz")
    replace_overlays = serializers.BooleanField(required=False, default=False)

    # --- validações geométricas ---
    def validate_aoi(self, value):
        # Aceita dict GeoJSON ou string; devolve GEOS MP (SRID 4326)
        try:
            g = GEOSGeometry(json.dumps(value)) if isinstance(
                value, dict) else GEOSGeometry(str(value))
        except Exception:
            raise serializers.ValidationError("AOI inválida.")
        if g.srid in (None, 0):
            g.srid = 4326
        elif g.srid != 4326:
            try:
                g.transform(4326)
            except Exception:
                pass
        if not g.valid:
            try:
                g = g.buffer(0)
            except Exception:
                pass
        if g.geom_type == "Polygon":
            g = MultiPolygon([g], srid=4326)
        if g.geom_type != "MultiPolygon":
            raise serializers.ValidationError(
                "AOI deve ser Polygon/MultiPolygon.")
        return g

    def validate_uf(self, v):
        v = (v or "").upper().strip()
        return v[:2] or ""

    def validate(self, attrs):
        # Se não vier project_id, exigimos um project_name para o UPSERT
        if not attrs.get("project_id") and not (attrs.get("project_name") or "").strip():
            raise serializers.ValidationError(
                {"project_name": "Obrigatório quando 'project_id' não for enviado."})
        return attrs
