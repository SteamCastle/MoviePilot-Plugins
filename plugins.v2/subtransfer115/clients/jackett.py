"""
Jackett 搜索客户端
通过 Torznab API 搜索种子资源
"""
import logging
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

TORZNAB_NS = "http://torznab.schemas.com/2010/feed"


class JackettClient:
    """Jackett Torznab API 客户端"""

    def __init__(
        self,
        base_url: str,
        apikey: str,
        proxy: Optional[str] = None,
    ):
        self._base_url = base_url.rstrip("/")
        self._apikey = apikey
        self._api_call_count = 0

        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "MoviePilot-SubTransfer115/1.0"})

        if proxy:
            self._session.proxies = {"http": proxy, "https": proxy}

    def reset_api_call_count(self):
        self._api_call_count = 0

    @property
    def api_call_count(self) -> int:
        return self._api_call_count

    def search(self, keyword: str, limit: int = 20) -> Dict:
        """
        搜索资源，返回与 PanSou 兼容的分组格式

        :param keyword: 搜索关键词
        :param limit: 结果上限
        :return: {"keyword": str, "total": int, "count": int, "results": {"磁力链接": [...]}}
        """
        url = f"{self._base_url}/api/v2.0/indexers/all/results/torznab/api"
        params = {
            "t": "search",
            "q": keyword,
            "apikey": self._apikey,
        }

        try:
            self._api_call_count += 1
            resp = self._session.get(url, params=params, timeout=30)
            resp.raise_for_status()

            items = self._parse_torznab_xml(resp.text)
            items = items[:limit]

            return {
                "keyword": keyword,
                "total": len(items),
                "count": len(items),
                "results": {"磁力链接": items} if items else {},
            }
        except requests.RequestException as e:
            logger.error(f"Jackett 搜索请求失败: {e}")
            return {"keyword": keyword, "total": 0, "count": 0, "results": {}, "error": str(e)}
        except Exception as e:
            logger.error(f"Jackett 搜索结果解析失败: {e}")
            return {"keyword": keyword, "total": 0, "count": 0, "results": {}, "error": str(e)}

    def _parse_torznab_xml(self, xml_text: str) -> List[Dict]:
        """解析 Torznab XML 响应为结果列表"""
        root = ET.fromstring(xml_text)
        channel = root.find("channel")
        if channel is None:
            return []

        results = []
        for item in channel.findall("item"):
            try:
                title = self._get_text(item, "title")
                if not title:
                    continue

                magnet_url = self._extract_magnet(item)
                if not magnet_url:
                    continue

                pub_date = self._get_text(item, "pubDate")
                size = self._extract_torznab_attr(item, "size")
                seeders = self._extract_torznab_attr(item, "seeders")

                results.append({
                    "url": magnet_url,
                    "title": title,
                    "update_time": pub_date or "",
                    "size": int(size) if size else 0,
                    "seeders": int(seeders) if seeders else 0,
                })
            except Exception as e:
                logger.debug(f"解析 Jackett item 失败: {e}")
                continue

        results.sort(key=lambda x: x.get("seeders", 0), reverse=True)
        return results

    def _extract_magnet(self, item: ET.Element) -> Optional[str]:
        """从 item 中提取磁力链接"""
        # 优先从 torznab:attr 中获取
        magnet = self._extract_torznab_attr(item, "magneturl")
        if magnet and magnet.startswith("magnet:"):
            return magnet

        # 从 guid 中获取
        guid = self._get_text(item, "guid")
        if guid and guid.startswith("magnet:"):
            return guid

        return None

    def _extract_torznab_attr(self, item: ET.Element, name: str) -> Optional[str]:
        """提取 torznab:attr 属性值"""
        for attr in item.findall(f"{{{TORZNAB_NS}}}attr"):
            if attr.get("name") == name:
                return attr.get("value")
        return None

    @staticmethod
    def _get_text(element: ET.Element, tag: str) -> Optional[str]:
        el = element.find(tag)
        return el.text if el is not None and el.text else None
