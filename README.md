# aoi-validation-notebooks
This repository contains parameterised Jupyter notebooks to validate, process, and upload Areas of Interest (AOIs) into geospatial data tables. The notebooks support multiple AOI layers and environments, use the key fields ZONE_TYPE and NAME as distinguishing fields, and are designed for interactive use via Jupyter or QGIS.

# Running the script (recommended approach)
The recommended approach is to ensure that the correct Python interpreter, packages, and .env file are used and this utilises Anaconda
1. Open Anaconda Prompt
2. Activate your environment if you have a preferred environment, or else skip this step
3. Run the following (or a subset of this as this runs slowly) if these packages are not installed in your Anaconda environment. This code creates a new environment, so you may choose to adjust the code to your preferred environment:
   conda create -n mt-aoi -c conda-forge pandas numpy geopandas shapely pyproj python-dotenv psycopg2 pyodbc sqlalchemy
   conda activate mt-aoi
5. Use the .env file to enter the correct variables. A .env.example file has also been provided to show an example of how this file should be completed. This will include usernames and passwords for database access
6. `cd` into the project directory e.g. cd "C:\Users\Craig Pearce\Desktop\github_aoi"
7. Run the following to enter data into a new data table in the maritime_assets database (default schema is ancillary):
   run_add_data_to_ma.bat
8. Run the following to enter data into ais_replica.dbo.AREAS_STATIC in dbdev01 or dbprim03 environments: (remember to set DEV or PRIM in the .env file):
   python upload_aoi_to_mt.py
   
