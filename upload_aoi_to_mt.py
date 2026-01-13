# -*- coding: utf-8 -*-
"""
Created on Wed Dec 31 09:57:39 2025

@author: Craig Pearce
"""

import pyodbc, os
from dotenv import load_dotenv
import geopandas as gpd, pandas as pd
from pathlib import Path
from shapely.geometry import Polygon, MultiPolygon, LinearRing
from shapely import wkt
from sqlalchemy import text
import logging
import unicodedata

# Logging
import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger(__name__)

# Load .env
here = Path(__file__).resolve().parent if "__file__" in globals() else Path.cwd()
load_dotenv(here / ".env", override=False)

# Input choices
input_file = os.getenv("INPUT_FILE_PATH")
geo_type_name = os.getenv("GEO_TYPE_NAME")
db_choice = os.getenv("DB_CHOICE")

# Database connection
def get_sql_server_conn_str(env: str) -> str:
    server_map = {
        "DEV": os.getenv("SQL_DEV_SERVER"),
        "PRIM": os.getenv("SQL_PRIM_SERVER"),
    }

    server = server_map.get(env)
    if not server:
        raise ValueError("env must be 'DEV' or 'PRIM' and server must be set in .env")

    # Credentials from .env file
    user = os.getenv("SQL_KP_USER")
    password = os.getenv("SQL_KP_PASSWORD")
    database = os.getenv("SQL_DATABASE", "ais_replica")
    
    # Use correct driver
    installed = set(pyodbc.drivers())
    if "ODBC Driver 18 for SQL Server" in installed:
        driver = "ODBC Driver 18 for SQL Server"
    elif "ODBC Driver 17 for SQL Server" in installed:
        driver = "ODBC Driver 17 for SQL Server"
    else:
        raise RuntimeError(f"No SQL Server ODBC driver found. Installed: {sorted(installed)}")

    # Raise error if credentials are missing
    if not user or not password:
        raise RuntimeError("Missing SQL_KP_USER / SQL_KP_PASSWORD in .env")

    # Return correct parameters to access the database
    return (
        f"DRIVER={{{driver}}};"
        f"SERVER={server};"
        f"DATABASE={database};"
        f"UID={user};PWD={password};"
        "Encrypt=yes;"
        "TrustServerCertificate=yes;"
    )

# --- Functions ---

# Correct Orientation Function
def _ring_is_ccw(coords) -> bool:
    # LinearRing.is_ccw exists in Shapely 1.8+ / 2.x
    return LinearRing(coords).is_ccw

# Ensure that outer and inner rings have the correct orientation
def enforce_shell_and_hole_orientation(geom, shell_ccw=True):
    """
    Enforce polygon ring orientation:
      - shell (exterior) CCW if shell_ccw=True, else CW
      - holes (interiors) opposite orientation to shell
    Works for Polygon and MultiPolygon.
    """
    if geom is None or geom.is_empty:
        return geom

    def fix_polygon(poly: Polygon) -> Polygon:
        shell = list(poly.exterior.coords)
        holes = [list(r.coords) for r in poly.interiors]

        # Ensure shell orientation
        shell_is_ccw = _ring_is_ccw(shell)
        if shell_ccw != shell_is_ccw:
            shell = shell[::-1]

        # Ensure holes opposite orientation to shell
        desired_hole_ccw = not shell_ccw
        fixed_holes = []
        for h in holes:
            h_is_ccw = _ring_is_ccw(h)
            if desired_hole_ccw != h_is_ccw:
                h = h[::-1]
            fixed_holes.append(h)

        return Polygon(shell, fixed_holes)

    if isinstance(geom, Polygon):
        return fix_polygon(geom)

    if isinstance(geom, MultiPolygon):
        return MultiPolygon([fix_polygon(p) for p in geom.geoms])

    return geom

# Detect which column in a CSV file has the WKT format shapes
def detect_wkt_column(df: pd.DataFrame) -> str | None:
    """
    Detect a WKT geometry column by inspecting string columns.
    Returns column name or None.
    """
    WKT_PREFIXES = ("POINT", "LINESTRING", "POLYGON",
                    "MULTIPOINT", "MULTILINESTRING", "MULTIPOLYGON",
                    "GEOMETRYCOLLECTION")

    for col in df.columns:
        if not pd.api.types.is_object_dtype(df[col]):
            continue

        sample = (
            df[col]
            .dropna()
            .astype(str)
            .str.strip()
            .head(5)
            .str.upper()
        )

        if len(sample) == 0:
            continue

        if sample.apply(lambda s: s.startswith(WKT_PREFIXES)).all():
            return col

    return None

# Always produce a gdf from any of three different formats
# csv file can have wkt column or separate lon/lat columns
import os
import pandas as pd
import geopandas as gpd
from shapely import wkt as shapely_wkt

