from dataclasses import dataclass

from app.core.model_aliases import image_model_options, video_model_options


@dataclass(frozen=True)
class StoryboardField:
    name: str
    required: bool = False
    description: str = ""


STORYBOARD_FIELDS: tuple[StoryboardField, ...] = (
    StoryboardField("场景描述", True, "粗略分镜描述"),
    StoryboardField("参考图", False, "视觉方向参考"),
    StoryboardField("关键帧提示词", False, "核心画面 Prompt"),
    StoryboardField("首帧提示词", False, "镜头起始画面 Prompt"),
    StoryboardField("尾帧提示词", False, "镜头结束画面 Prompt"),
    StoryboardField("视频 Prompt", False, "送入视频模型的连续镜头 Prompt"),
    StoryboardField("负面 Prompt", False, "避免闪烁、变形、错字等"),
    StoryboardField("镜头运动", False, "推拉摇移等运动描述"),
    StoryboardField("一致性说明", False, "人物、服装、场景和光线一致性"),
    StoryboardField("文本模型", False, "Prompt 优化模型"),
    StoryboardField("生成批次", True, "批量操作筛选"),
    StoryboardField("首帧同步设置", True, "是否用上一镜尾帧作为本镜首帧"),
    StoryboardField("关键帧生成设置", True, "是否为当前镜头生成关键帧候选"),
    StoryboardField("图片模型", False, "默认 DashScope 万相，也可切换其他图片模型"),
    StoryboardField("图片生成状态", True, "图片生成控制"),
    StoryboardField("关键帧图", False, "AI 生成候选"),
    StoryboardField("选中关键帧图", False, "视频输入"),
    StoryboardField("首帧图", False, "视频输入"),
    StoryboardField("尾帧图", False, "视频输入"),
    StoryboardField("审核状态", True, "人工审核记录"),
    StoryboardField("生成状态", True, "视频生成控制"),
    StoryboardField("驳回原因", False, "驳回后重生成参考"),
    StoryboardField("需要重新生成的选项", False, "按驳回原因选择要重做的 Prompt、图片或视频"),
    StoryboardField("重新生成状态", True, "选择启动后按重生成选项执行"),
    StoryboardField("视频链接", False, "视频结果"),
    StoryboardField("视频模型", False, "视频生成模型"),
    StoryboardField("满意度", False, "满意/不满意"),
    StoryboardField("归档链接", False, "复用路径"),
    StoryboardField("错误信息", False, "失败排错"),
    StoryboardField("Prompt 版本", True, "旧任务防覆盖"),
    StoryboardField("任务 ID", False, "任务追踪"),
    StoryboardField("视频存储位置", False, "可选：填写飞书文件夹链接后视频写入该文件夹"),
)


def build_field_map(fields_response: dict) -> dict[str, str]:
    items = fields_response.get("data", {}).get("items", [])
    return {item.get("field_name"): item.get("field_id") for item in items if item.get("field_name") and item.get("field_id")}


def validate_required_fields(field_map: dict[str, str]) -> list[str]:
    return [field.name for field in STORYBOARD_FIELDS if field.required and field.name not in field_map]


FIELD_TYPE_TEXT = 1
FIELD_TYPE_NUMBER = 2
FIELD_TYPE_SINGLE_SELECT = 3
FIELD_TYPE_MULTI_SELECT = 4
FIELD_TYPE_URL = 15
FIELD_TYPE_ATTACHMENT = 17


def bitable_field_definitions() -> list[dict]:
    attachment_fields = {"参考图", "关键帧图", "选中关键帧图", "首帧图", "尾帧图"}
    url_fields = {"视频链接", "归档链接", "视频存储位置"}
    single_select_fields = {
        "审核状态",
        "首帧同步设置",
        "关键帧生成设置",
        "图片生成状态",
        "生成状态",
        "重新生成状态",
        "满意度",
        "图片模型",
        "文本模型",
        "视频模型",
    }
    multi_select_fields = {"需要重新生成的选项"}
    number_fields = {"Prompt 版本"}
    fields = []
    for field in STORYBOARD_FIELDS:
        if field.name in attachment_fields:
            field_type = FIELD_TYPE_ATTACHMENT
        elif field.name in url_fields:
            field_type = FIELD_TYPE_URL
        elif field.name in single_select_fields:
            field_type = FIELD_TYPE_SINGLE_SELECT
        elif field.name in multi_select_fields:
            field_type = FIELD_TYPE_MULTI_SELECT
        elif field.name in number_fields:
            field_type = FIELD_TYPE_NUMBER
        else:
            field_type = FIELD_TYPE_TEXT
        item = {"field_name": field.name, "type": field_type}
        if field.name == "审核状态":
            item["property"] = {
                "options": [
                    {"name": "草稿"},
                    {"name": "待优化"},
                    {"name": "优化中"},
                    {"name": "待生成帧"},
                    {"name": "帧生成中"},
                    {"name": "待审核"},
                    {"name": "通过"},
                    {"name": "驳回"},
                    {"name": "视频生成中"},
                    {"name": "待验收"},
                    {"name": "已归档-满意"},
                    {"name": "已归档-不满意"},
                ]
            }
        elif field.name == "首帧同步设置":
            item["property"] = {"options": [{"name": "否"}, {"name": "是"}]}
        elif field.name == "关键帧生成设置":
            item["property"] = {"options": [{"name": "否"}, {"name": "是"}]}
        elif field.name in {"图片生成状态", "生成状态", "重新生成状态"}:
            item["property"] = {"options": [{"name": "未开始"}, {"name": "启动"}, {"name": "正在生成"}, {"name": "生成完成"}]}
        elif field.name == "需要重新生成的选项":
            item["property"] = {
                "options": [
                    {"name": "关键帧提示词"},
                    {"name": "首帧提示词"},
                    {"name": "尾帧提示词"},
                    {"name": "视频提示词"},
                    {"name": "关键帧重新生成"},
                    {"name": "首帧重新生成"},
                    {"name": "尾帧重新生成"},
                    {"name": "视频重新生成"},
                ]
            }
        elif field.name == "满意度":
            item["property"] = {"options": [{"name": "满意"}, {"name": "不满意"}]}
        elif field.name == "文本模型":
            item["property"] = {
                "options": [
                    {"name": "qwen-plus"},
                    {"name": "qwen-max"},
                    {"name": "gpt-5.4"},
                    {"name": "deepseek-v4-pro"},
                    {"name": "deepseek-v4-flash"},
                    {"name": "google/gemini-3.1-pro-preview"},
                    {"name": "google/gemini-3.1-flash-lite-preview"},
                ]
            }
        elif field.name == "图片模型":
            item["property"] = {"options": [{"name": name} for name in image_model_options()]}
        elif field.name == "视频模型":
            item["property"] = {"options": [{"name": name} for name in video_model_options()]}
        fields.append(item)
    return fields
