import io
import zipfile

import simplekml


def kmz_from_lines(rows, name='Rios - Recorte'):
    """
    rows: iterável de (props: dict, geom: GEOSGeometry)
    Gera KMZ com linhas azuis.
    """
    kml = simplekml.Kml(name=name)
    for props, geom in rows:
        if geom.empty:
            continue
        if geom.geom_type == 'MultiLineString':
            for ls in geom:
                ln = kml.newlinestring(coords=list(ls.coords))
                ln.name = props.get('name') or ''
                ln.style.linestyle.width = 2
                ln.style.linestyle.color = simplekml.Color.blue
        elif geom.geom_type == 'LineString':
            ln = kml.newlinestring(coords=list(geom.coords))
            ln.name = props.get('name') or ''
            ln.style.linestyle.width = 2
            ln.style.linestyle.color = simplekml.Color.blue
    data = kml.kml().encode('utf-8')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('doc.kml', data)
    buf.seek(0)
    return buf
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('doc.kml', data)
    buf.seek(0)
    return buf
