# restricoes/admin.py
from django.contrib import admin
from .models import (
    Restricoes,
    AreaVerdeV,
    CorteAreaVerdeV,
    RuaV,
    MargemRioV,
    MargemLTV,
    MargemFerroviaV,
    ManualRestricaoV,
)


@admin.register(Restricoes)
class RestricoesAdmin(admin.ModelAdmin):
    list_display = [
        "id", "project", "version", "label", "notes",
        "percent_permitido", "corte_pct_cache", "source",
        "is_active", "created_at", "created_by",
    ]
    list_filter = ["project", "is_active", "source", "created_at"]
    search_fields = ["label", "notes"]
    date_hierarchy = "created_at"


@admin.register(AreaVerdeV)
class AreaVerdeVAdmin(admin.ModelAdmin):
    list_display = ["id", "restricoes", "geom"]
    list_filter = ["restricoes"]


@admin.register(CorteAreaVerdeV)
class CorteAreaVerdeVAdmin(admin.ModelAdmin):
    list_display = ["id", "restricoes", "geom"]
    list_filter = ["restricoes"]


@admin.register(RuaV)
class RuaVAdmin(admin.ModelAdmin):
    list_display = ["id", "restricoes", "eixo", "largura_m", "mask"]
    list_filter = ["restricoes"]
    search_fields = ["id"]


@admin.register(MargemRioV)
class MargemRioVAdmin(admin.ModelAdmin):
    list_display = ["id", "restricoes", "centerline", "margem_m", "faixa"]
    list_filter = ["restricoes"]


@admin.register(MargemLTV)
class MargemLTVAdmin(admin.ModelAdmin):
    list_display = ["id", "restricoes", "centerline", "margem_m", "faixa"]
    list_filter = ["restricoes"]


@admin.register(MargemFerroviaV)
class MargemFerroviaVAdmin(admin.ModelAdmin):
    list_display = ["id", "restricoes", "centerline", "margem_m", "faixa"]
    list_filter = ["restricoes"]

@admin.register(ManualRestricaoV)
class MargemFerroviaVAdmin(admin.ModelAdmin):
    list_display = ["id", "restricoes"]
    list_filter = ["restricoes"]
