from django.shortcuts import get_object_or_404
from parcelamento.models import ParcelamentoVersao
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView
from restricoes.models import Restricoes

from .serializers import IncrementalPreviewSerializer
from .services import run_incremental_quarteiroes_calcadas


class QuarteiraoLotesPreviewView(APIView):
    """
    POST /api/parcelamento-blocos/preview/

    Body:
      {
        "restricoes_id": 48,
        "versao_id": null,
        "linha_base": {...GeoJSON...},   # obrigatório quando cria
        "params": {...},
        "max_quarteiroes": 1
      }
    """

    def post(self, request):
        ser = IncrementalPreviewSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data

        restricoes_id = data["restricoes_id"]
        versao_id = data.get("versao_id")
        linha_base = data.get("linha_base")
        params = data.get("params") or {}
        max_quarteiroes = int(data.get("max_quarteiroes") or 1)

        r = get_object_or_404(Restricoes, id=restricoes_id)

        # >>> IMPORTANT: o project vem da própria Restricoes (domínios separados, mas restrição pertence a um projeto)
        project = getattr(r, "project", None)
        if project is None:
            return Response(
                {"detail": "Restricoes não possui vínculo com Project (campo project ausente)."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user = getattr(request, "user", None)

        created = False
        versao = None

        if versao_id:
            versao = get_object_or_404(
                ParcelamentoVersao, id=versao_id, project=project)
            created = False
        else:
            # cria nova versão “semente” (service irá criar de fato)
            versao = None
            created = True

        try:
            result = run_incremental_quarteiroes_calcadas(
                project=project,
                restricoes=r,
                versao=versao,
                creating_new=created,
                linha_base=linha_base,
                params=params,
                max_quarteiroes=max_quarteiroes,
                user=user,
            )
            return Response(result, status=status.HTTP_200_OK)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response(
                {"detail": "Erro ao gerar quarteirões/calçadas.",
                    "error": str(e)},
                status=status.HTTP_400_BAD_REQUEST,
            )
