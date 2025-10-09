from django.contrib import admin
from .models import ParcelamentoPlano, ParcelamentoVersao, Via, Quarteirao, Lote

@admin.register(ParcelamentoPlano)
class PlanoAdmin(admin.ModelAdmin):
    list_display = ("id","project","nome","status","updated_at")

@admin.register(ParcelamentoVersao)
class VersaoAdmin(admin.ModelAdmin):
    list_display = ("id","plano","is_oficial","created_at")

@admin.register(Via)
class ViaAdmin(admin.ModelAdmin):
    list_display = ("id","versao","tipo","largura_m")

@admin.register(Quarteirao)
class QuarteiraoAdmin(admin.ModelAdmin):
    list_display = ("id","versao",)

@admin.register(Lote)
class LoteAdmin(admin.ModelAdmin):
    list_display = ("id","versao","area_m2","frente_m","prof_media_m","score_qualidade")
