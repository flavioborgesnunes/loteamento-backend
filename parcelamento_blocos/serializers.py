# parcelamento_blocos/serializers.py
from rest_framework import serializers


class IncrementalParamsSerializer(serializers.Serializer):
    # profundidade do LOTE (não do quarteirão)
    prof_lote_m = serializers.FloatField(required=False, default=25.0)

    # 1 ou 2 fileiras (duas fileiras => prof_quarteirao = 2 * prof_lote)
    fileiras = serializers.IntegerField(required=False, default=2)

    # calçada
    calcada_largura_m = serializers.FloatField(required=False, default=2.5)
    calcada_encosta_aoi = serializers.BooleanField(
        required=False, default=False)

    # ruas
    larg_rua_horiz_m = serializers.FloatField(required=False, default=12.0)
    larg_rua_vert_m = serializers.FloatField(required=False, default=12.0)

    # compat do model (sem lotes por enquanto, mas NOT NULL no DB)
    frente_min_m = serializers.FloatField(required=False, default=10.0)

    # comprimento máximo do quarteirão (sempre respeitado)
    compr_max_quarteirao_m = serializers.FloatField(
        required=False, default=120.0)

    # orientação
    orientacao_modo = serializers.ChoiceField(
        choices=("auto_top_edge", "usar_orientacao_graus"),
        required=False,
        default="auto_top_edge",
    )
    orientacao_graus = serializers.FloatField(required=False, allow_null=True)

    preferencia_cardinal = serializers.ChoiceField(
        choices=("NW", "NE", "SW", "SE"),
        required=False,
        default="NW",
    )

    # SRID de cálculo métrico
    srid_calc = serializers.IntegerField(required=False, default=3857)

    # sinaliza “nova fase” (trocar linha base/orientação numa mesma versão)
    start_new_phase = serializers.BooleanField(required=False, default=False)

    def validate(self, attrs):
        fileiras = int(attrs.get("fileiras") or 2)
        if fileiras not in (1, 2):
            raise serializers.ValidationError({"fileiras": "Use 1 ou 2."})

        prof_lote = float(attrs.get("prof_lote_m") or 0)
        if prof_lote <= 0:
            raise serializers.ValidationError({"prof_lote_m": "Deve ser > 0."})

        calcada = float(attrs.get("calcada_largura_m") or 0)
        if calcada < 0:
            raise serializers.ValidationError(
                {"calcada_largura_m": "Deve ser >= 0."})

        larg_h = float(attrs.get("larg_rua_horiz_m") or 0)
        if larg_h <= 0:
            raise serializers.ValidationError(
                {"larg_rua_horiz_m": "Deve ser > 0."})

        larg_v = float(attrs.get("larg_rua_vert_m") or 0)
        if larg_v <= 0:
            raise serializers.ValidationError(
                {"larg_rua_vert_m": "Deve ser > 0."})

        frente = float(attrs.get("frente_min_m") or 0)
        if frente <= 0:
            raise serializers.ValidationError(
                {"frente_min_m": "Deve ser > 0."})

        compr_max = float(attrs.get("compr_max_quarteirao_m") or 0)
        if compr_max <= 0:
            raise serializers.ValidationError(
                {"compr_max_quarteirao_m": "Deve ser > 0."})

        srid = int(attrs.get("srid_calc") or 0)
        if srid <= 0:
            raise serializers.ValidationError({"srid_calc": "SRID inválido."})

        return attrs


class IncrementalPreviewSerializer(serializers.Serializer):
    restricoes_id = serializers.IntegerField()
    versao_id = serializers.IntegerField(required=False, allow_null=True)

    # obrigatória na criação OU ao iniciar nova fase
    linha_base = serializers.JSONField(required=False, allow_null=True)

    params = IncrementalParamsSerializer(required=False, default=dict)
    max_quarteiroes = serializers.IntegerField(required=False, default=1)

    def validate(self, attrs):
        max_q = int(attrs.get("max_quarteiroes") or 1)
        if max_q < 1 or max_q > 50:
            raise serializers.ValidationError(
                {"max_quarteiroes": "Use entre 1 e 50."})

        versao_id = attrs.get("versao_id")
        linha_base = attrs.get("linha_base")

        # ✅ blindagem: params sempre vira dict
        params = attrs.get("params") or {}
        if not isinstance(params, dict):
            try:
                params = dict(params)
            except Exception:
                params = {}

        start_new_phase = bool(params.get("start_new_phase"))

        # linha_base é obrigatória:
        # - quando cria (versao_id não vem)
        # - OU quando start_new_phase=true
        if (not versao_id) or start_new_phase:
            if not linha_base:
                raise serializers.ValidationError(
                    {"linha_base": "Obrigatória na criação ou ao iniciar nova fase (params.start_new_phase=true)."}
                )

        # valida tipo básico
        if linha_base:
            if not isinstance(linha_base, dict):
                raise serializers.ValidationError(
                    {"linha_base": "Deve ser um objeto GeoJSON (dict)."})
            geom = linha_base.get("geometry") if linha_base.get(
                "type") == "Feature" else linha_base
            if not isinstance(geom, dict) or geom.get("type") != "LineString":
                raise serializers.ValidationError(
                    {"linha_base": "Deve ser uma Feature/geometry LineString GeoJSON."})
            coords = geom.get("coordinates") or []
            if not isinstance(coords, list) or len(coords) < 2:
                raise serializers.ValidationError(
                    {"linha_base": "LineString precisa ter pelo menos 2 pontos."})

        return attrs
