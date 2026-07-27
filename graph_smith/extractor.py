# Data Mart/twbx_extractor.py
import zipfile
import xml.etree.ElementTree as ET
import os

def extract_twbx(twbx_path, extract_to="extracted"):
    """
    Extracts the .twbx file contents to the specified folder.

    Args:
        twbx_path (str): Path to the .twbx file.
        extract_to (str): Directory to extract files into.

    Returns:
        str: Path to the extracted folder.
    """
    with zipfile.ZipFile(twbx_path, 'r') as zip_ref:
        zip_ref.extractall(extract_to)
    return extract_to

def parse_twb_file(twb_file_path):
    """
    Parses the .twb XML file to extract calculated and non-calculated fields.

    Args:
        twb_file_path (str): Path to the .twb XML file.

    Returns:
        tuple: (calculated_fields, non_calculated_fields) lists of dictionaries with field info.
    """
    tree = ET.parse(twb_file_path)
    root = tree.getroot()

    calculated_fields = []
    non_calculated_fields = []

    for col in root.findall(".//column"):
        calc = col.find("calculation")
        internal_name = col.get("name")
        # Tableau displays columns without a caption using the internal name.
        field_name = col.get("caption") or (internal_name or "").strip("[]")
        if calc is not None:
            calculated_fields.append({
                "field_name": field_name,
                "internal_name": internal_name,
                "formula": calc.get("formula"),
                "datatype": col.get("datatype")
            })
        else:
            non_calculated_fields.append({
                "field_name": field_name,
                "internal_name": internal_name,
                "datatype": col.get("datatype"),
                "mesure_or_dimension": col.get("role")
            })

    return calculated_fields, non_calculated_fields

def extract_and_parse(twbx_path, extract_folder="extracted", twb_relative_path=None):
    """
    Full process: extract the .twbx file and parse the contained .twb file.

    Args:
        twbx_path (str): Path to the .twbx file.
        extract_folder (str): Folder to extract contents into.
        twb_relative_path (str): Relative path of the .twb file inside the extracted folder.
                                If None, will be inferred from twbx_path by replacing .twbx with .twb.

    Returns:
        tuple: (calculated_fields, non_calculated_fields)
    """
    if twb_relative_path is None:
        x = os.path.basename(twbx_path)
        twb_relative_path = x.rsplit(".", 1)[0] + '.twb'

    # your extraction and parsing code here
    extract_twbx(twbx_path, extract_folder)
    calculated_fields, non_calculated_fields = parse_twb_file(os.path.join(extract_folder, twb_relative_path))
    return calculated_fields, non_calculated_fields
