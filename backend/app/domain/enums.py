from enum import Enum


class StrEnum(str, Enum):
    pass


class ShotStatus(StrEnum):
    DRAFT = "draft"
    PENDING_PROMPT = "pending_prompt"
    PROMPT_OPTIMIZING = "prompt_optimizing"
    PENDING_FRAMES = "pending_frames"
    FRAMES_GENERATING = "frames_generating"
    FRAME_PARTIAL_FAILED = "frame_partial_failed"
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    VIDEO_QUEUED = "video_queued"
    VIDEO_GENERATING = "video_generating"
    VIDEO_FAILED = "video_failed"
    PENDING_ACCEPTANCE = "pending_acceptance"
    ARCHIVED_SATISFIED = "archived_satisfied"
    ARCHIVED_UNSATISFIED = "archived_unsatisfied"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RETRYING = "retrying"
    STALE = "stale"
    CANCELLED = "cancelled"


class JobType(StrEnum):
    PROMPT_OPTIMIZE = "prompt_optimize"
    IMAGE_GENERATE = "image_generate"
    VIDEO_GENERATE = "video_generate"
    VIDEO_POLL = "video_poll"
    FEISHU_BACKFILL = "feishu_backfill"
    ARCHIVE_VIDEO = "archive_video"


class FrameType(StrEnum):
    KEYFRAME = "keyframe"
    FIRST_FRAME = "first_frame"
    LAST_FRAME = "last_frame"


class AssetType(StrEnum):
    REFERENCE = "reference"
    KEYFRAME = "keyframe"
    FIRST_FRAME = "first_frame"
    LAST_FRAME = "last_frame"
    VIDEO = "video"
    ARCHIVE = "archive"


class Satisfaction(StrEnum):
    SATISFIED = "satisfied"
    UNSATISFIED = "unsatisfied"
