from app.adapters.feishu_fields import bitable_field_definitions, build_field_map, validate_required_fields


def test_validate_required_fields():
    response = {
        "data": {
            "items": [
                {"field_name": "场景描述", "field_id": "fld1"},
                {"field_name": "生成批次", "field_id": "fld2"},
                {"field_name": "首帧同步设置", "field_id": "fld3"},
                {"field_name": "关键帧生成设置", "field_id": "fld4"},
                {"field_name": "审核状态", "field_id": "fld5"},
                {"field_name": "图片生成状态", "field_id": "fld6"},
                {"field_name": "生成状态", "field_id": "fld7"},
                {"field_name": "重新生成状态", "field_id": "fld8"},
                {"field_name": "Prompt 版本", "field_id": "fld9"},
            ]
        }
    }
    assert validate_required_fields(build_field_map(response)) == []


def test_reference_image_notes_field_is_available_as_text():
    definitions = bitable_field_definitions()
    field = next(item for item in definitions if item["field_name"] == "参考图批注")

    assert field["type"] == 1


def test_keyframe_time_and_video_duration_are_number_fields():
    definitions = bitable_field_definitions()
    by_name = {item["field_name"]: item for item in definitions}

    assert by_name["关键帧时间点"]["type"] == 2
    assert by_name["视频时长"]["type"] == 2
