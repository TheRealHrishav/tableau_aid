import os
import re
import zipfile
import shutil

def process_twbx_calculations(input_twbx_path, output_twbx_path, calc_mappings):
    """
    Unzips a .twbx file, removes multiple calculation metadata blocks from the inner .twb file,
    re-maps their dependencies, and packages everything back into a new .twbx file.

    :param input_twbx_path: Path to the source .twbx workbook file.
    :param output_twbx_path: Destination path for the modified output .twbx workbook.
    :param calc_mappings: Dictionary format: {"Calculation_ID": "Replacement_Field"}
    """
    # Temporary directory names for extraction
    extract_dir = "temp_twbx_extraction"

    # 1. Clean up any leftover temporary directories from a previous run
    if os.path.exists(extract_dir):
        shutil.rmtree(extract_dir)

    print(f"Extracting packaged workbook: {input_twbx_path}")
    with zipfile.ZipFile(input_twbx_path, 'r') as zip_ref:
        zip_ref.extractall(extract_dir)

    # 2. Locate the main .twb workbook file inside the extracted folder structure
    twb_file_name = None
    for root, dirs, files in os.walk(extract_dir):
        for file in files:
            if file.endswith('.twb'):
                twb_file_name = os.path.join(root, file)
                break
        if twb_file_name:
            break

    if not twb_file_name:
        raise FileNotFoundError("Could not find an underlying .twb file inside the provided .twbx container.")

    print(f"Found underlying layout file: {twb_file_name}")

    # 3. Read the plain-text XML content of the .twb file
    with open(twb_file_name, 'r', encoding='utf-8') as f:
        content = f.read()

    # 4. Iteratively execute the transformation loops over the string payload
    for calc_id, replacement_id in calc_mappings.items():
        print(f"\n>>> Running transformation pipeline for: {calc_id}")

        # Stage A: Purge the primary metadata block for the current calculation ID
        calc_def_pattern = rf"\s*<column[^>]*name='\[{calc_id}\]'[^>]*>\s*<calculation[^>]*/>\s*</column>"
        content, metadata_count = re.subn(calc_def_pattern, "", content, flags=re.DOTALL)
        print(f"    Purged {metadata_count} matching column metadata block(s).")

        # Stage B: Inline search-and-replace all visualization markup references
        content, tracking_count = re.subn(rf"\b{calc_id}\b", replacement_id, content)
        print(f"    Re-mapped {tracking_count} reference instance(s) inside view dependency trees.")

    # 5. Overwrite the inner .twb file with our updated script variations
    with open(twb_file_name, 'w', encoding='utf-8') as f:
        f.write(content)
    print("\nInner .twb transformation successful.")

    # 6. Re-package the entire updated folder tree back into a brand new zip archive (.twbx)
    print(f"Packaging files back up into: {output_twbx_path}")
    with zipfile.ZipFile(output_twbx_path, 'w', zipfile.ZIP_DEFLATED) as zip_out:
        for root, dirs, files in os.walk(extract_dir):
            for file in files:
                full_path = os.path.join(root, file)
                # Compute relative path inside the zip file so the directory structure isn't broken
                relative_path = os.path.relpath(full_path, extract_dir)
                zip_out.write(full_path, relative_path)

    # 7. Final environmental cleanup
    shutil.rmtree(extract_dir)
    print("\nBatch processing execution and packaging completed successfully!")
