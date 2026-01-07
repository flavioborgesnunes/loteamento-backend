import uuid

from django.conf import settings
from django.contrib.gis.db import models as gis
from django.db import models
from django.db.models import Q
from django.utils import timezone

# SRIDs
SRID_WGS84 = 4326


# ------------------------------------------------------------------------------
# Base para componentes editáveis / IA
# ------------------------------------------------------------------------------

ORIGEM_COMPONENTE = (
    ("heuristica", "Gerado por heurística (backend)"),
    ("ia", "Gerado por IA"),
    ("manual", "Desenhado / editado manualmente"),
    ("importado", "Importado de fonte externa"),
)


class EditableComponent(models.Model):
    """
    Campos comuns para tudo que a IA/usuário pode criar/editar.

    - stable_id: referência estável para a IA (não muda se o PK mudar).
    - origem: de onde veio esse componente (heurística, IA, manual).
    - created_by_ia: se foi criado diretamente por comando da IA.
    - edited_manualmente: já foi alterado manualmente depois de criado.
    - locked: não deve ser alterado em operações automáticas.
    - ia_metadata: espaço livre p/ comandos, contexto, histórico etc.
    """

    stable_id = models.UUIDField(
        default=uuid.uuid4,
        editable=False,
        db_index=True,
        help_text="Identificador estável para referência pela IA",
    )

    origem = models.CharField(
        max_length=20,
        choices=ORIGEM_COMPONENTE,
        default="heuristica",
        help_text="Origem do componente (heurística, IA, manual...)",
    )

    created_by_ia = models.BooleanField(
        default=False,
        help_text="Marcado como verdadeiro quando criado a partir de comando da IA",
    )

    edited_manualmente = models.BooleanField(
        default=False,
        help_text="Verdadeiro se o usuário já editou este componente manualmente",
    )

    locked = models.BooleanField(
        default=False,
        help_text="Se verdadeiro, o componente não é alterado por operações automáticas",
    )

    ia_metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text="Metadata livre para IA (comandos, contexto, tags, etc.)",
    )

    class Meta:
        abstract = True


# ------------------------------------------------------------------------------
# Plano e versões (versionamento)
# ------------------------------------------------------------------------------


class ParcelamentoPlano(EditableComponent):
    project = models.ForeignKey(
        "projetos.Project",
        on_delete=models.CASCADE,
        related_name="parcelamento_planos",
    )
    nome = models.CharField(max_length=160, default="Plano padrão")
    # draft|locked|archived
    status = models.CharField(max_length=20, default="draft")

    # parâmetros padrão
    frente_min_m = models.DecimalField(
        max_digits=8, decimal_places=2, default=10
    )
    prof_min_m = models.DecimalField(
        max_digits=8, decimal_places=2, default=25
    )
    larg_rua_vert_m = models.DecimalField(
        max_digits=8, decimal_places=2, default=12
    )
    larg_rua_horiz_m = models.DecimalField(
        max_digits=8, decimal_places=2, default=12
    )
    compr_max_quarteirao_m = models.DecimalField(
        max_digits=8, decimal_places=2, default=200
    )
    orientacao_graus = models.DecimalField(
        max_digits=6, decimal_places=2, null=True, blank=True
    )  # opcional
    srid_calc = models.IntegerField(default=3857)

    # Metadata temporal
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    # Campo extra p/ IA guardar prompt-resumo / observações do plano
    ia_resumo = models.TextField(
        blank=True,
        default="",
        help_text="Resumo em linguagem natural das intenções do plano para a IA",
    )

    def __str__(self):
        return f"{self.project_id} - {self.nome}"

    DIRECAO_CHOICES = [
        ("auto_maior_lado", "Automático (maior lado da área loteável)"),
        ("usar_orientacao_graus", "Usar orientação fixa em graus"),
    ]
    LADO_REF_CHOICES = [
        ("topo", "Lado superior da área loteável"),
        ("base", "Lado inferior"),
        ("esquerda", "Lado esquerdo"),
        ("direita", "Lado direito"),
    ]

    direcao_quarteiroes = models.CharField(
        max_length=32,
        choices=DIRECAO_CHOICES,
        default="auto_maior_lado",
        help_text=(
            "Define como orientar quarteirões: automático (maior lado da AL) "
            "ou usar um ângulo fixo (orientacao_graus)."
        ),
    )

    lado_ref_quarteiroes = models.CharField(
        max_length=16,
        choices=LADO_REF_CHOICES,
        default="topo",
        help_text=(
            "Lado de referência da AL para calcular o ângulo. "
            "Por padrão, segue o ângulo da parte superior ('topo')."
        ),
    )


