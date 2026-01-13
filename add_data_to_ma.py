# -*- coding: utf-8 -*-
"""
Created on Tue Mar 18 15:30:46 2025

@author: Craig Pearce
"""

# Load packages
from datetime import datetime, timezone
from dotenv import load_dotenv
import pandas as pd
import geopandas as gpd
import numpy as np
import os
from pathlib import Path
import psycopg2
from psycopg2.extras import execute_batch
from pyproj import CRS
from shapely import wkt
from typing import Optional, Tuple

# Logging
import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger(__name__)

# Load .env
here = Path(__file__).resolve().parent if "__file__" in globals() else Path.cwd()
load_dotenv(here / ".env", override=False)

# Read variables
input_path = os.getenv("INPUT_FILE_PATH")
target_schema = os.getenv("TARGET_SCHEMA", "ancillary")
target_table = os.getenv("TARGET_TABLE")
geom_col = os.getenv("GEOM_COL", "geometry")
pk_col = os.getenv("PK_COL", "id")
input_crs = os.getenv("ASSUME_CRS_IF_MISSING", "EPSG:4326")

# Database credentials
drop_table_if_exists_bool = os.getenv("DROP_EXISTING_TABLE","false").lower() == "true" # If a table of the same name already exists, delete that table
input_host=os.getenv("SQL_MA_HOST") # Host link
input_database=os.getenv("SQL_MA_DB") # Database name
input_user=os.getenv("SQL_MA_USER") # Add your username between the quotation marks
input_password=os.getenv("SQL_MA_PASS") # Add your password between the quotation marks

# Variable validation section of code
REQUIRED_VARS = [
  "INPUT_FILE_PATH","TARGET_SCHEMA","TARGET_TABLE","GEOM_COL","PK_COL",
  "SQL_MA_HOST","SQL_MA_DB","SQL_MA_USER","SQL_MA_PASS"
]

missing = [v for v in REQUIRED_VARS if not os.getenv(v)]

if missing:
    raise EnvironmentError(
        f"Missing required environment variables: {', '.join(missing)}"
    )

# Connect to the new geospatial database
def get_postgres_connection(host, database, user, password
):
    """
    Establishes and returns a connection to the PostgreSQL database.
    Returns:
        psycopg2.connection: A live connection to the PostgreSQL database.
    Raises:
        psycopg2.OperationalError: If the connection fails.
    """
    try:
        conn = psycopg2.connect(
            host=host,
            database=database,
            user=user,
            password=password
        )
        return conn
    except psycopg2.OperationalError as e:
        log.exception("Error connecting to the PostgreSQL database:")
        raise e

# Ensures that all geometries are entered into the database as multipolygons
def ensure_multipolygon(geom):
    if pd.isna(geom):
        return None
    wkt = geom.wkt if hasattr(geom, "wkt") else str(geom)
    if wkt.startswith("MULTIPOLYGON"):
        return wkt
    elif wkt.startswith("POLYGON"):
        return f"MULTIPOLYGON({wkt[7:]})"
    else:
        raise ValueError(f"Invalid geometry type - must be polygon or multipolygon")

# Ensures that data are entered into the database in the correct format
def pandas_dtype_to_pg(dtype):
    """Simple dtype mapping; extend as needed."""
    if pd.api.types.is_integer_dtype(dtype):
        return "BIGINT"
    if pd.api.types.is_float_dtype(dtype):
        return "DOUBLE PRECISION"
    if pd.api.types.is_bool_dtype(dtype):
        return "BOOLEAN"
    if pd.api.types.is_datetime64_any_dtype(dtype):
        return "TIMESTAMP"
    return "TEXT"

# Creates a table from the dataframe
def create_table_from_df(cur, target_table, df, schema=target_schema,
                         geom_col=None, srid=4326, drop_table_if_exists=False):

    qualified_name = f'{schema}.{target_table}'

    # Choose to drop existing table if you want to replace an entire table
    if drop_table_if_exists:
        cur.execute(f"DROP TABLE IF EXISTS {qualified_name} CASCADE;")

    col_defs = []
    for col in df.columns:
        if geom_col and col == geom_col:
            col_defs.append(f'"{col}" geometry(MultiPolygon, {srid})')
        else:
            pg_type = pandas_dtype_to_pg(df[col].dtype)
            col_defs.append(f'"{col}" {pg_type}')

    create_sql = f"""
    CREATE TABLE {qualified_name} (
        {", ".join(col_defs)}
    );
    """
    cur.execute(create_sql)

