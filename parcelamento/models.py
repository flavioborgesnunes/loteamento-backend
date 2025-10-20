from django.conf import settings
from django.contrib.gis.db import models as gis
from django.db import models
from django.utils import timezone

# SRIDs
SRID_WGS84 = 4326


class ParcelamentoPlano(models.Model):
    project = models.ForeignKey(
        "projetos.Project", on_delete=models.CASCADE, related_name="parcelamento_planos")
    nome = models.CharField(max_length=160, default="Plano padrão")
    # draft|locked|archived
    status = models.CharField(max_length=20, default="draft")

    # parâmetros padrão
    frente_min_m = models.DecimalField(
        max_digits=8, decimal_places=2, default=10)
    prof_min_m = models.DecimalField(
        max_digits=8, decimal_places=2, default=25)
    larg_rua_vert_m = models.DecimalField(
        max_digits=8, decimal_places=2, default=12)
    larg_rua_horiz_m = models.DecimalField(
        max_digits=8, decimal_places=2, default=12)
    compr_max_quarteirao_m = models.DecimalField(
        max_digits=8, decimal_places=2, default=200)
    orientacao_graus = models.DecimalField(
        max_digits=6, decimal_places=2, null=True, blank=True)  # opcional
    srid_calc = models.IntegerField(default=3857)

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.project_id} - {self.nome}"


class ParcelamentoVersao(models.Model):
    plano = models.ForeignKey(
        ParcelamentoPlano, on_delete=models.CASCADE, related_name="versoes")
    is_oficial = models.BooleanField(default=False)
    nota = models.TextField(blank=True, default="")
    # snapshot de parâmetros
    frente_min_m = models.DecimalField(max_digits=8, decimal_places=2)
    prof_min_m = models.DecimalField(max_digits=8, decimal_places=2)
    larg_rua_vert_m = models.DecimalField(max_digits=8, decimal_places=2)
    larg_rua_horiz_m = models.DecimalField(max_digits=8, decimal_places=2)
    compr_max_quarteirao_m = models.DecimalField(
        max_digits=8, decimal_places=2)
    orientacao_graus = models.DecimalField(
        max_digits=6, decimal_places=2, null=True, blank=True)
    srid_calc = models.IntegerField(default=3857)
    created_at = models.DateTimeField(default=timezone.now)


class Via(models.Model):
    TIPOS = (("vertical", "vertical"), ("horizontal", "horizontal"),
             ("travessa", "travessa"), ("acesso", "acesso"))
    versao = models.ForeignKey(
        ParcelamentoVersao, on_delete=models.CASCADE, related_name="vias")
    geom = gis.LineStringField(srid=SRID_WGS84)
    largura_m = models.DecimalField(max_digits=8, decimal_places=2, default=12)
    tipo = models.CharField(max_length=20, choices=TIPOS, default="vertical")
    nome = models.CharField(max_length=80, blank=True, default="")


class Quarteirao(models.Model):
    versao = models.ForeignKey(
        ParcelamentoVersao, on_delete=models.CASCADE, related_name="quarteiroes")
    geom = gis.MultiPolygonField(srid=SRID_WGS84)


class Lote(models.Model):
    versao = models.ForeignKey(
        ParcelamentoVersao, on_delete=models.CASCADE, related_name="lotes")
    geom = gis.MultiPolygonField(srid=SRID_WGS84)
    area_m2 = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    frente_m = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    prof_media_m = models.DecimalField(
        max_digits=10, decimal_places=2, default=0)
    orientacao_graus = models.DecimalField(
        max_digits=6, decimal_places=2, null=True, blank=True)
    score_qualidade = models.DecimalField(
        max_digits=6, decimal_places=2, default=0)

    # snapshot de regras usadas
    frente_min_m = models.DecimalField(max_digits=8, decimal_places=2)
    prof_min_m = models.DecimalField(max_digits=8, decimal_places=2)


class Calcada(models.Model):
    versao = models.ForeignKey(
        ParcelamentoVersao, on_delete=models.CASCADE, related_name="calcadas")
    geom = gis.MultiPolygonField(srid=SRID_WGS84)
    largura_m = models.DecimalField(
        max_digits=8, decimal_places=2, default=2.50)

    def __str__(self):
        return f"Calcada v{self.versao_id}"
