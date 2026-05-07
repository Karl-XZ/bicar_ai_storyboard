from app.adapters.feishu_fields import build_field_map, validate_required_fields


def test_validate_required_fields():
    response = {
        "data": {
            "items": [
                {"field_name": "镜号", "field_id": "fld1"},
                {"field_name": "场景描述", "field_id": "fld2"},
                {"field_name": "生成批次", "field_id": "fld3"},
                {"field_name": "审核状态", "field_id": "fld4"},
                {"field_name": "图片生成状态", "field_id": "fld5"},
                {"field_name": "生成状态", "field_id": "fld6"},
                {"field_name": "Prompt 版本", "field_id": "fld7"},
            ]
        }
    }
    assert validate_required_fields(build_field_map(response)) == []
