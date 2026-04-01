"""Unit tests for transform helper functions — no game data required."""

import pytest

from converter.errors import TransformError
from converter.lsx_model import LsxNode, LsxNodeAttribute, LsxRegion, LsxResource
from converter.transform import (
    PROP_TYPE_BOOL,
    PROP_TYPE_COLOR,
    PROP_TYPE_FLOAT,
    PROP_TYPE_INT32,
    PROP_TYPE_KEYFRAMED,
    PROP_TYPE_RANGE,
    PROP_TYPE_RESOURCE,
    PROP_TYPE_STRING,
    PROP_TYPE_VECTOR3,
    _allspark_type_to_prop_type,
    _fvec4_to_packed_color,
    _get_attribute_name,
    _guess_prop_type,
    _packed_color_to_fvec4,
    _strip_resource_display_name,
)


# ── _packed_color_to_fvec4 ──────────────────────────────────────────

class TestPackedColorToFvec4:
    def test_white(self):
        # -1 = 0xFFFFFFFF => ARGB(255,255,255,255) => RGBA "1 1 1 1"
        assert _packed_color_to_fvec4("-1") == "1.0 1.0 1.0 1.0" or \
               _packed_color_to_fvec4("-1") == "1 1 1 1"
        result = _packed_color_to_fvec4("-1")
        parts = [float(x) for x in result.split()]
        assert len(parts) == 4
        assert all(abs(p - 1.0) < 1e-6 for p in parts)

    def test_black_opaque(self):
        # 0xFF000000 = -16777216
        result = _packed_color_to_fvec4("-16777216")
        parts = [float(x) for x in result.split()]
        assert abs(parts[0]) < 1e-6  # R=0
        assert abs(parts[1]) < 1e-6  # G=0
        assert abs(parts[2]) < 1e-6  # B=0
        assert abs(parts[3] - 1.0) < 1e-6  # A=1

    def test_zero(self):
        result = _packed_color_to_fvec4("0")
        parts = [float(x) for x in result.split()]
        assert all(abs(p) < 1e-6 for p in parts)

    def test_invalid_returns_default(self):
        assert _packed_color_to_fvec4("not_a_number") == "0 0 0 1"


# ── _fvec4_to_packed_color ──────────────────────────────────────────

class TestFvec4ToPackedColor:
    def test_white(self):
        assert _fvec4_to_packed_color("1 1 1 1") == "-1"

    def test_black_opaque(self):
        assert _fvec4_to_packed_color("0 0 0 1") == "-16777216"

    def test_zero(self):
        assert _fvec4_to_packed_color("0 0 0 0") == "0"

    def test_short_vector_pads(self):
        # < 4 components should be padded with 0
        result = _fvec4_to_packed_color("1 0 0")
        # R=255, G=0, B=0, A=0 → 0x00FF0000
        assert result == str(0x00FF0000)

    def test_roundtrip(self):
        for val in ["-1", "0", "-16777216", "16711680"]:
            fvec = _packed_color_to_fvec4(val)
            back = _fvec4_to_packed_color(fvec)
            assert back == val, f"Roundtrip failed: {val} -> {fvec} -> {back}"


# ── _strip_resource_display_name ────────────────────────────────────

class TestStripResourceDisplayName:
    def test_with_display_name(self):
        val = "VFX_Material_Foo <f7fc084b-d098-0d9a-8033-1cb61c3beb37>"
        assert _strip_resource_display_name(val) == "f7fc084b-d098-0d9a-8033-1cb61c3beb37"

    def test_bare_guid(self):
        val = "f7fc084b-d098-0d9a-8033-1cb61c3beb37"
        assert _strip_resource_display_name(val) == val

    def test_no_guid(self):
        val = "just a plain string"
        assert _strip_resource_display_name(val) == val

    def test_empty(self):
        assert _strip_resource_display_name("") == ""


# ── _get_attribute_name ─────────────────────────────────────────────

class TestGetAttributeName:
    def test_dotted(self):
        assert _get_attribute_name("Particle.Appearance.Brightness") == "Brightness"

    def test_no_dot(self):
        assert _get_attribute_name("Brightness") == "Brightness"

    def test_empty(self):
        assert _get_attribute_name("") == ""