# Check if there is a primary key, if not then add one
def prepare_pk_column(df: pd.DataFrame, pk_col: str = "id", sort_cols=None, start: int = 1) -> pd.DataFrame:
    """
    Ensure df[pk_col] is a clean contiguous integer primary-key candidate (start..start+N-1).

    - If pk_col doesn't exist, it will be created.
    - If pk_col has NULLs, duplicates, non-numeric values, or gaps -> it is regenerated.
    - Regeneration is deterministic if sort_cols is provided.
    """
    out = df.copy()

    # Optional deterministic ordering
    if sort_cols:
        out = out.sort_values(by=sort_cols, kind="mergesort").reset_index(drop=True)
    else:
        out = out.reset_index(drop=True)

    # Helper: check if current pk_col is already a clean 1..N integer series
    def is_clean_contiguous_int(s: pd.Series) -> bool:
        if s.isna().any():
            return False
        # coerce to integer-like
        s_num = pd.to_numeric(s, errors="coerce")
        if s_num.isna().any():
            return False
        # must be integers
        if not np.all(np.equal(s_num, np.floor(s_num))):
            return False
        s_int = s_num.astype("int64")
        if s_int.duplicated().any():
            return False
        n = len(s_int)
        return s_int.min() == start and s_int.max() == start + n - 1

    if pk_col in out.columns and is_clean_contiguous_int(out[pk_col]):
        # Already good
        out[pk_col] = out[pk_col].astype("int64")
        return out

    # Regenerate IDs
    out[pk_col] = np.arange(start, start + len(out), dtype="int64")
    return out


# Add new table into specified database
def load_new_table(file_path, target_table, geom_col,
                  schema=target_schema, srid=4326, drop_table_if_exists=False):
    
    ext = os.path.splitext(file_path)[1].lower()

    # ---- Load to DF ----
    if ext == ".csv":
        df = pd.read_csv(file_path)
        if geom_col not in df.columns:
            raise ValueError(f"Column '{geom_col}' not found in CSV. Available columns: {list(df.columns)}")
        df[geom_col] = df[geom_col].apply(lambda s: None if pd.isna(s) else wkt.loads(s))
        gdf = gpd.GeoDataFrame(df, geometry=geom_col, crs="EPSG:4326")
        
    elif ext == ".shp" or ext == ".gpkg":
        gdf = gpd.read_file(file_path)
        # Make sure that 4326 is assigned
        gdf, original = ensure_crs_4326(gdf, assume_if_missing=input_crs)
        if geom_col not in gdf.columns:
            raise ValueError(f"Geometry column '{geom_col}' not found. Available columns: {list(gdf.columns)}")
            
    else:
        raise ValueError(f"Unsupported file type: {ext}")
     
    # Convert back to df  
    df = pd.DataFrame(gdf)

    # convert shapely geometry to MULTIPOLYGON WKT in geom_col
    df[geom_col] = gdf[geom_col].apply(lambda g: ensure_multipolygon(g)) 
        
    # Makes sure that a processed column of integers can be used as the primary key
    df = prepare_pk_column(df, pk_col)


    # ---- Connect to database then add data table ----
    conn = get_postgres_connection(input_host, input_database, input_user, input_password)
    try:
        with conn:
            with conn.cursor() as cur:
                # ---- CREATE TABLE ----
                log.info(
                    "Creating table %s.%s (drop_existing=%s). Columns=%s",
                    schema, target_table, drop_table_if_exists_bool, list(df.columns)
                )
    
                create_table_from_df(
                    cur,
                    target_table=target_table,
                    df=df,
                    schema=schema,
                    geom_col=geom_col,     # this column will be defined as geometry(MultiPolygon, srid)
                    srid=srid,
                    drop_table_if_exists=drop_table_if_exists_bool
                )
    
                # ---- INSERT rows (parameterized geometry) ----
                cols = list(df.columns)
                insert_cols_sql = ", ".join(f'"{c}"' for c in cols)
    
                # Build a VALUES placeholder list that converts WKT -> geometry only for geom_col
                value_exprs = []
                for c in cols:
                    if geom_col and c == geom_col:
                        value_exprs.append(f"ST_GeomFromText(%s, {srid})::geometry(MultiPolygon,{srid})")
                    else:
                        value_exprs.append("%s")
                values_sql = ", ".join(value_exprs)
    
                insert_sql = f'''
                    INSERT INTO "{schema}"."{target_table}" ({insert_cols_sql})
                    VALUES ({values_sql})
                '''
    
                # Build parameter rows: WKT strings (or None) are passed safely as parameters
                values = []
                for _, row in df.iterrows():
                    row_vals = []
                    for c in cols:
                        if geom_col and c == geom_col:
                            wkt = row[c]
                            row_vals.append(None if pd.isna(wkt) else wkt)
                        else:
                            v = row[c]
                            row_vals.append(None if pd.isna(v) else v)
                    values.append(tuple(row_vals))
    
                log.info("Inserting %d rows into %s.%s ...", len(values), schema, target_table)
                execute_batch(cur, insert_sql, values, page_size=5000)
                log.info("Insert complete.")
    
                # ---- ADD PRIMARY KEY (only after successful insert) ----
                log.info("Adding primary key (%s) to %s.%s ...", pk_col, schema, target_table)
                cur.execute(f'''
                    ALTER TABLE "{schema}"."{target_table}"
                    ADD PRIMARY KEY ("{pk_col}");
                ''')
                log.info("Primary key added successfully.")
    
    except Exception:
        # This logs the full traceback, which is very helpful for colleagues debugging issues.
        log.exception("Load failed for %s.%s", schema, target_table)
        raise
    
    finally:
        try:
            conn.close()
        except Exception:
            pass



