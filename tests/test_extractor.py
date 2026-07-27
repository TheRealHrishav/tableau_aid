import zipfile

import pytest

from graph_smith.extractor import extract_and_parse, extract_twbx, parse_twb_file

TWB_XML = """<?xml version='1.0' encoding='utf-8' ?>
<workbook>
  <datasources>
    <datasource>
      <column name="[Calc_1]" caption="Total Sales" datatype="real" role="measure">
        <calculation formula="[Sales] + [Tax]" />
      </column>
      <column name="[Region]" caption="Region" datatype="string" role="dimension">
      </column>
      <column name="[Calc_2]" datatype="integer" role="measure">
        <calculation formula="1" />
      </column>
    </datasource>
  </datasources>
</workbook>
"""


@pytest.fixture
def twbx_path(tmp_path):
    twb_file = tmp_path / "workbook.twb"
    twb_file.write_text(TWB_XML, encoding="utf-8")

    twbx_file = tmp_path / "workbook.twbx"
    with zipfile.ZipFile(twbx_file, "w") as zf:
        zf.write(twb_file, arcname="workbook.twb")

    return twbx_file


def test_extract_twbx_unzips_to_target_folder(twbx_path, tmp_path):
    extract_dir = tmp_path / "extracted"
    result = extract_twbx(str(twbx_path), str(extract_dir))

    assert result == str(extract_dir)
    assert (extract_dir / "workbook.twb").exists()


def test_parse_twb_file_separates_calculated_and_non_calculated_fields(tmp_path):
    twb_file = tmp_path / "workbook.twb"
    twb_file.write_text(TWB_XML, encoding="utf-8")

    calculated, non_calculated = parse_twb_file(str(twb_file))

    assert len(calculated) == 2
    assert len(non_calculated) == 1

    total_sales = next(f for f in calculated if f["internal_name"] == "[Calc_1]")
    assert total_sales["field_name"] == "Total Sales"
    assert total_sales["formula"] == "[Sales] + [Tax]"
    assert total_sales["datatype"] == "real"

    region = non_calculated[0]
    assert region["field_name"] == "Region"
    assert region["mesure_or_dimension"] == "dimension"


def test_parse_twb_file_falls_back_to_internal_name_when_no_caption(tmp_path):
    twb_file = tmp_path / "workbook.twb"
    twb_file.write_text(TWB_XML, encoding="utf-8")

    calculated, _ = parse_twb_file(str(twb_file))

    calc_2 = next(f for f in calculated if f["internal_name"] == "[Calc_2]")
    assert calc_2["field_name"] == "Calc_2"


def test_extract_and_parse_infers_twb_name_from_twbx_path(twbx_path, tmp_path):
    extract_dir = tmp_path / "extracted"
    calculated, non_calculated = extract_and_parse(str(twbx_path), str(extract_dir))

    assert len(calculated) == 2
    assert len(non_calculated) == 1