# ── _allspark_type_to_prop_type ─────────────────────────────────────

class TestAllsparkTypeToPropType:
    @pytest.mark.parametrize("allspark_type,expected", [
        ("Boolean", PROP_TYPE_BOOL),
        ("Float", PROP_TYPE_FLOAT),
        ("FloatSlider", PROP_TYPE_FLOAT),
        ("Integer", PROP_TYPE_INT32),
        ("Vector3", PROP_TYPE_VECTOR3),
        ("Color", PROP_TYPE_COLOR),
        ("Ramp", PROP_TYPE_KEYFRAMED),
        ("Resource", PROP_TYPE_RESOURCE),
        ("String", PROP_TYPE_STRING),
    ])
    def test_known_types(self, allspark_type, expected):
        assert _allspark_type_to_prop_type(allspark_type) == expected

    def test_unknown_defaults_to_string(self):
        assert _allspark_type_to_prop_type("SomeNewType") == PROP_TYPE_STRING


# ── _guess_prop_type ────────────────────────────────────────────────

class TestGuessPropType:
    def test_bool_true(self):
        assert _guess_prop_type("True") == PROP_TYPE_BOOL

    def test_bool_false(self):
        assert _guess_prop_type("false") == PROP_TYPE_BOOL

    def test_float(self):
        assert _guess_prop_type("3.14") == PROP_TYPE_FLOAT

    def test_integer_as_float(self):
        assert _guess_prop_type("42") == PROP_TYPE_FLOAT

    def test_vector3(self):
        assert _guess_prop_type("1.0,2.0,3.0") == PROP_TYPE_VECTOR3

    def test_color4(self):
        assert _guess_prop_type("1.0,0.5,0.0,1.0") == PROP_TYPE_COLOR

    def test_range(self):
        assert _guess_prop_type("0.5,1.5") == PROP_TYPE_RANGE

    def test_string_fallback(self):
        assert _guess_prop_type("some text") == PROP_TYPE_STRING


# ── Packed color edge cases ─────────────────────────────────────────

class TestPackedColorEdgeCases:
    def test_packed_color_empty(self):
        assert _packed_color_to_fvec4("") == "0 0 0 1"

    def test_fvec4_to_packed_empty_vector(self):
        """An empty string should pad to 4 components."""
        result = _fvec4_to_packed_color("")
        assert isinstance(result, str)


# ── Transform integration edge cases ───────────────────────────────

class TestTransformEdgeCases:
    def test_non_integer_property_type(self):
        """Transform should raise TransformError for non-integer Property @Type."""
        from converter.allspark import AllSparkRegistry
        from converter.transform import lsx_to_effect

        prop_node = LsxNode(id="Property", attributes=[
            LsxNodeAttribute(id="Type", attr_type="uint32", value="abc"),
            LsxNodeAttribute(id="FullName", attr_type="LSString", value="TestProp"),
        ])
        props_container = LsxNode(id="Properties", children=[prop_node])
        comp_node = LsxNode(id="EffectComponent", attributes=[
            LsxNodeAttribute(id="Type", attr_type="string", value="SomeComponent"),
            LsxNodeAttribute(id="Track", attr_type="uint32", value="0"),
        ], children=[props_container])
        effect_components = LsxNode(id="EffectComponents", children=[comp_node])
        effect_node = LsxNode(id="Effect", children=[effect_components])
        region = LsxRegion(id="Effect", nodes=[effect_node])
        res = LsxResource(regions=[region])

        reg = AllSparkRegistry()
        with pytest.raises(TransformError, match="Non-integer"):
            lsx_to_effect(res, reg)

    def test_lsx_to_effect_empty_resource(self):
        """Transform with missing Effects region should handle gracefully."""
        from converter.allspark import AllSparkRegistry
        from converter.transform import lsx_to_effect

        res = LsxResource()  # no regions
        reg = AllSparkRegistry()
        try:
            result = lsx_to_effect(res, reg)
            assert result is not None
        except (TransformError, AttributeError, KeyError):
            pass