def load_as_gdf(
    input_file: str,
    wkt_col: str | None = None,
    lon_col: str | None = None,
    lat_col: str | None = None,
    crs: str = "EPSG:4326",
    *,
    require_geometry_for_csv: bool = True,
) -> gpd.GeoDataFrame:
    """
    Load CSV / SHP / GPKG and always return a GeoDataFrame with a real geometry column.

    CSV behavior:
      - If wkt_col is provided, parse WKT to Shapely geometries (Polygon/MultiPolygon/Point/etc).
      - Else if lon_col/lat_col provided, create Point geometries.
      - Else:
          - if require_geometry_for_csv=True -> raise (recommended, prevents silent None geometries)
          - otherwise -> return GeoDataFrame with None geometry values.

    SHP/GPKG behavior:
      - read_file, ensure CRS exists (assign `crs` if missing).

    Returns:
      GeoDataFrame with active geometry column named "geometry".
    """
    ext = os.path.splitext(input_file)[1].lower()

    # --- CSV ---
    if ext == ".csv":
        df = pd.read_csv(input_file)
    
        # Auto-detect WKT column if not provided
        if wkt_col is None:
            wkt_col = detect_wkt_column(df)
    
        if wkt_col:
            log.info("Detected WKT geometry column: %s", wkt_col)
    
            geometry = df[wkt_col].apply(
                lambda v: shapely_wkt.loads(v) if isinstance(v, str) and v.strip() else None
            )
    
            df = df.drop(columns=[wkt_col])
            gdf = gpd.GeoDataFrame(df, geometry=geometry, crs=crs)
    
        elif lon_col and lat_col:
            gdf = gpd.GeoDataFrame(
                df,
                geometry=gpd.points_from_xy(df[lon_col], df[lat_col]),
                crs=crs,
            )
    
        else:
            raise ValueError(
                "CSV has no detectable geometry. "
                "Provide WKT, lon/lat columns, or specify wkt_col explicitly."
            )

    # --- Shapefile / GeoPackage ---
    if ext in (".shp", ".gpkg"):
        gdf = gpd.read_file(input_file)
        if gdf.crs is None:
            gdf = gdf.set_crs(crs)
        return gdf

    raise ValueError(f"Unsupported file type: {ext}")


#Resolve non-ascii
custom_map = {
    'ø': 'o',
    'Ø': 'O',
    'ß': 'ss',
    'æ': 'ae',
    'Æ': 'Ae',
    'ð': 'd',
    'Ð': 'D',
    'þ': 'th',
    'Þ': 'Th'
}

def normalize(text):
    if not isinstance(text, str):
        return text
    text = unicodedata.normalize('NFKD', text)
    for k, v in custom_map.items():
        text = text.replace(k, v)
    return text.encode('ascii', 'ignore').decode('ascii')

# Read file
gdf = load_as_gdf(input_file)

# Guard to make sure zone_name exists
if "zone_name" not in gdf.columns:
    raise ValueError(f"Expected column 'zone_name' not found. Columns: {list(gdf.columns)}")

#Initialcheck for Geopanadas
non_ascii_rows = gdf[gdf['zone_name'].str.contains(r'[^\x00-\x7F]', na=False)]
# log.info(non_ascii_rows)

#Apply fix
gdf['zone_name_cleaned'] = gdf['zone_name'].apply(normalize)
gdf.drop(columns=['zone_name'], inplace=True)
gdf.rename(columns={'zone_name_cleaned': 'zone_name'}, inplace=True)

#check for Geopanadas
non_ascii_rows = gdf[gdf['zone_name'].str.contains(r'[^\x00-\x7F]', na=False)]
# log.info(non_ascii_rows)
if len(non_ascii_rows) == 0:
    gdf.to_file("./Ascii_fixed.shp", driver="ESRI Shapefile", encoding='utf-8')

# --- Connect to SQL Server ---
conn_str = get_sql_server_conn_str(db_choice)
sql_conn = pyodbc.connect(conn_str)
log.info("Autocommit: %s", sql_conn.autocommit)
sql_cur = sql_conn.cursor()

# --- Correct Orientation ---
gdf["geometry"] = gdf["geometry"].apply(lambda g: enforce_shell_and_hole_orientation(g, shell_ccw=True))
gdf = gdf.to_crs(epsg=4326)

#fix invalid geometries , buffer 0m shapely known trick to clean and  re-generate and repair geometries
# gdf['geometry'] = gdf['geometry'].apply(lambda g: g if g.is_valid else g.buffer(0))

# Add GEO_TYPE and geography WKT columns
# gdf['geometry'] = gdf['geometry'].simplify(tolerance=0.0001, preserve_topology=True)
gdf['GEO_TYPE'] = geo_type_name
gdf['geometry_wkt'] = gdf['geometry'].apply(lambda g: g.wkt)
gdf['geography_wkt'] = gdf['geometry'].apply(lambda g: g.wkt)

# Drop the geometry (Shapely objects) before uploading
df = pd.DataFrame(gdf.drop(columns='geometry'))

# Insert into SQL Server
insert_sql = """
    INSERT INTO areas_static (POLYGON, GEO_TYPE, GEO_NAME_FULL)
    VALUES (
        geography::STGeomFromText(?, 4326).MakeValid(),
        ?,?
    )
"""

# Upload data
log.info("Starting data upload...")

# Upload data (batched)
log.info("Starting data upload (%d rows)...", len(df))

rows = [
    (row['geography_wkt'], row['GEO_TYPE'], row['zone_name'])
    for _, row in df.iterrows()
]

sql_cur.fast_executemany = True
sql_cur.executemany(insert_sql, rows)
sql_conn.commit()

log.info("Upload completed.")

