"""Bound public demo requests before Earth Engine and model work."""
import math
from datetime import date

VARIABLES = {'ndvi','evi','savi','ndwi','mndwi','ndbi','ui','nbi','bsi','ndsi',
             'lst','uhi','rgb','lulc','co','ch4','no2','so2','aerosol','o3','gpp','burned','ffpi'}

def validate_bbox(bbox):
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        raise ValueError('Could not resolve the region. Draw a small region on the map instead.')
    w,s,e,n = bbox
    if any(not isinstance(x, (int,float)) or not math.isfinite(x) for x in bbox):
        raise ValueError('Invalid region coordinates.')
    if not (-180 <= w < e <= 180 and -85 <= s < n <= 85):
        raise ValueError('Choose a valid region away from the poles and date line.')
    if (e-w) > 3 or (n-s) > 3:
        raise ValueError('Choose a city-sized region (at most 3 degrees across).')

def validate_roi(roi):
    if roi is None:
        return
    if not isinstance(roi, dict):
        raise ValueError('ROI must be a GeoJSON polygon.')
    geom = roi.get('geometry', roi)
    if not isinstance(geom, dict) or geom.get('type') != 'Polygon':
        raise ValueError('Draw a polygon or rectangle for the ROI.')
    rings = geom.get('coordinates')
    if not isinstance(rings, list) or not 1 <= len(rings) <= 10:
        raise ValueError('Invalid polygon.')
    points=[]
    for ring in rings:
        if not isinstance(ring,list) or len(ring)<4 or ring[0]!=ring[-1]:
            raise ValueError('Polygon rings must be closed.')
        for p in ring:
            if not isinstance(p,list) or len(p)!=2 or any(type(v) not in (int,float) or not math.isfinite(v) for v in p):
                raise ValueError('Invalid polygon coordinates.')
            points.append(p)
    if len(points)>500:
        raise ValueError('Use a simpler polygon (500 vertices maximum).')
    validate_bbox([min(p[0] for p in points),min(p[1] for p in points),max(p[0] for p in points),max(p[1] for p in points)])

def validate_analysis(region, start, end, variables):
    if not isinstance(region,str) or not 1<=len(region)<=160:
        raise ValueError('Specify a region of at most 160 characters.')
    try:
        a,b=date.fromisoformat(start),date.fromisoformat(end)
    except (ValueError,TypeError):
        raise ValueError('Specify valid start and end dates.')
    if not date(2013,1,1)<=a<b<=date.today() or b.year-a.year>4:
        raise ValueError('Choose a past period from 2013 onward, spanning at most five years.')
    if not isinstance(variables,list) or not 1<=len(variables)<=3 or any(v not in VARIABLES for v in variables):
        raise ValueError('Choose one to three supported GIS indices per request.')
