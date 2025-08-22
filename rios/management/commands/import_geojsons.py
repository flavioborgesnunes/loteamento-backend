import gzip
import json
import re
from pathlib import Path
from typing import List, Optional, Tuple

import ijson
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

# Opcionais (melhor experiência)
try:
    import fiona
except Exception:
    fiona = None

try:
    from pyproj import CRS, Transformer
except Exception:
    CRS = None
    Transformer = None


# ---------- CRS / reprojeção ----------
def _mk_transformer_to_4326(crs_like) -> Optional["Transformer"]:
    if Transformer is None or CRS is None:
        return None
    if crs_like in (None, {}, ""):
        return None
    try:
        src = CRS.from_user_input(crs_like)
        dst = CRS.from_epsg(4326)
        if src == dst:
            return None
        return Transformer.from_crs(src, dst, always_xy=True)
    except Exception:
        return None


def _tfm_coords(coords, tfm: Optional["Transformer"]):
    if tfm is None:
        return [(float(x), float(y)) for (x, y) in coords]
    xs, ys = zip(*coords)
    x2, y2 = tfm.transform(xs, ys)
    return list(zip(map(float, x2), map(float, y2)))


# ---------- GEOMETRIAS ----------
def _to_multilines(geom: dict, tfm: Optional["Transformer"]):
    from django.contrib.gis.geos import LineString, MultiLineString
    if not geom:
        return None
    gtype = geom.get("type")
    if gtype == "LineString":
        coords = geom.get("coordinates") or []
        if len(coords) >= 2:
            c = _tfm_coords(coords, tfm)
            return MultiLineString(LineString(c, srid=4326))
    elif gtype == "MultiLineString":
        lines = []
        for line in geom.get("coordinates") or []:
            if len(line) >= 2:
                c = _tfm_coords(line, tfm)
                lines.append(LineString(c, srid=4326))
        if lines:
            return MultiLineString(lines, srid=4326)
    elif gtype == "GeometryCollection":
        lines = []
        for sub in geom.get("geometries") or []:
            subml = _to_multilines(sub, tfm)
            if subml:
                for p in subml:
                    if not p.empty and len(p.coords) >= 2:
                        lines.append(LineString(p.coords, srid=4326))
        if lines:
            return MultiLineString(lines, srid=4326)
    return None


def _to_multipolygon(geom: dict, tfm: Optional["Transformer"]):
    from django.contrib.gis.geos import LinearRing, MultiPolygon, Polygon
    if not geom:
        return None
    gtype = geom.get("type")
    polys = []

    def _mk_poly(rings):
        if not rings:
            return None
        shell = rings[0]
        holes = rings[1:] if len(rings) > 1 else []
        shell_tf = _tfm_coords(shell, tfm)
        if len(shell_tf) < 4:
            return None
        shell_lr = LinearRing(shell_tf)
        holes_lr = [LinearRing(_tfm_coords(h, tfm))
                    for h in holes if len(h) >= 4]
        return Polygon(shell_lr, holes_lr, srid=4326)

    if gtype == "Polygon":
        rings = geom.get("coordinates") or []
        p = _mk_poly(rings)
        if p:
            polys.append(p)
    elif gtype == "MultiPolygon":
        for poly in geom.get("coordinates") or []:
            if poly:
                p = _mk_poly(poly)
                if p:
                    polys.append(p)
    elif gtype == "GeometryCollection":
        for sub in geom.get("geometries") or []:
            sub_mp = _to_multipolygon(sub, tfm)
            if sub_mp:
                for p in sub_mp:
                    if not p.empty:
                        polys.append(
                            Polygon(p.exterior_ring, list(p.interiors), srid=4326))

    if polys:
        return MultiPolygon(polys, srid=4326)
    return None


# ---------- IO ----------
def _open_stream(path: Path):
    return gzip.open(path, "rb") if path.suffix.lower() == ".gz" else open(path, "rb")