# Ensure that the CRS is EPSG:4326 as this is a prerequisite for database entry
def ensure_crs_4326(
    gdf: gpd.GeoDataFrame,
    *,
    assume_if_missing: Optional[str] = None,
    inplace: bool = False
) -> Tuple[gpd.GeoDataFrame, CRS]:
    """
    Validate a GeoDataFrame CRS and ensure it is EPSG:4326.

    Behavior:
    - If gdf.crs is None:
        - If assume_if_missing is provided (e.g., "EPSG:4326" or "EPSG:3857"),
          set CRS to that value (no coordinate change) and proceed.
        - Otherwise, raise a ValueError (recommended for safety).
    - If CRS exists and is not EPSG:4326:
        - Reproject to EPSG:4326 via to_crs("EPSG:4326").
    - If already EPSG:4326:
        - Return unchanged.

    Returns:
        (gdf_out, original_crs)

    Notes:
    - Setting CRS with set_crs() does NOT transform coordinates; it assigns metadata.
    - Reprojection happens only with to_crs().
    """
    if gdf is None or len(gdf) == 0:
        raise ValueError("GeoDataFrame is empty or None.")

    if gdf.geometry is None:
        raise ValueError("GeoDataFrame has no geometry column.")

    out = gdf if inplace else gdf.copy()

    original_crs = out.crs

    # Missing CRS: either assume or fail fast
    if out.crs is None:
        if not assume_if_missing:
            raise ValueError(
                "Input layer has no CRS (gdf.crs is None). "
                "Set assume_if_missing='EPSG:XXXX' if you want to assume a CRS, "
                "or fix the source file to include a CRS."
            )
        log.warning("Input CRS is missing; assuming %s (no coordinate transform).", assume_if_missing)
        out = out.set_crs(assume_if_missing, allow_override=True)
        original_crs = CRS.from_user_input(assume_if_missing)

    # Ensure CRS objects are comparable
    current = CRS.from_user_input(out.crs)
    target = CRS.from_epsg(4326)

    if current != target:
        log.info("Reprojecting from %s to EPSG:4326.", current.to_string())
        out = out.to_crs(target)
    else:
        log.info("CRS EPSG:4326 detected.")

    return out, CRS.from_user_input(original_crs) if original_crs else CRS.from_user_input(out.crs)


# Run code to load data into the table
load_new_table(
    file_path=input_path,
    target_table=target_table,
    geom_col=geom_col,
    schema=target_schema,
    srid=4326,
    drop_table_if_exists=drop_table_if_exists_bool
)