class ParcelamentoVersao(EditableComponent):
    # ✅ Sempre ligado a um Project
    project = models.ForeignKey(
        "projetos.Project",
        on_delete=models.CASCADE,
        related_name="parcelamento_versoes",
    )

    # ✅ Âncora: qual versão de restrições foi usada como base do parcelamento
    # (temporariamente nullable para migração segura; depois vamos travar)
    restricoes = models.ForeignKey(
        "restricoes.Restricoes",
        on_delete=models.SET_NULL,  # depois vira PROTECT
        null=True,
        blank=True,
        related_name="parcelamentos",
    )

    # ✅ Snapshot da base usada (robustez: reproduz mesmo se restrições mudarem depois)
    area_loteavel_snapshot = gis.MultiPolygonField(
        srid=4674, null=True, blank=True)

    # ✅ Número sequencial por projeto (diferencia versões do parcelamento sem confundir com restrições)
    numero = models.PositiveIntegerField(null=True, blank=True)

    # Plano vira opcional (template/preset)
    plano = models.ForeignKey(
        "parcelamento.ParcelamentoPlano",
        on_delete=models.SET_NULL,
        related_name="versoes",
        null=True,
        blank=True,
    )

    # --- Gestão de versões / opções ---
    STATUS_CHOICES = (
        ("draft", "draft"),
        ("candidate", "candidate"),
        ("final", "final"),
    )
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default="draft")

    parent = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="children",
        help_text="Versão pai (para opções A/B/C).",
    )
    label = models.CharField(max_length=120, blank=True, default="")

    # --- Referência geométrica do parcelamento ---
    linha_base = gis.LineStringField(srid=SRID_WGS84, null=True, blank=True)

    # “topo+esquerda” => Norte/Oeste
    preferencia_cardinal = models.CharField(
        max_length=4,
        default="NW",
        help_text="Preferência cardinal, ex: 'NW' (Norte/Oeste).",
    )

    # --- Snapshot de parâmetros (inclui calçadas e fileiras) ---
    fileiras = models.SmallIntegerField(default=1)  # 1 ou 2
    calcada_largura_m = models.DecimalField(
        max_digits=8, decimal_places=2, default=2.50)
    calcada_encosta_aoi = models.BooleanField(default=False)

    is_oficial = models.BooleanField(default=False)
    nota = models.TextField(blank=True, default="")

    # parâmetros snapshot
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

    # --- Estado incremental ---
    step_index = models.IntegerField(default=0)
    cursor_state = models.JSONField(default=dict, blank=True)
    debug_last = models.JSONField(default=dict, blank=True)

    ia_comando_gerador = models.TextField(
        blank=True,
        default="",
        help_text="Comando/descrição usado para gerar esta versão",
    )

    class Meta:
        constraints = [
            # Unicidade do número dentro do projeto (mas permite NULL durante migração)
            models.UniqueConstraint(
                fields=["project", "numero"],
                condition=Q(numero__isnull=False),
                name="uniq_parcelamento_versao_numero_por_project",
            ),
        ]

    def __str__(self):
        base = self.label or (
            f"Parcelamento #{self.numero}" if self.numero else f"Versão {self.pk}")
        return f"{base} ({self.status})"

# ------------------------------------------------------------------------------
# Vias
# ------------------------------------------------------------------------------


