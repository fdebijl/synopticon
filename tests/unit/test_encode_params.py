"""encode_params: Synology's wire-format quirks, verified byte-for-byte against
the captured `add_face` HAR payload (adding_face_to_photo_without_face.har, entry 8)."""

from __future__ import annotations

from synopticon.syno.client import QuotedString, encode_params
from synopticon.syno.models import BBox, Point


def test_scalars_and_none_dropped():
    out = encode_params(
        {"version": 3, "target_id": 9702, "ratio": 1.5, "flag": True, "off": False, "missing": None}
    )
    assert out["version"] == "3"
    assert out["target_id"] == "9702"
    assert out["ratio"] == "1.5"
    assert out["flag"] == "true"
    assert out["off"] == "false"
    assert "missing" not in out


def test_list_and_dict_compact_json():
    out = encode_params({"id": [2660], "additional": ["thumbnail"]})
    assert out["id"] == "[2660]"
    assert out["additional"] == '["thumbnail"]'


def test_quoted_string_matches_capture():
    # requests.md: name_prefix="F" (suggest), name="Sofia Alario" (merge).
    assert encode_params({"name_prefix": QuotedString("F")})["name_prefix"] == '"F"'
    assert encode_params({"name": QuotedString("Sofia Alario")})["name"] == '"Sofia Alario"'


def test_plain_str_passed_through_unquoted():
    # api/method ride bare in body-form POSTs; only QuotedString-wrapped values get quoted.
    out = encode_params({"api": "SYNO.Foto.Browse.Person", "method": "add_face"})
    assert out["api"] == "SYNO.Foto.Browse.Person"
    assert out["method"] == "add_face"


def test_add_face_payload_byte_exact_against_har():
    # adding_face_to_photo_without_face.har entry 8, decoded `face=` value.
    face = [
        {
            "face_bounding_box": {
                "top_left": {"x": 0.4008430182190183, "y": 0.23976940730726903},
                "bottom_right": {"x": 0.6994280869527612, "y": 0.46366557859071184},
            },
            "face_id_temp": "103153-0",
            "person_id": 2660,
        }
    ]
    out = encode_params({"face": face, "id_item": 103153})
    expected_face = (
        '[{"face_bounding_box":{"top_left":{"x":0.4008430182190183,"y":0.23976940730726903},'
        '"bottom_right":{"x":0.6994280869527612,"y":0.46366557859071184}},'
        '"face_id_temp":"103153-0","person_id":2660}]'
    )
    assert out["face"] == expected_face
    assert out["id_item"] == "103153"


def test_bbox_dataclass_matches_equivalent_raw_dict():
    bbox = BBox(top_left=Point(x=0.1, y=0.2), bottom_right=Point(x=0.3, y=0.4))
    via_dataclass = encode_params(
        {"face": [{"face_bounding_box": bbox, "face_id_temp": "1-0", "person_id": 1}]}
    )
    via_raw_dict = encode_params(
        {
            "face": [
                {
                    "face_bounding_box": {
                        "top_left": {"x": 0.1, "y": 0.2},
                        "bottom_right": {"x": 0.3, "y": 0.4},
                    },
                    "face_id_temp": "1-0",
                    "person_id": 1,
                }
            ]
        }
    )
    assert via_dataclass["face"] == via_raw_dict["face"]