# ---------- FLUSH ----------
def _flush_rows_polygon(table: str, rows: List[Tuple[Optional[str], Optional[str], str]], extra_cols=None):
    """
    rows: [(name, source, wkt), ...]
    extra_cols: dict com colunas fixas (ex.: {'uf': 'SC'})
    """
    extra_cols = extra_cols or {}
    col_names = ["name", "source", "geom"] + list(extra_cols.keys())
    placeholders = "(%s,%s,ST_GeomFromText(%s,4326)" + \
        "".join([",%s"] * len(extra_cols)) + ")"
    sql = f'INSERT INTO "{table}" ({",".join(col_names)}) VALUES {placeholders}'
    values = [tuple(r) + tuple(extra_cols.values()) for r in rows]
    with connection.cursor() as cur, transaction.atomic():
        cur.executemany(sql, values)


def _flush_rows_lines(table: str, rows: List[Tuple[Optional[str], Optional[str], str]]):
    sql = f'INSERT INTO "{table}" (name, source, geom) VALUES (%s,%s,ST_GeomFromText(%s,4326))'
    with connection.cursor() as cur, transaction.atomic():
        cur.executemany(sql, rows)


def _vacuum_analyze(table: str):
    with connection.cursor() as cur:
        cur.execute(f'VACUUM ANALYZE "{table}";')


# ---------- CMD ----------
ALIASES = {
    "areas": "geodata_area",
    "area": "geodata_area",
    "cidades": "geodata_cidade",
    "cidade": "geodata_cidade",
    "lt": "geodata_linhatransmissao",
    "malha_ferroviaria": "geodata_malha_ferroviaria",
    "linhas": "geodata_linhatransmissao",
    "linhas_transmissao": "geodata_linhatransmissao",
    "limites": "geodata_limitefederal",
    "limites_federais": "geodata_limitefederal",
}


