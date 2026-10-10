"""Render a static preview from the same Earth Engine tiles used by Leaflet."""
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import math
import numpy as np
from PIL import Image
import requests


def tile_preview(tile_url, bbox, dim=512):
    west, south, east, north = bbox
    def mercator_y(latitude):
        lat = math.radians(max(-85.0511, min(85.0511, latitude)))
        return (1 - math.asinh(math.tan(lat)) / math.pi) / 2
    width_fraction = (east - west) / 360
    height_fraction = mercator_y(south) - mercator_y(north)
    zoom = max(0, min(18, math.floor(math.log2(dim / (256 * max(width_fraction, height_fraction))))))
    world = 256 * 2 ** zoom
    x0, x1 = (west + 180) / 360 * world, (east + 180) / 360 * world
    y0, y1 = mercator_y(north) * world, mercator_y(south) * world
    left, right = math.floor(x0 / 256), math.floor(x1 / 256)
    top, bottom = math.floor(y0 / 256), math.floor(y1 / 256)
    tiles = [(x, y) for x in range(left, right + 1) for y in range(top, bottom + 1)]
    if len(tiles) > 16:
        raise ValueError('Preview tile extent is too large')
    def fetch(position):
        x, y = position
        response = requests.get(tile_url.format(z=zoom, x=x, y=y), timeout=(3, 40))
        response.raise_for_status()
        return position, Image.open(BytesIO(response.content)).convert('RGBA')
    mosaic = Image.new('RGBA', ((right-left+1)*256, (bottom-top+1)*256))
    with ThreadPoolExecutor(max_workers=4) as pool:
        for (x, y), tile in pool.map(fetch, tiles):
            mosaic.paste(tile, ((x-left)*256, (y-top)*256))
    # Reproject Web Mercator tile rows to equally spaced latitude rows for the
    # geographic bounds used by make_analysis_map; this affects display only.
    width = dim
    height = max(1, round(dim * (north-south) / (east-west)))
    height = min(dim * 2, height)
    xs = np.linspace(x0, x1, width, endpoint=False) + (x1-x0)/(2*width)
    latitudes = np.linspace(north, south, height, endpoint=False) - (north-south)/(2*height)
    ys = np.array([mercator_y(lat) * world for lat in latitudes])
    pixels = np.asarray(mosaic)
    columns = np.clip((xs-left*256).astype(int), 0, pixels.shape[1]-1)
    rows = np.clip((ys-top*256).astype(int), 0, pixels.shape[0]-1)
    return pixels[rows[:,None],columns[None,:]]
