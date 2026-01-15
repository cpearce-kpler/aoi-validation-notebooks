# aoi-validation-notebooks
This repository contains parameterised Jupyter notebooks to validate, process, and upload Areas of Interest (AOIs) into geospatial data tables. The notebooks support multiple AOI layers and environments, use the key fields ZONE_TYPE and NAME as distinguishing fields, and are designed for interactive use via Jupyter or QGIS.

# Running the script (recommended approach)
# The recommended approach is to ensure that the correct Python interpreter, packages, and .env file are used
1. Use the .env file to emter the correct variables. This will include usernames and passwords for database access
2. Open Anaconda Prompt
3. Convert .ipynb file/s to .py files. One example of how this can be done in Anaconda Prompt is to use this code: python -m jupytext --to py add_data_to_ma.ipynb
4. Activate the environment (if needed)
5. `cd` into the project directory e.g. cd "C:\Users\Craig Pearce\Desktop\github"
6. Run: python add_data_to_ma.py