class Via(EditableComponent):
    TIPOS = (
        ("vertical", "vertical"),
        ("horizontal", "horizontal"),
        ("travessa", "travessa"),
        ("acesso", "acesso"),
    )

    CATEGORIAS = (
        ("local", "Local"),
        ("coletora", "Coletora"),
        ("arterial", "Arterial"),
        ("expressa", "Expressa"),
    )

    versao = models.ForeignKey(
        ParcelamentoVersao,
        on_delete=models.CASCADE,
        related_name="vias",
    )
    geom = gis.LineStringField(srid=SRID_WGS84)

    largura_m = models.DecimalField(
        max_digits=8, decimal_places=2, default=12
    )

    # tipo geométrico / orientação (mantido para compatibilidade com o backend atual)
    tipo = models.CharField(max_length=20, choices=TIPOS, default="vertical")

    # categoria hierárquica da via (para IA e análise urbana)
    categoria = models.CharField(
        max_length=20, choices=CATEGORIAS, default="local"
    )

    nome = models.CharField(max_length=80, blank=True, default="")

    # indica se este trecho de via funciona como ponte (atravessa rio, córrego etc.)
    is_ponte = models.BooleanField(default=False)

    # opcional: sobre o que é a ponte (ex.: "rio", "córrego", "vala", "ferrovia")
    ponte_sobre = models.CharField(max_length=80, blank=True, default="")

    def __str__(self):
        base = self.nome or f"Via {self.id}"
        if self.is_ponte:
            return f"{base} (ponte)"
        return base


# ------------------------------------------------------------------------------
# Quarteirões
# ------------------------------------------------------------------------------


class Quarteirao(EditableComponent):
    versao = models.ForeignKey(
        ParcelamentoVersao,
        on_delete=models.CASCADE,
        related_name="quarteiroes",
    )
    geom = gis.MultiPolygonField(srid=SRID_WGS84)

    def __str__(self):
        return f"Quarteirão {self.id} (versão {self.versao_id})"


# ------------------------------------------------------------------------------
# Lotes
# ------------------------------------------------------------------------------


class Lote(EditableComponent):
    versao = models.ForeignKey(
        ParcelamentoVersao,
        on_delete=models.CASCADE,
        related_name="lotes",
    )
    geom = gis.MultiPolygonField(srid=SRID_WGS84)
    area_m2 = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    frente_m = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    prof_media_m = models.DecimalField(
        max_digits=10, decimal_places=2, default=0
    )
    orientacao_graus = models.DecimalField(
        max_digits=6, decimal_places=2, null=True, blank=True
    )
    score_qualidade = models.DecimalField(
        max_digits=6, decimal_places=2, default=0
    )

    # snapshot de regras usadas
    frente_min_m = models.DecimalField(max_digits=8, decimal_places=2)
    prof_min_m = models.DecimalField(max_digits=8, decimal_places=2)

    # numeração sequencial do lote na versão (ex.: 35, 36...)
    numero = models.PositiveIntegerField(default=0)

    # identificação de quadra (opcional, para futuros fluxos)
    quadra = models.CharField(max_length=40, blank=True, default="")

    def __str__(self):
        if self.numero:
            return f"Lote {self.numero} (versão {self.versao_id})"
        return f"Lote {self.id} (versão {self.versao_id})"


# ------------------------------------------------------------------------------
# Calçadas
# ------------------------------------------------------------------------------


class Calcada(EditableComponent):
    versao = models.ForeignKey(
        ParcelamentoVersao,
        on_delete=models.CASCADE,
        related_name="calcadas",
    )
    geom = gis.MultiPolygonField(srid=SRID_WGS84)
    largura_m = models.DecimalField(
        max_digits=8, decimal_places=2, default=2.50
    )

    def __str__(self):
        return f"Calcada v{self.versao_id}"


# ------------------------------------------------------------------------------
# Áreas públicas
# ------------------------------------------------------------------------------


class AreaPublica(EditableComponent):
    TIPOS = (
        ("praca", "Praça"),
        ("esporte", "Esporte / Lazer"),
        ("institucional", "Institucional"),
        ("equipamento", "Equipamento público"),
        ("outro", "Outro"),
    )

    versao = models.ForeignKey(
        ParcelamentoVersao,
        on_delete=models.CASCADE,
        related_name="areas_publicas",
    )
    geom = gis.MultiPolygonField(srid=SRID_WGS84)

    tipo = models.CharField(max_length=40, choices=TIPOS, default="praca")
    nome = models.CharField(max_length=160, blank=True, default="")
    descricao = models.TextField(blank=True, default="")

    def __str__(self):
        base = self.nome or f"Área pública {self.id}"
        return f"{base} ({self.get_tipo_display()})"
