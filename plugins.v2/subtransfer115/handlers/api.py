"""
API 处理模块
负责插件的外部 API 接口
"""
from typing import Callable

from app.core.config import settings
from app.log import logger


class ApiHandler:
    """API 处理器"""

    def __init__(
        self,
        pansou_client,
        p115_manager,
        only_115: bool = True,
        save_path: str = "",
        get_data_func: Callable = None,
        save_data_func: Callable = None,
        jackett_client=None,
    ):
        """
        初始化 API 处理器

        :param pansou_client: PanSou 客户端实例
        :param p115_manager: 115 客户端管理器
        :param only_115: 是否只搜索115网盘资源
        :param save_path: 默认转存目录
        :param get_data_func: 获取数据的函数
        :param save_data_func: 保存数据的函数
        :param jackett_client: Jackett 客户端实例
        """
        self._pansou_client = pansou_client
        self._p115_manager = p115_manager
        self._only_115 = only_115
        self._save_path = save_path
        self._get_data = get_data_func
        self._save_data = save_data_func
        self._jackett_client = jackett_client

    def search(self, keyword: str, apikey: str) -> dict:
        """
        API: 搜索网盘资源

        :param keyword: 搜索关键词
        :param apikey: API 密钥
        :return: 搜索结果
        """
        if apikey != settings.API_TOKEN:
            return {"error": "API密钥错误"}

        results = {}

        if self._pansou_client:
            cloud_types = ["115"] if self._only_115 else None
            results.update(
                self._pansou_client.search(keyword=keyword, cloud_types=cloud_types, limit=10).get("results", {})
            )

        if self._jackett_client:
            jackett_result = self._jackett_client.search(keyword=keyword, limit=10)
            if not jackett_result.get("error"):
                results.update(jackett_result.get("results", {}))

        if not results and not self._pansou_client and not self._jackett_client:
            return {"error": "搜索客户端未初始化"}

        return {"keyword": keyword, "results": results}

    def search_test(self, keyword: str, source: str) -> dict:
        """
        API: 搜索测试（直接关键词搜索，不依赖 MediaInfo）

        :param keyword: 搜索关键词
        :param source: 搜索源 (pansou | jackett)
        :return: 搜索结果
        """
        if source == "pansou":
            if not self._pansou_client:
                return {"error": "PanSou 客户端未初始化，请检查配置"}
            result = self._pansou_client.search(
                keyword=keyword,
                cloud_types=["115", "magnet", "ed2k"],
                limit=20
            )
            items = []
            type_map = {"115网盘": "115", "磁力链接": "magnet", "电驴链接": "ed2k"}
            if result and not result.get("error"):
                for type_name, type_items in result.get("results", {}).items():
                    pan_type = type_map.get(type_name, type_name)
                    for item in type_items:
                        item["pan_type"] = pan_type
                        items.append(item)
            items.sort(key=lambda x: x.get("update_time", ""), reverse=True)
            return {"keyword": keyword, "source": "pansou", "total": len(items), "results": items}
        elif source == "jackett":
            if not self._jackett_client:
                return {"error": "Jackett 客户端未初始化，请检查配置"}
            result = self._jackett_client.search(keyword=keyword, limit=20)
            items = []
            if result and not result.get("error"):
                for type_name, type_items in result.get("results", {}).items():
                    for item in type_items:
                        item["pan_type"] = "magnet"
                        item["indexer"] = type_name
                        items.append(item)
            items.sort(key=lambda x: x.get("update_time", ""), reverse=True)
            return {"keyword": keyword, "source": "jackett", "total": len(items), "results": items}
        else:
            return {"error": f"未知的搜索源: {source}，可选值为 pansou 或 jackett"}

    def transfer(self, share_url: str, save_path: str, apikey: str) -> dict:
        """
        API: 转存分享链接

        :param share_url: 分享链接
        :param save_path: 转存路径
        :param apikey: API 密钥
        :return: 转存结果
        """
        if apikey != settings.API_TOKEN:
            return {"success": False, "error": "API密钥错误"}

        if not self._p115_manager:
            return {"success": False, "error": "115 客户端未初始化"}

        success = self._p115_manager.transfer_share(share_url, save_path or self._save_path)
        return {"success": success}

    def clear_history(self, apikey: str) -> dict:
        """
        API: 清空历史记录

        :param apikey: API 密钥
        :return: 操作结果
        """
        if apikey != settings.API_TOKEN:
            return {"success": False, "message": "API密钥错误"}

        if self._save_data:
            self._save_data('history', [])
        logger.info("SubTransfer115 历史记录已清空")
        return {"success": True, "message": "历史记录已清空"}

    def list_directories(self, path: str = "/", apikey: str = "") -> dict:
        """
        API: 列出115网盘指定路径下的目录

        :param path: 目录路径
        :param apikey: API 密钥
        :return: 目录列表
        """
        if apikey != settings.API_TOKEN:
            return {"success": False, "error": "API密钥错误"}

        if not self._p115_manager:
            return {"success": False, "error": "115客户端未初始化"}

        try:
            directories = self._p115_manager.list_directories(path)

            # 构建面包屑导航
            breadcrumbs = []
            if path and path != "/":
                parts = [p for p in path.split("/") if p]
                current_path = ""
                breadcrumbs.append({"name": "根目录", "path": "/"})
                for part in parts:
                    current_path = f"{current_path}/{part}"
                    breadcrumbs.append({"name": part, "path": current_path})
            else:
                breadcrumbs.append({"name": "根目录", "path": "/"})

            return {
                "success": True,
                "path": path,
                "breadcrumbs": breadcrumbs,
                "directories": directories
            }
        except Exception as e:
            logger.error(f"列出115目录失败: {e}")
            return {"success": False, "error": str(e)}
