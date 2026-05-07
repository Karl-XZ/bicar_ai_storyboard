import hashlib
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.domain.enums import AssetType, Satisfaction
from app.models.asset import Asset


class AssetService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def put_bytes(
        self,
        *,
        project_id: UUID,
        shot_id: UUID | None,
        asset_type: AssetType,
        content: bytes,
        filename: str,
        provider: str | None = None,
        model_id: str | None = None,
        prompt_hash: str | None = None,
        version: int = 1,
    ) -> Asset:
        storage_uri = self._write_local(project_id=project_id, filename=filename, content=content)
        asset = Asset(
            project_id=project_id,
            shot_id=shot_id,
            asset_type=asset_type.value,
            storage_uri=storage_uri,
            public_url=storage_uri,
            provider=provider,
            model_id=model_id,
            prompt_hash=prompt_hash,
            version=version,
        )
        self.db.add(asset)
        self.db.flush()
        return asset

    def latest_for_shot(self, shot_id: UUID, asset_type: AssetType) -> Asset | None:
        return self.db.scalar(
            select(Asset)
            .where(Asset.shot_id == shot_id, Asset.asset_type == asset_type.value)
            .order_by(Asset.created_at.desc())
        )

    def list_for_shot(self, shot_id: UUID, asset_type: AssetType | None = None) -> list[Asset]:
        statement = select(Asset).where(Asset.shot_id == shot_id)
        if asset_type:
            statement = statement.where(Asset.asset_type == asset_type.value)
        return list(self.db.scalars(statement.order_by(Asset.created_at.asc())))

    def archive_video(self, *, video_asset: Asset, satisfaction: Satisfaction) -> Asset:
        source = Path(video_asset.storage_uri.replace("file://", ""))
        archive_dir = "satisfied" if satisfaction == Satisfaction.SATISFIED else "unsatisfied"
        content = source.read_bytes() if source.exists() else b""
        filename = f"archive/{archive_dir}/{source.name}"
        return self.put_bytes(
            project_id=video_asset.project_id,
            shot_id=video_asset.shot_id,
            asset_type=AssetType.ARCHIVE,
            content=content,
            filename=filename,
            provider=video_asset.provider,
            model_id=video_asset.model_id,
            prompt_hash=video_asset.prompt_hash,
            version=video_asset.version,
        )

    def _write_local(self, *, project_id: UUID, filename: str, content: bytes) -> str:
        root = Path(settings.storage_local_root)
        digest = hashlib.sha256(content).hexdigest()[:12]
        target = root / str(project_id) / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            stem = target.stem
            suffix = target.suffix
            target = target.with_name(f"{stem}_{digest}{suffix}")
        target.write_bytes(content)
        return f"file://{target.resolve()}"