class Command(BaseCommand):
    help = "Importa .geojson/.geojson.gz em streaming para uma tabela PostGIS."

    def add_arguments(self, parser):
        parser.add_argument("files", nargs="+", type=str,
                            help="Arquivos .geojson/.geojson.gz")
        parser.add_argument("--table", required=True,
                            help="Tabela destino (ex.: geodata_area) ou atalho (areas|lt|cidades|limites)")
        parser.add_argument("--truncate", action="store_true",
                            help="TRUNCATE antes de importar")
        parser.add_argument("--batch", type=int, default=5000,
                            help="Tamanho do lote (default 5000)")
        parser.add_argument("--max-features", type=int,
                            default=0, help="Limite de features (0=todas)")
        parser.add_argument("--name-prop", type=str, default="",
                            help="Propriedade p/ name (senão tenta name/nome)")
        parser.add_argument("--source", type=str, default="",
                            help="Valor para o campo source")
        parser.add_argument("--extra-uf", type=str, default="",
                            help="Força UF (ex.: SC) — válido para geodata_area")

    def handle(self, *args, **opts):
        files = [Path(p) for p in opts["files"]]
        table_arg = (opts["table"] or "").strip().lower()
        table = ALIASES.get(table_arg, table_arg)

        # detecta tipo por tabela
        target_is_polygon = any(key in table for key in [
                                "_area", "_cidade", "_limitefederal"])
        target_is_lines = any(key in table for key in [
                              "_linhatransmissao", "_malha_ferroviaria", "_ferrovia"])

        if not (target_is_polygon or target_is_lines):
            raise CommandError(
                f'Tabela "{table}" não reconhecida para tipo geométrico.')

        batch_size = max(500, int(opts.get("batch", 5000)))
        max_features = max(0, int(opts.get("max-features", 0)))
        name_prop = (opts.get("name-prop") or "").strip()
        source_label = (opts.get("source") or "").strip() or None

        # truncate
        if opts.get("truncate"):
            self.stdout.write(f'🧹 TRUNCATE "{table}" ...')
            with connection.cursor() as cur:
                cur.execute(f'TRUNCATE "{table}" RESTART IDENTITY;')

        # extra cols fixas
        base_extra = {}
        if table == "geodata_area":
            extra_uf = (opts.get("extra-uf") or "").strip().upper()
            if extra_uf:
                base_extra["uf"] = extra_uf

        total_in_all = total_saved_all = 0

        for path in files:
            if not path.exists():
                raise CommandError(f"Arquivo não encontrado: {path}")

            # por-arquivo: inferir UF se destino = geodata_area e não veio --extra-uf
            per_file_extra = dict(base_extra)
            if table == "geodata_area" and "uf" not in per_file_extra:
                m = re.search(r"_([A-Z]{2})\.geojson(?:\.gz)?$", path.name)
                if m:
                    per_file_extra["uf"] = m.group(1).upper()

            print(f'📥 Importando {path} → "{table}"  extras={per_file_extra}')

            use_ijson = path.suffix.lower() == ".gz" or fiona is None
            batch_rows: List[Tuple[Optional[str], Optional[str], str]] = []
            total_in = total_saved = 0

            # ---------- ijson ----------
            if use_ijson:
                transformer = None
                with _open_stream(path) as f:
                    try:
                        feats = ijson.items(f, "features.item")
                    except Exception as e:
                        raise CommandError(
                            f"Erro no parser incremental (FeatureCollection?): {e}")

                    for feat in feats:
                        total_in += 1
                        try:
                            geom = feat.get("geometry")
                            if not geom:
                                continue

                            if target_is_polygon:
                                g = _to_multipolygon(geom, transformer)
                            else:
                                g = _to_multilines(geom, transformer)
                            if not g or g.empty:
                                continue

                            props = feat.get("properties") or {}
                            name = props.get(name_prop) if name_prop else (
                                props.get("name") or props.get("nome"))
                            batch_rows.append((name, source_label, g.wkt))

                            if len(batch_rows) >= batch_size:
                                if target_is_polygon:
                                    _flush_rows_polygon(
                                        table, batch_rows, extra_cols=per_file_extra)
                                else:
                                    _flush_rows_lines(table, batch_rows)
                                total_saved += len(batch_rows)
                                print(f'   [{table}] salvos: {total_saved}')
                                batch_rows.clear()
                        except Exception as e:
                            print(
                                f"[warn:{table}] feature #{total_in} ignorada: {e}")

                        if max_features and total_in >= max_features:
                            break

            # ---------- Fiona ----------
            else:
                try:
                    with fiona.open(path) as src:
                        crs_like = src.crs or src.crs_wkt
                        transformer = _mk_transformer_to_4326(crs_like)

                        for feat in src:
                            total_in += 1
                            try:
                                geom = feat.get("geometry")
                                if not geom:
                                    continue

                                if target_is_polygon:
                                    g = _to_multipolygon(geom, transformer)
                                else:
                                    g = _to_multilines(geom, transformer)
                                if not g or g.empty:
                                    continue

                                props = feat.get("properties") or {}
                                name = props.get(name_prop) if name_prop else (
                                    props.get("name") or props.get("nome"))
                                batch_rows.append((name, source_label, g.wkt))

                                if len(batch_rows) >= batch_size:
                                    if target_is_polygon:
                                        _flush_rows_polygon(
                                            table, batch_rows, extra_cols=per_file_extra)
                                    else:
                                        _flush_rows_lines(table, batch_rows)
                                    total_saved += len(batch_rows)
                                    print(
                                        f'   [{table}] salvos: {total_saved}')
                                    batch_rows.clear()
                            except Exception as e:
                                print(
                                    f"[warn:{table}] feature #{total_in} ignorada: {e}")

                            if max_features and total_in >= max_features:
                                break
                except Exception as e:
                    raise CommandError(f"Erro com Fiona em {path}: {e}")

            # flush final
            if batch_rows:
                if target_is_polygon:
                    _flush_rows_polygon(table, batch_rows,
                                        extra_cols=per_file_extra)
                else:
                    _flush_rows_lines(table, batch_rows)
                total_saved += len(batch_rows)
                batch_rows.clear()

            _vacuum_analyze(table)
            print(
                f'✅ {path.name} → "{table}": lidas {total_in} | salvas {total_saved}')
            total_in_all += total_in
            total_saved_all += total_saved

        print(
            f'🏁 Concluído: "{table}" | total lidas {total_in_all} | total salvas {total_saved_all}')
