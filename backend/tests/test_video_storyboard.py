import asyncio

from app.services.bot_commands import _parse_video_storyboard_command
from app.services.video_storyboard import VideoStoryboardService, VideoStoryboardShot
from tests.test_feishu_storyboard_flow import FakeFeishuClient, make_db


def test_parse_video_storyboard_command_is_explicit_only():
    assert _parse_video_storyboard_command("参考这个视频为我出一个分镜脚本模板") is None

    command = _parse_video_storyboard_command(
        "视频拆分镜 视频=https://ocnwptzvwvt6.feishu.cn/file/O6qLblRTnoHMykxCRSxcnDJinje "
        "项目名=萨博900广告 镜头数=8 抽帧数=12 "
        "目录=https://ocnwptzvwvt6.feishu.cn/drive/folder/TcAUfNw3nlk8eTdrPWxc0kK3nJe"
    )

    assert command
    assert command.video_reference == "https://ocnwptzvwvt6.feishu.cn/file/O6qLblRTnoHMykxCRSxcnDJinje"
    assert command.project_name == "萨博900广告"
    assert command.target_shots == 8
    assert command.sample_count == 12
    assert command.parent_folder_url == "https://ocnwptzvwvt6.feishu.cn/drive/folder/TcAUfNw3nlk8eTdrPWxc0kK3nJe"


def test_video_storyboard_service_creates_project_and_populates_rows(monkeypatch):
    async def run_flow():
        db = make_db()
        fake = FakeFeishuClient()
        service = VideoStoryboardService(db, feishu=fake)

        async def fake_download(reference: str):
            assert reference == "https://ocnwptzvwvt6.feishu.cn/file/video_token"
            return "萨博900广告.mp4", b"video bytes", "video/mp4"

        async def fake_extract(video_path, output_dir, *, sample_count: int):
            assert sample_count == 4
            output_dir.mkdir(parents=True, exist_ok=True)
            frames = []
            for index in range(1, 5):
                path = output_dir / f"frame_{index:03d}.jpg"
                path.write_bytes(b"jpg")
                frames.append(path)
            return frames

        async def fake_analyze(frames, *, source_filename: str, target_shots: int, model: str):
            assert source_filename == "萨博900广告.mp4"
            assert target_shots == 4
            assert model
            assert len(frames) == 4
            return [
                VideoStoryboardShot(
                    scene_description="黑底中，萨博900车身从暗处浮现",
                    first_frame_prompt="黑色背景，车灯未亮",
                    last_frame_prompt="车灯亮起，轮廓浮现",
                    video_prompt="镜头缓慢推近，车身从暗处出现",
                    camera_motion="缓慢推近",
                    consistency_notes="根据抽样画面推断，黑金复古广告质感",
                ),
                VideoStoryboardShot(
                    scene_description="车身细节快速切换，金属反光",
                    first_frame_prompt="钣金细节特写",
                    last_frame_prompt="徽标反光特写",
                    video_prompt="微距切换车身细节，强调复古质感",
                    camera_motion="微距横移",
                    consistency_notes="保持暗调与硬光",
                ),
            ]

        monkeypatch.setattr(service, "_download_video", fake_download)
        monkeypatch.setattr(service, "_extract_frames", fake_extract)
        monkeypatch.setattr(service, "_analyze_frames", fake_analyze)

        result = await service.create_project_from_video(
            video_reference="https://ocnwptzvwvt6.feishu.cn/file/video_token",
            project_name="萨博900广告",
            chat_id="oc_test",
            sample_count=4,
            target_shots=4,
        )

        assert result.project.name == "萨博900广告"
        assert result.table_url == "https://feishu.test/base/app_001?table=tbl_001"
        assert result.shot_count == 2
        assert result.frame_count == 4
        assert fake.records[0]["fields"]["场景描述"] == "黑底中，萨博900车身从暗处浮现"
        assert fake.records[0]["fields"]["视频 Prompt"] == "镜头缓慢推近，车身从暗处出现"
        assert fake.records[1]["fields"]["场景描述"] == "车身细节快速切换，金属反光"
        assert fake.records[1]["fields"]["镜头运动"] == "微距横移"

    asyncio.run(run_flow())
