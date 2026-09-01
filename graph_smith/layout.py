# ==========================================
# 1. The Core Normalization Engine
# ==========================================
def get_base_name(col_name):
    """
    Strips ALL Tableau namespaces, aggregations, and brackets 
    to return the naked field name.
    """
    if not col_name:
        return None
    
    # Step A: Remove datasource prefix if it exists 
    # Example: '[federated.123].[none:Rep segment:nk]' -> '[none:Rep segment:nk]'
    if '].[' in col_name:
        col_name = col_name.split('].[')[-1]
        # Re-add opening bracket if split removed it
        if not col_name.startswith('['):
            col_name = '[' + col_name
            
    # Step B: Extract the core text from inside Tableau's aggregation/type syntax
    # Example: '[none:Rep segment:nk]' -> 'Rep segment'
    m = re.search(r'\[[^:]+:([^:]+):[^\]]*\]', col_name)
    if m:
        return m.group(1)
    
    # Step C: Fallback - just strip all brackets
    # Example: '[Rep segment]' -> 'Rep segment'
    return col_name.strip('[]')

# ==========================================
# 2. Extract and Parse
# ==========================================
twbx_file = ""
extract_dir = "extracted"
twb_file_path = f"{extract_dir}/"

with zipfile.ZipFile(twbx_file, 'r') as zip_ref:
    zip_ref.extractall(extract_dir)

tree = ET.parse(twb_file_path)
root = tree.getroot()

# ==========================================
# 3. Build Global Metadata Lookup
# ==========================================
field_lookup = {}
for col in root.findall(".//column"):
    name = col.get("name")
    if name:
        base = get_base_name(name)
        # Save first occurrence or overwrite if it has a caption (friendly display name)
        if base not in field_lookup or col.get("caption"):
            field_lookup[base] = {
                "field_name": col.get("caption") or base,
                "datatype": col.get("datatype"),
                "role": col.get("role")
            }

# ==========================================
# 4. Map Worksheets & Dashboard (Updated for Slices)
# ==========================================
fw_rows = []
ws_slices = set() # We will capture our slice fields here

for ws in root.findall(".//worksheet"):
    ws_name = ws.get("name")
    ws_fields_used = set()
    
    # A. Get fields actively used in the visual shelf
    for ci in ws.findall(".//column-instance"):
        col = ci.get("column")
        if col: ws_fields_used.add(get_base_name(col))
        
    # B. Get fields used in <slices> instead of filters
    slices_node = ws.find(".//slices")
    if slices_node is not None:
        # Note: slices use the inner text of the <column> tag, not an attribute
        for col_node in slices_node.findall("column"):
            col_text = col_node.text
            if col_text:
                base_name = get_base_name(col_text.strip())
                if base_name:
                    ws_fields_used.add(base_name)
                    # Register it globally as a slice/filter for this worksheet
                    ws_slices.add((ws_name, base_name))
            
    # C. Build the Worksheet dataframe rows
    for base in ws_fields_used:
        meta = field_lookup.get(base, {})
        fw_rows.append({
            "worksheet_name": ws_name,
            "internal_name": base, # The naked text, e.g., 'Rep segment'
            "field_name": meta.get("field_name", base),
            "datatype": meta.get("datatype"),
            "role": meta.get("role")
        })

df_fw = pd.DataFrame(fw_rows)

# Map Dashboards
dw_rows = []
for dash in root.findall(".//dashboard"):
    dash_name = dash.get("name")
    worksheets = set()
    for zone in dash.findall(".//zone"):
        ws = zone.get("name")
        if ws: worksheets.add(ws)
        
    for ws in worksheets:
        dw_rows.append({
            "dashboard_name": dash_name,
            "worksheet_name": ws
        })

df_dw = pd.DataFrame(dw_rows)

# ==========================================
# 5. Merge and Flag
# ==========================================
# Merge Field-Worksheet data with Dashboard-Worksheet data
df_final = df_fw.merge(df_dw, on="worksheet_name", how="left")

# Check our tuple against the ws_slices set using the naked internal name
df_final["is_filter"] = df_final.apply(
    lambda row: (row["worksheet_name"], row["internal_name"]) in ws_slices, 
    axis=1
)

# Test Output
#test_output = df_final[df_final['worksheet_name'] == '$ D-1 1.3  inf arr']
df_final.drop_duplicates()