from asyncio import to_thread
from typing import Any

from app import schemas
from app.core.config import settings
from app.helper.downloader import DownloaderHelper
from app.helper.torrent import TorrentHelper
from app.log import logger
from app.modules.qbittorrent.qbittorrent import Qbittorrent
from app.modules.transmission.transmission import Transmission
from app.plugins import _PluginBase
from app.schemas import ServiceInfo
from app.schemas.types import EventType
from app.utils.string import StringUtils


class DownloaderApi(_PluginBase):
    # 插件名称
    plugin_name = "下载器API"
    # 插件描述
    plugin_desc = "外部调用API直接下载，不识别。"
    # 插件图标
    # 插件图标
    plugin_icon = "sync_file.png"
    # 插件版本
    plugin_version = "1.3.5"
    # 插件作者
    plugin_author = "yubanmeiqin9048"
    # 作者主页
    author_url = "https://github.com/yubanmeiqin9048"
    # 插件配置项ID前缀
    plugin_config_prefix = "downloaderapi_"
    # 加载顺序
    plugin_order = 68
    # 可使用的用户级别
    auth_level = 2

    # 私有属性
    _enabled = False
    # 下载器
    _downloader = None
    _save_path = None
    _supported_downloaders = {"qbittorrent", "transmission"}

    def init_plugin(self, config: dict | None = None):
        self.downloader_helper = DownloaderHelper()
        self.torrent_helper = TorrentHelper()
        if not config:
            return
        self._enabled = config.get("enabled", False)
        self._save_path = config.get("save_path", None)
        self._downloader = config.get("downloader", None)
        if not self.downloader:
            self._enabled = False
            self.__update_config()
            return

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> list[dict[str, Any]]:  # type: ignore
        pass

    def get_api(self) -> list[dict[str, Any]]:
        return [
            {
                "path": "/download_torrent_notest",
                "endpoint": self.download_torrent,
                "methods": ["GET"],
                "summary": "下载种子",
                "description": "直接下载种子，不识别",
            }
        ]

    def get_page(self) -> list[dict]:  # type: ignore
        pass

    def get_form(self) -> tuple[list[dict], dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        downloader_options = [
            {"title": config.name, "value": config.name}
            for config in self.downloader_helper.get_configs().values()
            if config.type in self._supported_downloaders
        ]
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用插件",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "save_path",
                                            "label": "保存路径",
                                            "hint": "输入可访问路径",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "downloader",
                                            "label": "下载器",
                                            "items": downloader_options,
                                            "hint": "选择下载器",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                ],
            },
        ], {"enabled": False, "save_path": "", "downloader": ""}

    def stop_service(self):
        """
        退出插件
        """
        pass

    def __update_config(self):
        self.update_config(
            {
                "enabled": self._enabled,
                "save_path": self._save_path,
                "downloader": self._downloader,
            }
        )

    @staticmethod
    def _clean_label(label: str | None) -> str | None:
        if not label:
            return None
        label = str(label).strip()
        return label or None

    def _build_labels(self, site_name: str | None = None, site_tag: str | None = None) -> list[str]:
        """
        构建前置标签，优先使用显式 site_tag，其次使用 site_name。
        同时补上 MP 默认下载标签，确保后续转移链路仍能识别任务。
        """
        labels: list[str] = []
        base_labels = getattr(settings, "TORRENT_TAG", None)
        if base_labels:
            labels.extend(
                [label.strip() for label in str(base_labels).split(",") if label and str(label).strip()]
            )

        explicit_site_tag = self._clean_label(site_tag)
        explicit_site_name = self._clean_label(site_name)
        if explicit_site_tag:
            labels.append(explicit_site_tag)
        elif explicit_site_name:
            labels.append(explicit_site_name)

        return list(dict.fromkeys(labels))

    @staticmethod
    def _get_torrent_hash(torrent: Any) -> str | None:
        """
        兼容QB/TR的种子hash字段
        """
        if torrent is None:
            return None
        return (
            getattr(torrent, "hash", None)
            or getattr(torrent, "hashString", None)
            or getattr(torrent, "hash_string", None)
        )

    @staticmethod
    def _get_torrent_size(torrent: Any) -> int:
        """
        兼容QB/TR的种子大小字段
        """
        if torrent is None:
            return 0
        size = (
            getattr(torrent, "size", None)
            or getattr(torrent, "total_size", None)
            or getattr(torrent, "totalSize", None)
        )
        return int(size or 0)

    @staticmethod
    def _get_torrent_id(torrent: Any) -> str | int | None:
        if torrent is None:
            return None
        return getattr(torrent, "id", None) or getattr(torrent, "torrent_id", None)

    async def _add_qbittorrent(
        self,
        torrent_url: str,
        site_name: str | None = None,
        site_tag: str | None = None,
    ) -> tuple[str | None, int, str | None]:
        """
        通过QB添加种子并返回hash与大小
        """
        track_tag = StringUtils.generate_random_str(10)
        labels = self._build_labels(site_name=site_name, site_tag=site_tag)
        qb_tags = ",".join([track_tag, *labels]) if labels else track_tag
        state = await to_thread(
            self.downloader.add_torrent,
            content=torrent_url,
            download_dir=self._save_path,
            tag=qb_tags,
        )
        if not state:
            return None, 0, "种子添加下载失败"

        torrent_hash = await to_thread(self.downloader.get_torrent_id_by_tag, track_tag)
        if not torrent_hash:
            return None, 0, "种子添加成功，但未能定位下载任务"

        await to_thread(self.downloader.remove_torrents_tag, torrent_hash, [track_tag])

        torrents, error = await to_thread(self.downloader.get_torrents, torrent_hash)
        if error:
            return torrent_hash, 0, "种子添加成功，但查询任务详情失败"

        torrent = torrents[0] if torrents else None
        return torrent_hash, self._get_torrent_size(torrent), None

    async def _add_transmission(
        self,
        torrent_url: str,
        site_name: str | None = None,
        site_tag: str | None = None,
    ) -> tuple[str | None, int, str | None]:
        """
        通过Transmission添加种子并返回hash与大小
        """
        labels = self._build_labels(site_name=site_name, site_tag=site_tag)
        torrent = await to_thread(
            self.downloader.add_torrent,
            content=torrent_url,
            download_dir=self._save_path,
            labels=labels or None,
        )
        if not torrent:
            return None, 0, "种子添加下载失败"

        torrent_hash = self._get_torrent_hash(torrent)
        size = self._get_torrent_size(torrent)
        if torrent_hash:
            return torrent_hash, size, None

        torrent_id = self._get_torrent_id(torrent)
        torrents, error = await to_thread(self.downloader.get_torrents, ids=torrent_id) if torrent_id else ([], True)
        if error:
            return None, 0, "种子添加成功，但查询下载任务失败"
        if not torrents:
            return None, 0, "种子添加成功，但未能定位下载任务"

        torrent = torrents[0]
        torrent_hash = self._get_torrent_hash(torrent)
        if not torrent_hash:
            return None, 0, "种子添加成功，但未能解析下载任务标识"

        return torrent_hash, self._get_torrent_size(torrent), None

    async def download_torrent(
        self,
        torrent_url: str,
        site_name: str | None = None,
        site_tag: str | None = None,
    ) -> schemas.Response:
        """
        API调用下载种子
        """
        try:
            if not self.downloader:
                return schemas.Response(success=False, message="未配置下载器")
            if isinstance(self.downloader, Transmission):
                torrent_hash, size, error_message = await self._add_transmission(
                    torrent_url,
                    site_name=site_name,
                    site_tag=site_tag,
                )
            else:
                torrent_hash, size, error_message = await self._add_qbittorrent(
                    torrent_url,
                    site_name=site_name,
                    site_tag=site_tag,
                )

            if error_message:
                return schemas.Response(success=False, message=error_message)

            self.eventmanager.send_event(
                EventType.PluginAction,
                {"action": "downloaderapi_add", "hash": torrent_hash, "size": size},
            )
            return schemas.Response(success=True, message="下载成功")
        except Exception as e:
            return schemas.Response(success=False, message=f"调用失败，原因：{e}")

    @property
    def service_info(self) -> ServiceInfo | None:
        """
        服务信息
        """
        if not self._downloader:
            logger.warning("尚未配置下载器，请检查配置")
            return None

        service = self.downloader_helper.get_service(name=self._downloader)
        if not service:
            logger.warning("获取下载器实例失败，请检查配置")
            return None
        if service.type not in self._supported_downloaders:
            logger.warning(f"下载器 {self._downloader} 类型 {service.type} 暂不支持")
            return None
        if not service.instance:
            logger.warning("下载器实例为空，请检查配置")
            return None
        if service.instance.is_inactive():
            logger.warning(f"下载器 {self._downloader} 未连接，请检查配置")
            return None

        return service

    @property
    def downloader(self) -> Qbittorrent | Transmission | None:
        """
        下载器实例
        """
        return self.service_info.instance if self.service_info else None
