from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .roads.orchestrator import generate_and_rank_road_networks
from .serializers import RoadsPreviewRequestSerializer


class RoadsPreviewView(APIView):
    """
    POST /roads/preview/
    Body:
      - al_geojson: geometry (Polygon/MultiPolygon)
      - params: regras e parâmetros do gerador
      - targets: metas para avaliação
    Response:
      - best
      - ranked[]
    """

    def post(self, request):
        ser = RoadsPreviewRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data

        al_geojson = data["al_geojson"]
        params = data.get("params") or {}
        targets = data.get("targets") or {}
        srid_calc = int(data.get("srid_calc") or 3857)

        payload = generate_and_rank_road_networks(
            al_wgs_geojson=al_geojson,
            params=params,
            srid_calc=srid_calc,
            targets=targets,
        )
        return Response(payload, status=status.HTTP_200_OK)
