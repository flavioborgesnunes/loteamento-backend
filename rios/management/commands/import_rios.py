# rios/management/commands/import_rios.py
import gzip
import io
import json
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

import ijson
from django.contrib.gis.geos import GEOSGeometry, LineString, MultiLineString
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from rios.models import Waterway

# Importes opcionais: fiona (preferido para .geojson) e pyproj (reprojeção)
try:
    import fiona
except Exception:
    fiona = None

try:
    from pyproj import CRS, Transformer
except Exception:
    CRS = None
    Transformer = None


def _mk_transformer_to_4326(crs_like) -> Optional[Transformer]:
    """
    Retorna um Transformer para reprojetar -> EPSG:4326, ou None se já estiver em 4326
    ou se pyproj não estiver disponível.
    """
    if Transformer is None or CRS is None:
        return None

    if crs_like in (None, {}, ""):
        # Sem CRS declarado: assumimos que já está em 4326
        return None

    try:
        src = CRS.from_user_input(crs_like)
        dst = CRS.from_epsg(4326)
        if src == dst:
            return None
        return Transformer.from_crs(src, dst, always_xy=True)
    except Exception:
        # Se falhar, assume 4326
        return None


def _transform_linestring_coords(coords: Iterable[Tuple[float, float]], tfm: Optional[Transformer]):
    if tfm is None:
        return [(float(x), float(y)) for (x, y) in coords]
    # always_xy=True -> (x,y) = (lon,lat) quando for geográfico; em projetado é (E, N)
    xs, ys = zip(*coords)
    x2, y2 = tfm.transform(xs, ys)
    return list(zip(map(float, x2), map(float, y2)))


def _geom_to_multilines(geom: dict, tfm: Optional[Transformer]) -> Optional[MultiLineString]:
    """
    Converte um mapeamento GeoJSON em MultiLineString (SRID=4326).
    Aceita LineString, MultiLineString e GeometryCollection (extraindo linhas).
    """
    if not geom:
        return None
    gtype = geom.get("type")
    if gtype not in ("LineString", "MultiLineString", "GeometryCollection"):
        return None

    lines: List[LineString] = []

    if gtype == "LineString":
        coords = geom.get("coordinates") or []
        if len(coords) >= 2:
            coords = _transform_linestring_coords(coords, tfm)
            if len(coords) >= 2:
                lines.append(LineString(coords, srid=4326))

    elif gtype == "MultiLineString":
        mcoords = geom.get("coordinates") or []
        for line in mcoords:
            if len(line) >= 2:
                coords = _transform_linestring_coords(line, tfm)
                if len(coords) >= 2:
                    lines.append(LineString(coords, srid=4326))

    else:  # GeometryCollection
        geoms = geom.get("geometries") or []
        for sub in geoms:
            sub_mls = _geom_to_multilines(sub, tfm)
            if isinstance(sub_mls, MultiLineString):
                for p in sub_mls:
                    if not p.empty and len(p.coords) >= 2:
                        lines.append(LineString(p.coords, srid=4326))

    if not lines:
        return None
    return MultiLineString(lines, srid=4326)


def _open_gz_stream(path: Path):
    # Leitura binária gzip para ijson
    return gzip.open(path, "rb")


