"""
搜索处理模块
负责通过 PanSou / Jackett 搜索网盘资源和种子资源
"""
from typing import Optional, List, Dict

from app.log import logger
from app.schemas import MediaInfo
from app.schemas.types import MediaType


class SearchHandler:
    """搜索处理器 - PanSou + Jackett"""

    def __init__(
        self,
        pansou_client=None,
        pansou_enabled: bool = False,
        only_115: bool = True,
        pansou_channels: str = "",
        pansou_cloud_types: List[str] = None,
        jackett_client=None,
        jackett_enabled: bool = False,
    ):
        self._pansou_client = pansou_client
        self._pansou_enabled = pansou_enabled
        self._only_115 = only_115
        self._pansou_channels = pansou_channels
        self._pansou_cloud_types = pansou_cloud_types or ["115"]
        self._jackett_client = jackett_client
        self._jackett_enabled = jackett_enabled

    def get_enabled_sources(self) -> List[str]:
        sources = []
        if self._pansou_enabled and self._pansou_client:
            sources.append("pansou")
        if self._jackett_enabled and self._jackett_client:
            sources.append("jackett")
        return sources

    def search_resources(
        self,
        mediainfo: MediaInfo,
        media_type: MediaType,
        season: Optional[int] = None
    ) -> List[Dict]:
        """搜索所有启用的源，合并结果"""
        all_results = []
        for source in self.get_enabled_sources():
            results = self.search_single_source(source, mediainfo, media_type, season)
            all_results.extend(results)

        all_results.sort(key=lambda x: x.get("update_time", ""), reverse=True)
        return all_results

    def search_single_source(
        self,
        source: str,
        mediainfo: MediaInfo,
        media_type: MediaType,
        season: Optional[int] = None
    ) -> List[Dict]:
        if source == "pansou":
            if media_type == MediaType.MOVIE:
                return self._search_pansou_movie(mediainfo)
            else:
                return self._search_pansou_tv(mediainfo, season)
        elif source == "jackett":
            if media_type == MediaType.MOVIE:
                return self._search_jackett_movie(mediainfo)
            else:
                return self._search_jackett_tv(mediainfo, season)
        else:
            logger.warning(f"未知的搜索源: {source}")
            return []

    # ---- PanSou ----

    def _pansou_search(self, keyword: str) -> List[Dict]:
        cloud_types = self._pansou_cloud_types if self._pansou_cloud_types else ["115"]

        channels = None
        if self._pansou_channels and self._pansou_channels.strip():
            channels = [ch.strip() for ch in self._pansou_channels.split(',') if ch.strip()]

        search_results = self._pansou_client.search(
            keyword=keyword, cloud_types=cloud_types, channels=channels, limit=20
        )

        results = search_results.get("results", {}) if search_results and not search_results.get("error") else {}

        all_results = []
        type_name_map = {
            "115网盘": "115",
            "磁力链接": "magnet",
            "电驴链接": "ed2k"
        }

        for type_name, items in results.items():
            pan_type = type_name_map.get(type_name, type_name)
            for item in items:
                item["pan_type"] = pan_type
                all_results.append(item)

        all_results.sort(key=lambda x: x.get("update_time", ""), reverse=True)
        return all_results

    def _check_tmdb_multiple_results(self, title: str) -> bool:
        try:
            from app.modules.themoviedb.tmdbapi import TmdbApi
            tmdb_api = TmdbApi()
            results = tmdb_api.search_multiis(title)
            if results and len(results) > 1:
                logger.info(f"TMDB 搜索 '{title}' 发现 {len(results)} 个同名结果，将使用严格搜索（带年份）")
                return True
            return False
        except Exception as e:
            logger.warning(f"检查 TMDB 多结果失败: {e}，默认使用宽松搜索")
            return False

    def _search_pansou_movie(self, mediainfo: MediaInfo) -> List[Dict]:
        if not self._pansou_client:
            logger.warning("PanSou 客户端未初始化，跳过 PanSou 查询")
            return []

        need_strict_search = self._check_tmdb_multiple_results(mediainfo.title)
        if need_strict_search and mediainfo.year:
            keyword = f"{mediainfo.title} {mediainfo.year}"
        else:
            keyword = mediainfo.title

        logger.info(f"使用 PanSou 搜索电影资源: {mediainfo.title}，关键词: '{keyword}'")
        results = self._pansou_search(keyword)
        if results:
            logger.info(f"PanSou 搜索到 {len(results)} 个结果")
        else:
            logger.info("PanSou 未找到资源")
        return results

    def _search_pansou_tv(self, mediainfo: MediaInfo, season: int) -> List[Dict]:
        if not self._pansou_client:
            logger.warning("PanSou 客户端未初始化，跳过 PanSou 查询")
            return []

        search_keywords = [
            f"{mediainfo.title}{season}",
            mediainfo.title
        ]

        for keyword in search_keywords:
            logger.info(f"使用 PanSou 搜索电视剧资源: {mediainfo.title} S{season}，关键词: '{keyword}'")
            results = self._pansou_search(keyword)
            if results:
                logger.info(f"PanSou 关键词 '{keyword}' 搜索到 {len(results)} 个结果")
                return results
            else:
                logger.info(f"PanSou 关键词 '{keyword}' 无结果，尝试下一个降级关键词")

        logger.info("PanSou 未找到资源")
        return []

    # ---- Jackett ----

    def _jackett_search(self, keyword: str) -> List[Dict]:
        search_results = self._jackett_client.search(keyword=keyword, limit=20)
        results = search_results.get("results", {}) if search_results and not search_results.get("error") else {}

        all_results = []
        for items in results.values():
            for item in items:
                item["pan_type"] = "magnet"
                all_results.append(item)

        all_results.sort(key=lambda x: x.get("update_time", ""), reverse=True)
        return all_results

    def _search_jackett_movie(self, mediainfo: MediaInfo) -> List[Dict]:
        if not self._jackett_client:
            logger.warning("Jackett 客户端未初始化，跳过 Jackett 查询")
            return []

        need_strict_search = self._check_tmdb_multiple_results(mediainfo.title)
        if need_strict_search and mediainfo.year:
            keyword = f"{mediainfo.title} {mediainfo.year}"
        else:
            keyword = mediainfo.title

        logger.info(f"使用 Jackett 搜索电影资源: {mediainfo.title}，关键词: '{keyword}'")
        results = self._jackett_search(keyword)
        if results:
            logger.info(f"Jackett 搜索到 {len(results)} 个结果")
        else:
            logger.info("Jackett 未找到资源")
        return results

    def _search_jackett_tv(self, mediainfo: MediaInfo, season: int) -> List[Dict]:
        if not self._jackett_client:
            logger.warning("Jackett 客户端未初始化，跳过 Jackett 查询")
            return []

        search_keywords = [
            f"{mediainfo.title}{season}",
            mediainfo.title
        ]

        for keyword in search_keywords:
            logger.info(f"使用 Jackett 搜索电视剧资源: {mediainfo.title} S{season}，关键词: '{keyword}'")
            results = self._jackett_search(keyword)
            if results:
                logger.info(f"Jackett 关键词 '{keyword}' 搜索到 {len(results)} 个结果")
                return results
            else:
                logger.info(f"Jackett 关键词 '{keyword}' 无结果，尝试下一个降级关键词")

        logger.info("Jackett 未找到资源")
        return []
