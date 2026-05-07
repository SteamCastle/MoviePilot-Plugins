"""
SubTransfer115 插件
结合MoviePilot订阅功能，通过PanSou/Jackett搜索115网盘资源并转存缺失剧集
"""
import datetime
from pathlib import Path
from threading import Lock
from typing import Optional, Any, List, Dict, Tuple

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import text

from app.core.config import settings, global_vars
from app.core.event import Event, eventmanager
from app.db import SessionFactory
from app.db.subscribe_oper import SubscribeOper
from app.db.models.site import Site
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType, MediaType, NotificationType

from .clients import PanSouClient, P115ClientManager, JackettClient
from .handlers import SearchHandler, SyncHandler, SubscribeHandler, ApiHandler
from .ui import UIConfig

lock = Lock()


class SubTransfer115(_PluginBase):
    """SubTransfer115 插件 - 订阅转存115网盘"""

    plugin_name = "SubTransfer115"
    plugin_desc = "结合MoviePilot订阅功能，通过PanSou/Jackett搜索115网盘资源并转存缺失的电影和剧集。"
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Plugins/main/icons/cloud.png"
    plugin_version = "1.0.6"
    plugin_author = "SteamCastle"
    author_url = "https://github.com/SteamCastle"
    plugin_config_prefix = "subtransfer115_"
    plugin_order = 20
    auth_level = 1

    _scheduler: Optional[BackgroundScheduler] = None
    _toggle_scheduler: Optional[BackgroundScheduler] = None

    _enabled: bool = False
    _onlyonce: bool = False
    _cron: str = "30 2,10,18 * * *"
    _notify: bool = False

    _cookies: str = ""
    _pansou_enabled: bool = True
    _pansou_url: str = "https://so.252035.xyz"
    _pansou_username: str = ""
    _pansou_password: str = ""
    _pansou_auth_enabled: bool = False
    _pansou_channels: str = "QukanMovie"
    _pansou_cloud_types: List[str] = ["115"]

    _jackett_enabled: bool = False
    _jackett_url: str = ""
    _jackett_apikey: str = ""
    _jackett_tag: str = ""

    _save_path: str = "/我的接收/MoviePilot/TV"
    _movie_save_path: str = "/我的接收/MoviePilot/Movie"
    _offline_download_path: str = "/我的接收/MoviePilot/OfflineTV"
    _movie_offline_download_path: str = "/我的接收/MoviePilot/OfflineMovie"
    _only_115: bool = True
    _exclude_subscribes: List[int] = []

    _block_system_subscribe: bool = False

    _max_transfer_per_sync: int = 50
    _batch_size: int = 20
    _skip_other_season_dirs: bool = True

    _unblock_site_ids: List[int] = []
    _unblock_site_names: List[str] = []
    _unblock_delay_minutes: int = 5
    _system_subscribe_window_hours: float = 1.0

    _pansou_client: Optional[PanSouClient] = None
    _p115_manager: Optional[P115ClientManager] = None
    _jackett_client: Optional[JackettClient] = None

    _search_handler: Optional[SearchHandler] = None
    _subscribe_handler: Optional[SubscribeHandler] = None
    _sync_handler: Optional[SyncHandler] = None
    _api_handler: Optional[ApiHandler] = None

    _MIN_INTERVAL_HOURS: int = 8

    # ------------------ 调度器 ------------------

    def _ensure_toggle_scheduler(self):
        if not self._toggle_scheduler:
            self._toggle_scheduler = BackgroundScheduler(timezone=settings.TZ)
            self._toggle_scheduler.start()

    def _cancel_toggle_jobs(self):
        if not self._toggle_scheduler:
            return
        for job_id in ["subtransfer_unblock_job", "subtransfer_reblock_job"]:
            try:
                self._toggle_scheduler.remove_job(job_id)
            except Exception:
                pass

    # ------------------ cron间隔校验 ------------------

    @staticmethod
    def _cron_interval_ge_min_hours(cron_expr: str, min_hours: int) -> bool:
        cron_expr = (cron_expr or "").strip()
        if not cron_expr:
            return False
        try:
            tz = pytz.timezone(settings.TZ)
            trigger = CronTrigger.from_crontab(cron_expr, timezone=tz)
        except Exception:
            return False

        now = datetime.datetime.now(tz=pytz.timezone(settings.TZ))
        fire_times: List[datetime.datetime] = []
        prev = None
        current = now
        for _ in range(12):
            nxt = trigger.get_next_fire_time(prev, current)
            if not nxt:
                break
            fire_times.append(nxt)
            prev = nxt
            current = nxt + datetime.timedelta(seconds=1)

        if len(fire_times) < 2:
            return True

        min_delta = min(fire_times[i + 1] - fire_times[i] for i in range(len(fire_times) - 1))
        return min_delta >= datetime.timedelta(hours=min_hours)

    # ------------------ 站点解析 ------------------

    def _load_site_records(self) -> List[Dict[str, Any]]:
        with SessionFactory() as db:
            rows = db.execute(text("SELECT id, name, is_active FROM site")).fetchall()
        out = []
        for r in rows:
            out.append({"id": int(r[0]), "name": str(r[1]), "is_active": bool(r[2])})
        return out

    def _resolve_site_ids(self, ids: Optional[List[int]] = None, names: Optional[List[str]] = None) -> List[int]:
        ids = ids or []
        names = names or []

        site_records = self._load_site_records()
        by_name = {s["name"]: s for s in site_records}
        by_id = {s["id"]: s for s in site_records}

        final_ids: List[int] = []
        for sid in ids:
            if sid in by_id:
                final_ids.append(sid)
            else:
                logger.warning(f"站点ID不存在：id={sid}（将跳过）")

        for nm in names:
            rec = by_name.get(nm)
            if not rec:
                logger.warning(f"站点名称不存在：name={nm}（将跳过）")
                continue
            final_ids.append(int(rec["id"]))

        seen = set()
        uniq = []
        for x in final_ids:
            if x not in seen:
                seen.add(x)
                uniq.append(x)

        mapped = []
        for x in uniq:
            rec = by_id.get(x, {})
            mapped.append(f"{rec.get('name','?')}({x})")
        logger.info(f"订阅站点解析结果：ids={uniq} | 映射={mapped}")
        return uniq

    def _ensure_115_site_id(self, db=None) -> int:
        def _do_ensure(session):
            row = session.execute(text("SELECT id FROM site WHERE name=:n LIMIT 1"), {"n": "115网盘"}).fetchone()
            if row and row[0] is not None:
                return int(row[0])

            row_ex = session.execute(text("SELECT id FROM site WHERE id=:i"), {"i": -1}).fetchone()
            if not row_ex:
                session.execute(
                    text(
                        "INSERT INTO site (id, name, url, is_active, limit_interval, limit_count, limit_seconds, timeout) "
                        "VALUES (:id, :name, :url, :is_active, :limit_interval ,:limit_count, :limit_seconds, :timeout)"
                    ),
                    {
                        "id": -1, "name": "115网盘", "url": "https://115.com",
                        "is_active": True, "limit_interval": 10000000,
                        "limit_count": 1, "limit_seconds": 10000000, "timeout": 1
                    }
                )
                session.commit()
                logger.info("已插入站点记录：115网盘(id=-1)")
            return -1

        if db is not None:
            return _do_ensure(db)
        else:
            with SessionFactory() as new_db:
                return _do_ensure(new_db)

    def _apply_sites_to_all_subscribes(self, site_ids: List[int], reason: str):
        exclude_ids = set(self._exclude_subscribes or [])
        with SessionFactory() as db:
            subscribe_oper = SubscribeOper(db=db)
            subs = subscribe_oper.list() or []
            updated, excluded = 0, 0
            for s in subs:
                if s.id in exclude_ids:
                    excluded += 1
                    continue
                subscribe_oper.update(s.id, {"sites": site_ids})
                updated += 1
        logger.info(f"{reason}：已更新 {updated} 个订阅（跳过 {excluded} 个排除订阅）")

    # ------------------ 禁用窗口判断 ------------------

    def _window_disabled(self) -> bool:
        if not self._unblock_site_names:
            return True
        if float(self._system_subscribe_window_hours or 0) <= 0:
            return True
        if int(self._unblock_delay_minutes) < 0:
            return True
        return False

    def _window_enabled(self) -> bool:
        return not self._window_disabled()

    def _try_set_default_sites_for_unblocked(self, site_ids: List[int]):
        try:
            from app.db.systemconfig_oper import SystemConfigOper
        except Exception:
            return

        def _build_oper(db):
            try:
                return SystemConfigOper(db)
            except Exception:
                try:
                    return SystemConfigOper(db=db)
                except Exception:
                    return None

        candidate_keys = [
            "subscribe_sites", "subscribe_site_ids",
            "system_subscribe_sites", "system_subscribe_site_ids",
            "subscribe_sites_selected",
        ]

        with SessionFactory() as db:
            oper = _build_oper(db)
            if not oper:
                return
            get_fn = getattr(oper, "get", None) or getattr(oper, "get_by_key", None)
            set_fn = getattr(oper, "set", None) or getattr(oper, "set_by_key", None)
            if not get_fn or not set_fn:
                return
            for k in candidate_keys:
                try:
                    cur = get_fn(k)
                except Exception:
                    cur = None
                if cur is None:
                    continue
                try:
                    set_fn(k, site_ids)
                    logger.info(f"已恢复系统订阅：已尝试同步默认订阅站点 key={k} value={site_ids}")
                    break
                except Exception:
                    continue

    # ------------------ 两态切换 ------------------

    def _enter_blocked(self, reason: str):
        self._ensure_toggle_scheduler()
        self._cancel_toggle_jobs()
        self._init_subscribe_handler()
        self._subscribe_handler.set_blocked_sites_only_115()
        self._block_system_subscribe = True
        self.__update_config()
        logger.info(f"已屏蔽系统订阅（仅115网盘）：{reason}")

    def _enter_unblocked(self, reason: str):
        if not self._window_enabled():
            self._block_system_subscribe = True
            self.__update_config()
            self._enter_blocked(reason=f"{reason}（窗口禁用）")
            return
        self._ensure_toggle_scheduler()
        self._cancel_toggle_jobs()
        self._init_subscribe_handler()
        site_ids = self._resolve_site_ids(ids=self._unblock_site_ids, names=self._unblock_site_names)
        if not site_ids:
            self._block_system_subscribe = True
            self.__update_config()
            self._enter_blocked(reason=f"{reason}（站点解析失败）")
            return
        self._apply_sites_to_all_subscribes(site_ids, reason="已恢复系统订阅：全量同步站点")
        self._try_set_default_sites_for_unblocked(site_ids)
        self._block_system_subscribe = False
        self.__update_config()
        logger.info(f"已恢复系统订阅：站点={self._unblock_site_names} 窗口期={self._system_subscribe_window_hours}h（{reason}）")
        self._schedule_reblock_after_window()

    def _schedule_reblock_after_window(self):
        hours = float(self._system_subscribe_window_hours or 0)
        if hours <= 0:
            return
        tz = pytz.timezone(settings.TZ)
        now = datetime.datetime.now(tz=tz)
        run_date = now + datetime.timedelta(hours=hours)
        self._toggle_scheduler.add_job(
            func=lambda: self._enter_blocked(reason="窗口到期"),
            trigger="date", run_date=run_date,
            id="subtransfer_reblock_job", replace_existing=True
        )
        logger.info(f"已安排：{run_date} 切换为已屏蔽系统订阅（仅115网盘）")

    def _schedule_unblock_after_delay(self, base_time: datetime.datetime):
        delay = int(self._unblock_delay_minutes)
        if delay < 0:
            return
        if not self._window_enabled():
            return
        self._ensure_toggle_scheduler()
        self._cancel_toggle_jobs()
        tz = pytz.timezone(settings.TZ)
        base_time = base_time.astimezone(tz)
        run_date = base_time + datetime.timedelta(minutes=delay)
        self._toggle_scheduler.add_job(
            func=lambda: self._enter_unblocked(reason="触发条件1：最后一次任务"),
            trigger="date", run_date=run_date,
            id="subtransfer_unblock_job", replace_existing=True
        )
        logger.info(f"已安排：{run_date} 切换为已恢复系统订阅（延迟={delay}min）")

    # ------------------ 最后一次任务判断 ------------------

    def _is_last_run_today(self, run_start: datetime.datetime) -> bool:
        try:
            tz = pytz.timezone(settings.TZ)
            run_start = run_start.astimezone(tz)
            trigger = CronTrigger.from_crontab(self._cron, timezone=tz)
            nxt = trigger.get_next_fire_time(None, run_start + datetime.timedelta(seconds=1))
            if not nxt:
                return False
            return nxt.date() != run_start.date()
        except Exception as e:
            logger.warning(f"判断是否当天最后一次触发失败：{e}，按 23:00 兜底")
            return run_start.hour == 23 and run_start.minute == 00

    # ------------------ 事件兜底 ------------------

    def _get_subscribe_id_from_event(self, event: Event) -> Optional[int]:
        if not event or not event.event_data:
            return None
        data = event.event_data or {}
        subscribe_id = data.get("subscribe_id") or data.get("id")
        if not subscribe_id and isinstance(data.get("subscribe"), dict):
            subscribe_id = data["subscribe"].get("id")
        try:
            return int(subscribe_id) if subscribe_id is not None else None
        except Exception:
            return None

    @eventmanager.register(EventType.SubscribeAdded)
    def on_subscribe_added(self, event: Event):
        sid = self._get_subscribe_id_from_event(event)
        if not sid:
            return
        try:
            self._init_subscribe_handler()
            if self._block_system_subscribe:
                if hasattr(self._subscribe_handler, "set_sites_for_subscribe_only_115"):
                    self._subscribe_handler.set_sites_for_subscribe_only_115(sid)
                else:
                    with SessionFactory() as db:
                        site_id_115 = self._ensure_115_site_id(db)
                        SubscribeOper(db=db).update(sid, {"sites": [site_id_115]})
                logger.info(f"已屏蔽系统订阅：新增订阅已拉回仅115（subscribe_id={sid}）")
            else:
                if self._window_enabled() and hasattr(self._subscribe_handler, "set_sites_for_subscribe_by_names"):
                    self._subscribe_handler.set_sites_for_subscribe_by_names(sid, self._unblock_site_names)
                    logger.info(f"已恢复系统订阅：新增订阅已同步窗口站点（subscribe_id={sid})")
        except Exception as e:
            logger.error(f"SubscribeAdded 兜底失败：{e}")

    @eventmanager.register(EventType.SubscribeModified)
    def on_subscribe_modified(self, event: Event):
        sid = self._get_subscribe_id_from_event(event)
        if not sid:
            return
        if self._block_system_subscribe:
            logger.info(f"已屏蔽系统订阅：检测到订阅改动，按规则不自动拉回（subscribe_id={sid}）")
        return

    # ------------------ init_plugin ------------------

    def init_plugin(self, config: dict = None):
        self.stop_service()
        self._ensure_toggle_scheduler()

        old_block = bool(self._block_system_subscribe)

        if config:
            self._enabled = config.get("enabled", False)
            self._cron = (config.get("cron", self._cron) or "").strip()
            if self._cron:
                ok = self._cron_interval_ge_min_hours(self._cron, self._MIN_INTERVAL_HOURS)
                if not ok:
                    logger.warning(f"Cron 过于频繁（要求间隔>= {self._MIN_INTERVAL_HOURS}h）：{self._cron}，已回退默认")
                    self._cron = "30 */8 * * *"

            self._notify = config.get("notify", False)
            self._onlyonce = config.get("onlyonce", False)
            self._cookies = config.get("cookies", "")

            self._pansou_enabled = config.get("pansou_enabled", True)
            self._pansou_url = config.get("pansou_url", "https://so.252035.xyz/")
            self._pansou_username = config.get("pansou_username", "")
            self._pansou_password = config.get("pansou_password", "")
            self._pansou_auth_enabled = config.get("pansou_auth_enabled", False)
            self._pansou_channels = config.get("pansou_channels", "QukanMovie")
            self._pansou_cloud_types = config.get("pansou_cloud_types", ["115"]) or ["115"]

            self._jackett_enabled = config.get("jackett_enabled", False)
            self._jackett_url = config.get("jackett_url", "")
            self._jackett_apikey = config.get("jackett_apikey", "")
            self._jackett_tag = config.get("jackett_tag", "")

            self._save_path = config.get("save_path", "/我的接收/MoviePilot/TV")
            self._movie_save_path = config.get("movie_save_path", "/我的接收/MoviePilot/Movie")
            self._offline_download_path = config.get("offline_download_path", "/我的接收/MoviePilot/OfflineTV")
            self._movie_offline_download_path = config.get("movie_offline_download_path", "/我的接收/MoviePilot/OfflineMovie")
            self._only_115 = config.get("only_115", True)
            self._exclude_subscribes = config.get("exclude_subscribes", []) or []

            self._max_transfer_per_sync = int(config.get("max_transfer_per_sync", 50) or 50)
            self._batch_size = int(config.get("batch_size", 20) or 20)
            self._skip_other_season_dirs = config.get("skip_other_season_dirs", True)

            self._unblock_site_ids = config.get("unblock_site_ids", []) or []
            raw_sites = config.get("unblock_site_names", self._unblock_site_names)
            if isinstance(raw_sites, str):
                self._unblock_site_names = [x.strip() for x in raw_sites.split(",") if x.strip()]
            else:
                self._unblock_site_names = raw_sites or []

            self._unblock_delay_minutes = int(config.get("unblock_delay_minutes", self._unblock_delay_minutes))
            self._system_subscribe_window_hours = float(
                config.get("unblock_window_hours", config.get("system_subscribe_window_hours", self._system_subscribe_window_hours))
            )
            self._block_system_subscribe = bool(config.get("block_system_subscribe", False))

        self._init_clients()
        self._init_handlers()

        if (old_block is True) and (self._block_system_subscribe is False) and (not self._window_enabled()):
            self._block_system_subscribe = True
            self.__update_config()
            self._enter_blocked(reason="触发条件2（窗口无效）")
            return

        if self._block_system_subscribe:
            self._enter_blocked(reason="配置应用")
        else:
            self._enter_unblocked(reason="配置应用")

        if self._enabled or self._onlyonce:
            if self._onlyonce:
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                self._scheduler.add_job(
                    func=self.sync_subscribes, trigger='date',
                    run_date=datetime.datetime.now(tz=pytz.timezone(settings.TZ)) + datetime.timedelta(seconds=3)
                )
                if self._scheduler.get_jobs():
                    self._scheduler.start()
            if self._onlyonce:
                self._onlyonce = False
                self.__update_config()

    # ------------------ init clients/handlers ------------------

    def _init_clients(self):
        proxy = settings.PROXY
        if proxy:
            logger.info(f"使用 MoviePilot PROXY: {proxy}")

        if self._pansou_enabled and self._pansou_url:
            self._pansou_client = PanSouClient(
                base_url=self._pansou_url,
                username=self._pansou_username,
                password=self._pansou_password,
                auth_enabled=self._pansou_auth_enabled,
                proxy=proxy
            )

        if self._jackett_enabled and self._jackett_url and self._jackett_apikey:
            self._jackett_client = JackettClient(
                base_url=self._jackett_url,
                apikey=self._jackett_apikey,
                proxy=proxy,
                tag=self._jackett_tag or None,
            )
            logger.info("Jackett 客户端已初始化")

        if self._cookies:
            self._p115_manager = P115ClientManager(cookies=self._cookies)

    def _init_subscribe_handler(self):
        self._subscribe_handler = SubscribeHandler(
            exclude_subscribes=self._exclude_subscribes,
            notify=self._notify,
            post_message_func=self.post_message
        )

    def _init_handlers(self):
        self._init_subscribe_handler()

        self._search_handler = SearchHandler(
            pansou_client=self._pansou_client,
            pansou_enabled=self._pansou_enabled,
            only_115=self._only_115,
            pansou_channels=self._pansou_channels,
            pansou_cloud_types=self._pansou_cloud_types,
            jackett_client=self._jackett_client,
            jackett_enabled=self._jackett_enabled,
        )

        self._sync_handler = SyncHandler(
            p115_manager=self._p115_manager,
            search_handler=self._search_handler,
            subscribe_handler=self._subscribe_handler,
            chain=self.chain,
            save_path=self._save_path,
            movie_save_path=self._movie_save_path,
            offline_download_path=self._offline_download_path,
            movie_offline_download_path=self._movie_offline_download_path,
            max_transfer_per_sync=self._max_transfer_per_sync,
            batch_size=self._batch_size,
            skip_other_season_dirs=self._skip_other_season_dirs,
            notify=self._notify,
            post_message_func=self.post_message,
            get_data_func=self.get_data,
            save_data_func=self.save_data
        )

        self._api_handler = ApiHandler(
            pansou_client=self._pansou_client,
            p115_manager=self._p115_manager,
            only_115=self._only_115,
            save_path=self._save_path,
            get_data_func=self.get_data,
            save_data_func=self.save_data,
            jackett_client=self._jackett_client,
        )

    # ------------------ 配置写回 ------------------

    def __update_config(self):
        self.update_config({
            "enabled": self._enabled,
            "cron": self._cron,
            "notify": self._notify,
            "onlyonce": self._onlyonce,
            "only_115": self._only_115,
            "save_path": self._save_path,
            "movie_save_path": self._movie_save_path,
            "offline_download_path": self._offline_download_path,
            "movie_offline_download_path": self._movie_offline_download_path,
            "cookies": self._cookies,
            "pansou_enabled": self._pansou_enabled,
            "pansou_url": self._pansou_url,
            "pansou_username": self._pansou_username,
            "pansou_password": self._pansou_password,
            "pansou_auth_enabled": self._pansou_auth_enabled,
            "pansou_channels": self._pansou_channels,
            "pansou_cloud_types": self._pansou_cloud_types,
            "jackett_enabled": self._jackett_enabled,
            "jackett_url": self._jackett_url,
            "jackett_apikey": self._jackett_apikey,
            "jackett_tag": self._jackett_tag,
            "exclude_subscribes": self._exclude_subscribes,
            "block_system_subscribe": self._block_system_subscribe,
            "max_transfer_per_sync": self._max_transfer_per_sync,
            "batch_size": self._batch_size,
            "skip_other_season_dirs": self._skip_other_season_dirs,
            "unblock_site_ids": self._unblock_site_ids,
            "unblock_site_names": self._unblock_site_names,
            "unblock_delay_minutes": self._unblock_delay_minutes,
            "system_subscribe_window_hours": self._system_subscribe_window_hours,
            "unblock_window_hours": self._system_subscribe_window_hours,
        })

    # ------------------ stop ------------------

    def stop_service(self):
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception:
            pass
        try:
            if self._toggle_scheduler:
                self._toggle_scheduler.remove_all_jobs()
                if self._toggle_scheduler.running:
                    self._toggle_scheduler.shutdown()
                self._toggle_scheduler = None
        except Exception:
            pass

    # ======================================================================
    # 必备接口
    # ======================================================================

    def get_state(self) -> bool:
        return self._enabled

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return UIConfig.get_form()

    def get_page(self) -> Optional[List[dict]]:
        history = self.get_data('history') or []
        return UIConfig.get_page(history)

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {"path": "/sync_subscribes", "endpoint": self.sync_subscribes, "methods": ["GET"], "summary": "执行同步订阅追更"},
            {"path": "/clear_history", "endpoint": self.api_clear_history, "methods": ["POST"], "summary": "清空历史记录"},
            {"path": "/search_test", "endpoint": self.api_search_test, "methods": ["GET"], "summary": "搜索测试"},
        ]

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return [{
            "cmd": "/subtransfer115_action",
            "event": EventType.PluginAction,
            "desc": "SubTransfer115 订阅追更",
            "category": "订阅",
            "data": {"action": "subtransfer115_action"}
        }]

    def get_service(self) -> List[Dict[str, Any]]:
        if not self._enabled:
            return []

        if self._cron and self._cron_interval_ge_min_hours(self._cron, self._MIN_INTERVAL_HOURS):
            try:
                return [{
                    "id": "SubTransfer115", "name": "SubTransfer115 订阅追更服务",
                    "trigger": CronTrigger.from_crontab(self._cron),
                    "func": self.sync_subscribes, "kwargs": {}
                }]
            except Exception as e:
                logger.warning(f"Cron 表达式无效：{self._cron}，将回退 interval=8h。错误：{e}")

        return [{
            "id": "SubTransfer115", "name": "SubTransfer115 订阅追更服务",
            "trigger": "interval", "func": self.sync_subscribes, "kwargs": {"hours": 8}
        }]

    # ======================================================================
    # _do_sync
    # ======================================================================

    def _do_sync(self) -> bool:
        if not self._search_handler.get_enabled_sources():
            logger.error("没有启用的搜索源，无法执行")
            if self._notify:
                self.post_message(
                    mtype=NotificationType.Plugin,
                    title="【SubTransfer115】配置错误",
                    text="没有启用的搜索源，请在设置中启用 PanSou 或 Jackett。"
                )
            return False

        if not self._p115_manager:
            logger.error("115 客户端未初始化，请检查 Cookie 配置")
            return False

        if not self._p115_manager.check_login():
            logger.error("115 登录失败，Cookie 可能已过期")
            if self._notify:
                self.post_message(
                    mtype=NotificationType.Manual,
                    title="【SubTransfer115】登录失败",
                    text="115 Cookie 可能已过期，请更新后重试。"
                )
            return False

        logger.info("开始执行 115 网盘订阅同步...")
        if self._notify:
            self.post_message(
                mtype=NotificationType.Plugin,
                title="【SubTransfer115】开始执行",
                text="正在扫描订阅列表并同步缺失内容..."
            )

        try:
            self._p115_manager.reset_api_call_count()
        except Exception:
            pass
        try:
            if self._pansou_client:
                self._pansou_client.reset_api_call_count()
        except Exception:
            pass
        try:
            if self._jackett_client:
                self._jackett_client.reset_api_call_count()
        except Exception:
            pass

        with SessionFactory() as db:
            subscribes = SubscribeOper(db=db).list("N,R")

        if not subscribes:
            logger.info("无订阅数据")
            if self._notify:
                self.post_message(
                    mtype=NotificationType.Plugin,
                    title="【SubTransfer115】执行完成",
                    text="当前无订阅数据。"
                )
            return True

        tv_subscribes = [s for s in subscribes if s.type == MediaType.TV.value]
        movie_subscribes = [s for s in subscribes if s.type == MediaType.MOVIE.value]

        if not tv_subscribes and not movie_subscribes:
            logger.info("无电影/剧集订阅")
            return True

        history: List[dict] = self.get_data('history') or []
        transfer_details: List[Dict[str, Any]] = []
        transferred_count = 0

        exclude_ids = set(self._exclude_subscribes or [])

        for subscribe in movie_subscribes:
            if global_vars.is_system_stopped:
                break
            if subscribe.id in exclude_ids:
                continue
            transferred_count = self._sync_handler.process_movie_subscribe(
                subscribe=subscribe, history=history,
                transfer_details=transfer_details, transferred_count=transferred_count
            )

        for subscribe in tv_subscribes:
            if global_vars.is_system_stopped:
                break
            if subscribe.id in exclude_ids:
                continue
            transferred_count = self._sync_handler.process_tv_subscribe(
                subscribe=subscribe, history=history,
                transfer_details=transfer_details, transferred_count=transferred_count,
                exclude_ids=exclude_ids
            )

        self.save_data('history', history)
        logger.info(f"115 网盘订阅同步完成，共转存 {transferred_count} 个文件")

        if self._notify:
            if transferred_count > 0:
                self._sync_handler.send_transfer_notification(transfer_details, transferred_count)
            else:
                self.post_message(
                    mtype=NotificationType.Plugin,
                    title="【SubTransfer115】执行完成",
                    text="本次同步未发现需要转存的新资源。"
                )

        return True

    # ------------------ API / 同步入口 ------------------

    def api_clear_history(self, apikey: str) -> dict:
        return self._api_handler.clear_history(apikey)

    def sync_subscribes(self):
        with lock:
            tz = pytz.timezone(settings.TZ)
            run_start = datetime.datetime.now(tz=tz)
            success = False
            try:
                success = self._do_sync()
            except Exception as e:
                logger.error(f"同步任务异常：{e}")
            finally:
                if success and self._is_last_run_today(run_start):
                    if int(self._unblock_delay_minutes) < 0 or (not self._window_enabled()):
                        self._enter_blocked(reason="触发条件1")
                    else:
                        self._schedule_unblock_after_delay(datetime.datetime.now(tz=pytz.timezone(settings.TZ)))

    def api_search(self, keyword: str, apikey: str) -> dict:
        return self._api_handler.search(keyword, apikey)

    def api_transfer(self, share_url: str, save_path: str, apikey: str) -> dict:
        return self._api_handler.transfer(share_url, save_path, apikey)

    def api_list_directories(self, path: str = "/", apikey: str = "") -> dict:
        return self._api_handler.list_directories(path, apikey)

    def api_search_test(self, keyword: str, source: str = "pansou", apikey: str = "") -> dict:
        """
        API: 搜索测试 — 输入关键词选择搜索源进行搜索测试
        """
        if apikey != settings.API_TOKEN:
            return {"error": "API密钥错误"}
        if not keyword or not keyword.strip():
            return {"error": "关键词不能为空"}
        return self._api_handler.search_test(keyword.strip(), source)

    @eventmanager.register(EventType.PluginAction)
    def remote_sync(self, event: Event):
        if not event:
            return
        event_data = event.event_data
        if not event_data or event_data.get("action") != "subtransfer115_action":
            return
        logger.info("收到命令，开始执行追更任务")
        self.post_message(
            mtype=NotificationType.Plugin, channel=event_data.get("channel"),
            title="【SubTransfer115】开始执行",
            text="已收到远程命令，正在执行追更任务...",
            userid=event_data.get("user")
        )
        self.sync_subscribes()
        self.post_message(
            mtype=NotificationType.Plugin, channel=event_data.get("channel"),
            title="【SubTransfer115】执行完成",
            text="远程触发的追更任务已完成。",
            userid=event_data.get("user")
        )