class Command(BaseCommand):
    help = "Importa um rios.geojson (ou .geojson.gz) grande em streaming para Waterway, com reprojeção p/ EPSG:4326."

    def add_arguments(self, parser):
        parser.add_argument("geojson_path", type=str,
                            help="Caminho para rios.geojson (.geojson ou .geojson.gz)")
        parser.add_argument("--truncate", action="store_true",
                            help="Apaga os dados antes de importar")
        parser.add_argument("--batch", type=int, default=5000,
                            help="Tamanho do lote de inserts (default: 5000)")
        parser.add_argument("--max-features", type=int,
                            default=0, help="Limite de features (0 = todas)")
        parser.add_argument(
            "--source", type=str, default="rios.geojson", help="Valor do campo 'source'")
        parser.add_argument("--name-prop", type=str, default="",
                            help="Nome do campo de nome (ex.: 'name' ou 'nome')")

    def handle(self, *args, **opts):
        path = Path(opts["geojson_path"])
        if not path.exists():
            raise CommandError(f"Arquivo não encontrado: {path}")

        batch_size: int = max(500, int(opts.get("batch", 5000)))
        max_features: int = max(0, int(opts.get("max-features", 0)))
        source_label: str = opts.get("source", "rios.geojson")
        name_prop: str = (opts.get("name-prop") or "").strip()

        if opts.get("truncate", False):
            self.stdout.write("🧹 Limpando tabela rios_waterway ...")
            Waterway.objects.all().delete()
            with connection.cursor() as cur:
                cur.execute(
                    """
                    SELECT setval(
                        pg_get_serial_sequence('rios_waterway','id'),
                        COALESCE((SELECT MAX(id) FROM rios_waterway), 1),
                        TRUE
                    );
                    """
                )

        total_in = 0
        total_saved = 0
        batch: List[Waterway] = []

        # Regra: .geojson.gz => ijson; caso contrário, tenta Fiona (se disponível); se não, cai para ijson.
        use_ijson = path.suffix.lower() == ".gz" or fiona is None

        if use_ijson:
            self.stdout.write(f"📥 Abrindo (ijson streaming): {path}")
            # Para ijson, não temos CRS global: assumimos EPSG:4326
            transformer = None

            with _open_gz_stream(path) if path.suffix.lower() == ".gz" else open(path, "rb") as f:
                try:
                    items = ijson.items(f, "features.item")
                except Exception as e:
                    raise CommandError(
                        f"Falha ao iniciar parser incremental. "
                        f"Certifique-se de que o arquivo é FeatureCollection válido. Erro: {e}"
                    )

                for feat in items:
                    total_in += 1
                    try:
                        geom = feat.get("geometry")
                        if not geom:
                            continue

                        # Converte para MultiLineString (EPSG:4326)
                        mls = _geom_to_multilines(geom, transformer)
                        if not mls or mls.empty:
                            continue

                        props = feat.get("properties") or {}
                        if name_prop:
                            name = props.get(name_prop)
                        else:
                            name = props.get("name") or props.get("nome")

                        batch.append(
                            Waterway(geom=mls, name=name, source=source_label))

                        if len(batch) >= batch_size:
                            with transaction.atomic():
                                Waterway.objects.bulk_create(
                                    batch, ignore_conflicts=True, batch_size=batch_size)
                            total_saved += len(batch)
                            self.stdout.write(
                                f"[import] {total_saved} salvos...")
                            batch.clear()

                    except Exception as e:
                        self.stderr.write(
                            f"[warn] feature #{total_in} ignorada: {e}")

                    if max_features and total_in >= max_features:
                        break

        else:
            # Fiona em streaming, com detecção de CRS
            self.stdout.write(f"📥 Abrindo (fiona streaming): {path}")
            try:
                with fiona.open(path) as src:
                    # CRS pode vir como dict (projjson), wkt, ou None
                    crs_like = src.crs or src.crs_wkt
                    transformer = _mk_transformer_to_4326(crs_like)

                    for feat in src:
                        total_in += 1
                        try:
                            geom = feat.get("geometry")
                            if not geom:
                                continue

                            mls = _geom_to_multilines(geom, transformer)
                            if not mls or mls.empty:
                                continue

                            props = feat.get("properties") or {}
                            if name_prop:
                                name = props.get(name_prop)
                            else:
                                name = props.get("name") or props.get("nome")

                            batch.append(
                                Waterway(geom=mls, name=name, source=source_label))

                            if len(batch) >= batch_size:
                                with transaction.atomic():
                                    Waterway.objects.bulk_create(
                                        batch, ignore_conflicts=True, batch_size=batch_size)
                                total_saved += len(batch)
                                self.stdout.write(
                                    f"[import] {total_saved} salvos...")
                                batch.clear()

                        except Exception as e:
                            self.stderr.write(
                                f"[warn] feature #{total_in} ignorada: {e}")

                        if max_features and total_in >= max_features:
                            break

            except Exception as e:
                raise CommandError(f"Erro abrindo com Fiona: {e}. "
                                   f"Instale o GDAL corretamente ou use .geojson.gz + ijson.")

        # Flush final
        if batch:
            with transaction.atomic():
                Waterway.objects.bulk_create(
                    batch, ignore_conflicts=True, batch_size=batch_size)
            total_saved += len(batch)
            batch.clear()

        # Estatística p/ planner
        with connection.cursor() as cur:
            cur.execute("VACUUM ANALYZE rios_waterway;")

        self.stdout.write(self.style.SUCCESS(
            f"✅ Import finalizado. Lidas: {total_in} | Salvas: {total_saved}"
        ))
