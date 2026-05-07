"""
同步处理模块
负责核心的同步逻辑：处理电影订阅、处理电视剧订阅
"""
import datetime
from typing import List, Dict, Any, Set, Optional, Callable

from app.core.config import global_vars
from app.core.metainfo import MetaInfo
from app.chain.download import DownloadChain
from app.db import SessionFactory
from app.db.subscribe_oper import SubscribeOper
from app.db.downloadhistory_oper import DownloadHistoryOper
from app.log import logger
from app.schemas import MediaInfo
from app.schemas.types import MediaType, NotificationType
from app.utils.string import StringUtils

from ..utils import FileMatcher, SubscribeFilter
from .search import SearchHandler
from .subscribe import SubscribeHandler


class SyncHandler:
    """同步处理器"""

    def __init__(
        self,
        p115_manager,
        search_handler: SearchHandler,
        subscribe_handler: SubscribeHandler,
        chain,
        save_path: str,
        movie_save_path: str,
        offline_download_path: str = "",
        movie_offline_download_path: str = "",
        max_transfer_per_sync: int = 50,
        batch_size: int = 20,
        skip_other_season_dirs: bool = True,
        notify: bool = False,
        post_message_func: Callable = None,
        get_data_func: Callable = None,
        save_data_func: Callable = None
    ):
        self._p115_manager = p115_manager
        self._search_handler = search_handler
        self._subscribe_handler = subscribe_handler
        self._chain = chain
        self._save_path = save_path
        self._movie_save_path = movie_save_path
        self._offline_download_path = offline_download_path or save_path
        self._movie_offline_download_path = movie_offline_download_path or movie_save_path
        self._max_transfer_per_sync = max_transfer_per_sync
        self._batch_size = batch_size
        self._skip_other_season_dirs = skip_other_season_dirs
        self._notify = notify
        self._post_message = post_message_func
        self._get_data = get_data_func
        self._save_data = save_data_func

    def process_movie_subscribe(
        self, subscribe, history: List[dict],
        transfer_details: List[Dict[str, Any]], transferred_count: int
    ) -> int:
        try:
            logger.info(f"处理电影订阅：{subscribe.name} ({subscribe.year})")

            movie_history_score = -1
            movie_perfect_match = False
            for h in history:
                if (h.get("title") == subscribe.name
                        and h.get("type") == "电影"
                        and h.get("status") == "成功"):
                    score = h.get("filter_score", 0)
                    perfect = h.get("perfect_match", False)
                    if score > movie_history_score:
                        movie_history_score = score
                        movie_perfect_match = perfect

            is_best_version = bool(subscribe.best_version)

            if movie_history_score >= 0:
                if not is_best_version or movie_perfect_match:
                    logger.info(f"电影 {subscribe.name} 已在历史记录中(洗版:{is_best_version}, 完美匹配:{movie_perfect_match})，跳过")
                    return transferred_count
                else:
                    logger.info(f"电影 {subscribe.name} 洗版中，历史分数 {movie_history_score}，尝试寻找更优资源")

            meta = MetaInfo(subscribe.name)
            meta.year = subscribe.year
            meta.type = MediaType.MOVIE

            mediainfo: MediaInfo = self._chain.recognize_media(
                meta=meta, mtype=MediaType.MOVIE,
                tmdbid=subscribe.tmdbid, doubanid=subscribe.doubanid, cache=True
            )
            if not mediainfo:
                logger.warn(f"无法识别媒体信息：{subscribe.name}")
                return transferred_count

            p115_results = self._search_handler.search_resources(
                mediainfo=mediainfo, media_type=MediaType.MOVIE
            )

            if not p115_results:
                logger.info(f"未找到电影 {mediainfo.title} 的 115 网盘资源")
                return transferred_count

            logger.info(f"找到 {len(p115_results)} 个 115 网盘资源")

            subscribe_filter = SubscribeFilter(
                quality=subscribe.quality, resolution=subscribe.resolution,
                effect=subscribe.effect, strict=not is_best_version
            )
            if subscribe_filter.has_filters():
                mode_text = "洗版模式" if is_best_version else "严格模式"
                logger.info(f"电影 {subscribe.name} 过滤条件({mode_text}) - 质量: {subscribe.quality}, 分辨率: {subscribe.resolution}, 特效: {subscribe.effect}")

            movie_transferred = False
            for resource in p115_results:
                if movie_transferred:
                    break

                share_url = resource.get("url", "")
                resource_title = resource.get("title", "")
                pan_type = resource.get("pan_type", "115")

                if not share_url:
                    continue

                logger.info(f"检查资源 ({pan_type})：{resource_title} - {share_url[:50]}...")

                try:
                    if pan_type == "115":
                        share_status = self._p115_manager.check_share_status(share_url)
                        if not share_status.is_valid:
                            logger.warning(f"分享链接无效：{share_url}，原因：{share_status.status_text}")
                            continue

                        share_files = self._p115_manager.list_share_files(share_url)
                        if not share_files:
                            logger.info(f"分享链接无内容：{share_url}")
                            continue

                        matched_file = FileMatcher.match_movie_file(
                            share_files, mediainfo.title, subscribe_filter=subscribe_filter
                        )

                        if not matched_file:
                            logger.info("未找到匹配的电影文件")
                            continue

                        file_name = matched_file.get('name', '')
                        logger.info(f"找到匹配文件：{file_name}")

                        _, current_score = subscribe_filter.match(file_name) if subscribe_filter.has_filters() else (True, 0)
                        is_perfect = subscribe_filter.is_perfect_match(file_name) if subscribe_filter.has_filters() else True

                        if is_best_version and movie_history_score >= 0:
                            if current_score <= movie_history_score:
                                logger.info(f"电影 {mediainfo.title} 已有分数 {movie_history_score}，当前 {current_score}，跳过")
                                continue
                            else:
                                logger.info(f"电影 {mediainfo.title} 洗版：旧分数 {movie_history_score} -> 新分数 {current_score}")

                        save_dir = f"{self._movie_save_path}/{mediainfo.title} ({mediainfo.year})" if mediainfo.year else f"{self._movie_save_path}/{mediainfo.title}"
                        logger.info(f"转存目标路径: {save_dir}")

                        success = self._p115_manager.transfer_file(
                            share_url=share_url, file_id=matched_file.get("id"), save_path=save_dir
                        )

                        history_item = {
                            "title": mediainfo.title, "year": mediainfo.year,
                            "type": "电影", "status": "成功" if success else "失败",
                            "share_url": share_url, "file_name": file_name,
                            "filter_score": current_score, "perfect_match": is_perfect,
                            "pan_type": pan_type,
                            "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        }
                        history.append(history_item)

                        if success:
                            transferred_count += 1
                            movie_transferred = True
                            movie_history_score = current_score
                            score_info = f"(分数:{current_score}, 完美匹配:{is_perfect})" if subscribe_filter.has_filters() else ""
                            logger.info(f"成功转存电影：{mediainfo.title} {score_info}")

                            transfer_details.append({
                                "type": "电影", "title": mediainfo.title,
                                "year": mediainfo.year, "image": mediainfo.get_poster_image(),
                                "file_name": file_name
                            })

                            try:
                                DownloadHistoryOper().add(
                                    path=save_dir, type=mediainfo.type.value,
                                    title=mediainfo.title, year=mediainfo.year,
                                    tmdbid=mediainfo.tmdb_id, imdbid=mediainfo.imdb_id,
                                    tvdbid=mediainfo.tvdb_id, doubanid=mediainfo.douban_id,
                                    image=mediainfo.get_poster_image(), downloader="115网盘",
                                    download_hash=matched_file.get("id"),
                                    torrent_name=resource_title,
                                    torrent_description=file_name, torrent_site="115网盘",
                                    username="SubTransfer115",
                                    date=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                    note={"source": f"Subscribe|{subscribe.name}", "share_url": share_url}
                                )
                                logger.debug(f"已记录电影 {mediainfo.title} 下载历史")
                            except Exception as e:
                                logger.warning(f"记录下载历史失败：{e}")

                            self._subscribe_handler.check_and_finish_subscribe(
                                subscribe=subscribe, mediainfo=mediainfo, success_episodes=[1]
                            )
                        else:
                            logger.error(f"转存失败：{mediainfo.title}")
                    else:
                        if pan_type in ("magnet", "ed2k"):
                            save_dir = f"{self._movie_offline_download_path}/{mediainfo.title} ({mediainfo.year})" if mediainfo.year else f"{self._movie_offline_download_path}/{mediainfo.title}"
                            logger.info(f"添加离线下载任务：{pan_type} - {share_url[:50]}...，保存到: {save_dir}")
                            success = self._p115_manager.add_offline_task(share_url, save_path=save_dir)

                            history_item = {
                                "title": mediainfo.title, "year": mediainfo.year,
                                "type": "电影", "status": "成功" if success else "失败",
                                "share_url": share_url, "file_name": resource_title,
                                "filter_score": 0, "perfect_match": False,
                                "pan_type": pan_type,
                                "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            }
                            history.append(history_item)

                            if success:
                                transferred_count += 1
                                movie_transferred = True
                                logger.info(f"成功添加离线下载任务：{mediainfo.title} ({pan_type})")
                                transfer_details.append({
                                    "type": "电影", "title": mediainfo.title,
                                    "year": mediainfo.year, "image": mediainfo.get_poster_image(),
                                    "file_name": f"[离线下载] {resource_title}"
                                })
                                self._subscribe_handler.check_and_finish_subscribe(
                                    subscribe=subscribe, mediainfo=mediainfo, success_episodes=[1]
                                )
                            else:
                                logger.error(f"离线下载任务添加失败：{mediainfo.title}")

                except Exception as e:
                    logger.error(f"处理资源出错：{share_url[:50]}, 错误：{str(e)}")
                    continue

        except Exception as e:
            logger.error(f"处理电影订阅 {subscribe.name} 出错：{str(e)}")

        return transferred_count

    def process_tv_subscribe(
        self, subscribe, history: List[dict],
        transfer_details: List[Dict[str, Any]], transferred_count: int,
        exclude_ids: Set[int]
    ) -> int:
        try:
            logger.info(f"订阅信息：{subscribe.name}，开始集数：{subscribe.start_episode}, 总集数：{subscribe.total_episode}, 缺失集数：{subscribe.lack_episode}")
            logger.info(f"处理订阅：{subscribe.name} (S{subscribe.season or 1})")

            if subscribe.lack_episode == 0:
                logger.info(f"{subscribe.name} S{subscribe.season or 1} 订阅显示媒体库已完整(lack_episode=0)，跳过")
                return transferred_count

            meta = MetaInfo(subscribe.name)
            meta.year = subscribe.year
            meta.begin_season = subscribe.season or 1
            meta.type = MediaType.TV

            mediainfo: MediaInfo = self._chain.recognize_media(
                meta=meta, mtype=MediaType.TV,
                tmdbid=subscribe.tmdbid, doubanid=subscribe.doubanid, cache=True
            )

            if not mediainfo:
                logger.warn(f"无法识别媒体信息：{subscribe.name}")
                return transferred_count

            totals = {}
            if subscribe.season and subscribe.total_episode:
                totals = {subscribe.season: subscribe.total_episode}

            downloadchain = DownloadChain()
            exist_flag, no_exists = downloadchain.get_no_exists_info(
                meta=meta, mediainfo=mediainfo, totals=totals
            )

            if exist_flag:
                logger.info(f"{mediainfo.title_year} S{meta.begin_season} 媒体库中已完整存在")
                total_ep = subscribe.total_episode or 0
                start_ep = subscribe.start_episode or 1
                if total_ep > 0:
                    all_episodes = list(range(start_ep, total_ep + 1))
                    self._subscribe_handler.check_and_finish_subscribe(
                        subscribe=subscribe, mediainfo=mediainfo, success_episodes=all_episodes
                    )
                elif subscribe.lack_episode != 0:
                    SubscribeOper().update(subscribe.id, {"lack_episode": 0})
                return transferred_count

            season = meta.begin_season or 1
            missing_episodes = []
            mediakey = mediainfo.tmdb_id or mediainfo.douban_id

            if no_exists and mediakey:
                season_info = no_exists.get(mediakey, {})
                not_exist_info = season_info.get(season)
                if not_exist_info:
                    missing_episodes = not_exist_info.episodes or []
                    if not missing_episodes and not_exist_info.total_episode:
                        start_ep = not_exist_info.start_episode or 1
                        missing_episodes = list(range(start_ep, not_exist_info.total_episode + 1))

            if not missing_episodes:
                logger.info(f"{mediainfo.title_year} S{season} 没有缺失剧集信息")
                return transferred_count

            if subscribe.start_episode:
                original_count = len(missing_episodes)
                missing_episodes = [ep for ep in missing_episodes if ep >= subscribe.start_episode]
                if len(missing_episodes) < original_count:
                    logger.info(f"根据订阅设置，过滤掉小于 {subscribe.start_episode} 的剧集")

            is_best_version = bool(subscribe.best_version)

            transferred_episodes = set()
            episode_history_scores: Dict[int, int] = {}
            for h in history:
                if (h.get("title") == mediainfo.title
                        and h.get("season") == season
                        and h.get("status") == "成功"):
                    ep = h.get("episode")
                    score = h.get("filter_score", 0)
                    perfect = h.get("perfect_match", False)
                    if not is_best_version:
                        transferred_episodes.add(ep)
                    else:
                        if perfect:
                            transferred_episodes.add(ep)
                        else:
                            if ep not in episode_history_scores or score > episode_history_scores[ep]:
                                episode_history_scores[ep] = score

            show_folder = f"{mediainfo.title} ({mediainfo.year})" if mediainfo.year else mediainfo.title
            save_dir = f"{self._save_path}/{show_folder}/Season {season}"

            existing_episodes_in_cloud = FileMatcher.check_existing_episodes(
                self._p115_manager, mediainfo, season, save_dir
            )

            all_existing = transferred_episodes | existing_episodes_in_cloud

            if is_best_version and episode_history_scores:
                episodes_to_upgrade = set(episode_history_scores.keys())
                all_existing = all_existing - episodes_to_upgrade
                if episodes_to_upgrade:
                    logger.info(f"{mediainfo.title_year} S{season} 洗版模式：{len(episodes_to_upgrade)} 集待升级")

            if all_existing:
                missing_episodes = [ep for ep in missing_episodes if ep not in all_existing]
                logger.info(
                    f"{mediainfo.title_year} S{season} 跳过已存在的 {len(all_existing)} 集 "
                    f"(历史记录:{len(transferred_episodes)}, 网盘:{len(existing_episodes_in_cloud)})"
                )

            if not missing_episodes:
                logger.info(f"{mediainfo.title_year} S{season} 所有缺失剧集已存在于网盘")
                if existing_episodes_in_cloud:
                    self._subscribe_handler.check_and_finish_subscribe(
                        subscribe=subscribe, mediainfo=mediainfo,
                        success_episodes=list(existing_episodes_in_cloud)
                    )
                return transferred_count

            if mediainfo.tmdb_id:
                try:
                    from app.chain.tmdb import TmdbChain
                    tmdb_episodes = TmdbChain().tmdb_episodes(tmdbid=mediainfo.tmdb_id, season=season)
                    if tmdb_episodes:
                        today = datetime.date.today().isoformat()
                        aired_episodes = set()
                        for ep in tmdb_episodes:
                            if ep.air_date and ep.air_date <= today and ep.episode_number:
                                aired_episodes.add(ep.episode_number)
                        if aired_episodes:
                            not_aired = [ep for ep in missing_episodes if ep not in aired_episodes]
                            if not_aired:
                                missing_episodes = [ep for ep in missing_episodes if ep in aired_episodes]
                                logger.info(f"{mediainfo.title_year} S{season} 跳过 {len(not_aired)} 集未播出剧集：{not_aired}")
                                if not missing_episodes:
                                    logger.info(f"{mediainfo.title_year} S{season} 所有缺失剧集均未播出，跳过")
                                    return transferred_count
                except Exception as e:
                    logger.warning(f"{mediainfo.title_year} S{season} 查询TMDB剧集播出日期失败：{e}，将继续处理所有缺失剧集")

            logger.info(f"{mediainfo.title_year} S{season} 待转存剧集：{missing_episodes}")

            subscribe_filter = SubscribeFilter(
                quality=subscribe.quality, resolution=subscribe.resolution,
                effect=subscribe.effect, strict=not is_best_version
            )
            if subscribe_filter.has_filters():
                mode_text = "洗版模式" if is_best_version else "严格模式"
                logger.info(f"{mediainfo.title} S{season} 过滤条件({mode_text}) - 质量: {subscribe.quality}, 分辨率: {subscribe.resolution}, 特效: {subscribe.effect}")

            success_episodes = []

            if not self._search_handler.get_enabled_sources():
                logger.warning(f"没有可用的搜索源，跳过 {mediainfo.title} S{season} 的搜索")
                return transferred_count

            if transferred_count >= self._max_transfer_per_sync:
                logger.info(f"已达单次同步上限 {self._max_transfer_per_sync}，剩余 {len(missing_episodes)} 集将在下次同步处理")
                return transferred_count

            logger.info(f"开始搜索 {mediainfo.title} S{season}（缺失: {len(missing_episodes)} 集）")

            p115_results = self._search_handler.search_resources(
                mediainfo=mediainfo,
                media_type=MediaType.TV, season=season
            )

            if not p115_results:
                logger.info(f"未找到资源")
                return transferred_count

            logger.info(f"找到 {len(p115_results)} 个资源")

            for resource in p115_results:
                if transferred_count >= self._max_transfer_per_sync:
                    logger.info(f"已达单次同步上限 {self._max_transfer_per_sync}")
                    break

                share_url = resource.get("url", "")
                resource_title = resource.get("title", "")
                pan_type = resource.get("pan_type", "115")

                if not share_url:
                    continue

                logger.info(f"检查资源 ({pan_type})：{resource_title} - {share_url[:50]}...")

                try:
                    if pan_type == "115":
                        share_status = self._p115_manager.check_share_status(share_url)
                        if not share_status.is_valid:
                            logger.warning(f"分享链接无效：{share_url}，原因：{share_status.status_text}")
                            continue

                        share_files = self._p115_manager.list_share_files(
                            share_url, target_season=(season if self._skip_other_season_dirs else None)
                        )
                        if not share_files:
                            logger.info(f"分享链接无内容：{share_url}")
                            continue

                        logger.info(f"分享包含 {len(share_files)} 个文件/目录")

                        matched_items = []
                        for episode in missing_episodes[:]:
                            matched_file = FileMatcher.match_episode_file(
                                share_files, mediainfo.title, season, episode,
                                subscribe_filter=subscribe_filter
                            )
                            if matched_file:
                                file_name = matched_file.get('name', '')
                                logger.info(f"找到匹配文件：{file_name} -> E{episode:02d}")

                                _, current_score = subscribe_filter.match(file_name) if subscribe_filter.has_filters() else (True, 0)
                                is_perfect = subscribe_filter.is_perfect_match(file_name) if subscribe_filter.has_filters() else True

                                is_upgrade = False
                                if is_best_version and episode in episode_history_scores:
                                    old_score = episode_history_scores[episode]
                                    if current_score <= old_score:
                                        logger.info(f"E{episode:02d} 已有分数 {old_score}，当前 {current_score}，跳过")
                                        continue
                                    else:
                                        logger.info(f"E{episode:02d} 洗版：旧分数 {old_score} -> 新分数 {current_score}")
                                        is_upgrade = True

                                matched_items.append({
                                    "file": matched_file, "episode": episode,
                                    "score": current_score, "is_perfect": is_perfect,
                                    "is_upgrade": is_upgrade
                                })

                        if not matched_items:
                            logger.info(f"该分享未匹配到 S{season} 的任何缺失剧集")
                            continue

                        remaining_quota = self._max_transfer_per_sync - transferred_count
                        if len(matched_items) > remaining_quota:
                            logger.info(f"匹配 {len(matched_items)} 集，但受配额限制仅转存 {remaining_quota} 集")
                            matched_items = matched_items[:remaining_quota]

                        file_ids = [item["file"]["id"] for item in matched_items]
                        logger.info(f"准备批量转存 {len(file_ids)} 个文件到: {save_dir}")

                        success_ids, failed_ids = self._p115_manager.transfer_files_batch(
                            share_url=share_url, file_ids=file_ids,
                            save_path=save_dir, batch_size=self._batch_size
                        )

                        success_id_set = set(success_ids)
                        batch_success_episodes = []

                        for item in matched_items:
                            file_id = item["file"]["id"]
                            episode = item["episode"]
                            file_name = item["file"]["name"]
                            current_score = item["score"]
                            is_perfect = item["is_perfect"]
                            is_upgrade = item["is_upgrade"]
                            success = file_id in success_id_set

                            history_item = {
                                "title": mediainfo.title, "season": season,
                                "episode": episode, "type": "电视剧",
                                "status": "成功" if success else "失败",
                                "share_url": share_url, "file_name": file_name,
                                "filter_score": current_score, "perfect_match": is_perfect,
                                "pan_type": pan_type,
                                "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            }
                            history.append(history_item)

                            if success:
                                transferred_count += 1
                                episode_history_scores[episode] = current_score
                                if episode in missing_episodes:
                                    missing_episodes.remove(episode)
                                if not is_upgrade:
                                    success_episodes.append(episode)

                                score_info = f"(分数:{current_score}, 完美匹配:{is_perfect})" if subscribe_filter.has_filters() else ""
                                upgrade_info = " [洗版升级]" if is_upgrade else ""
                                logger.info(f"成功转存：{mediainfo.title} S{season:02d}E{episode:02d} {score_info}{upgrade_info}")

                                existing_detail = next(
                                    (d for d in transfer_details
                                     if d.get("title") == mediainfo.title and d.get("season") == season), None
                                )
                                if existing_detail:
                                    existing_detail["episodes"].append(episode)
                                else:
                                    transfer_details.append({
                                        "type": "电视剧", "title": mediainfo.title,
                                        "year": mediainfo.year, "season": season,
                                        "episodes": [episode],
                                        "image": mediainfo.get_poster_image()
                                    })
                                batch_success_episodes.append(episode)
                            else:
                                logger.error(f"转存失败：{mediainfo.title} S{season:02d}E{episode:02d}")

                        if batch_success_episodes:
                            try:
                                episodes_str = StringUtils.format_ep(batch_success_episodes)
                                DownloadHistoryOper().add(
                                    path=save_dir, type=mediainfo.type.value,
                                    title=mediainfo.title, year=mediainfo.year,
                                    tmdbid=mediainfo.tmdb_id, imdbid=mediainfo.imdb_id,
                                    tvdbid=mediainfo.tvdb_id, doubanid=mediainfo.douban_id,
                                    seasons=f"S{season:02d}", episodes=episodes_str,
                                    image=mediainfo.get_poster_image(), downloader="115网盘",
                                    download_hash=share_url, torrent_name=resource_title,
                                    torrent_site="115网盘", username="SubTransfer115",
                                    date=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                    note={"source": f"Subscribe|{subscribe.name}", "share_url": share_url}
                                )
                                logger.debug(f"已记录 {mediainfo.title} S{season:02d} {episodes_str} 下载历史")
                            except Exception as e:
                                logger.warning(f"记录下载历史失败：{e}")

                        if not missing_episodes:
                            break
                    else:
                        if pan_type in ("magnet", "ed2k"):
                            offline_save_dir = f"{self._offline_download_path}/{show_folder}/Season {season}"
                            logger.info(f"添加离线下载任务：{pan_type} - {share_url[:50]}...，保存到: {offline_save_dir}")
                            success = self._p115_manager.add_offline_task(share_url, save_path=offline_save_dir)

                            history_item = {
                                "title": mediainfo.title, "season": season,
                                "episode": 0, "type": "电视剧",
                                "status": "成功" if success else "失败",
                                "share_url": share_url, "file_name": resource_title,
                                "filter_score": 0, "perfect_match": False,
                                "pan_type": pan_type,
                                "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            }
                            history.append(history_item)

                            if success:
                                transferred_count += 1
                                logger.info(f"成功添加离线下载任务：{mediainfo.title} S{season} ({pan_type})")
                                transfer_details.append({
                                    "type": "电视剧", "title": mediainfo.title,
                                    "year": mediainfo.year, "season": season,
                                    "episodes": list(missing_episodes),
                                    "image": mediainfo.get_poster_image()
                                })
                                success_episodes.extend(list(missing_episodes))
                                missing_episodes.clear()
                                break
                            else:
                                logger.error(f"离线下载任务添加失败：{mediainfo.title} S{season}")

                except Exception as e:
                    logger.error(f"处理资源出错：{share_url[:50]}, 错误：{str(e)}")
                    continue

            all_success_episodes = list(set(success_episodes) | existing_episodes_in_cloud)
            if all_success_episodes:
                self._subscribe_handler.check_and_finish_subscribe(
                    subscribe=subscribe, mediainfo=mediainfo,
                    success_episodes=all_success_episodes
                )

        except Exception as e:
            logger.error(f"处理订阅 {subscribe.name} 出错：{str(e)}")

        return transferred_count

    def send_transfer_notification(self, transfer_details: List[Dict[str, Any]], total_count: int):
        if not transfer_details or not self._post_message:
            return

        text_lines = []
        first_image = None

        for detail in transfer_details:
            if detail.get("type") == "电影":
                title = detail.get("title", "未知")
                year = detail.get("year", "")
                text_lines.append(f"{title} ({year})")
                if not first_image and detail.get("image"):
                    first_image = detail.get("image")
            else:
                title = detail.get("title", "未知")
                season = detail.get("season", 1)
                episodes = detail.get("episodes", [])
                episodes.sort()
                if len(episodes) <= 5:
                    ep_str = ", ".join([f"E{e:02d}" for e in episodes])
                else:
                    ep_str = f"E{episodes[0]:02d}-E{episodes[-1]:02d} 共{len(episodes)}集"
                text_lines.append(f"{title} S{season:02d} {ep_str}")
                if not first_image and detail.get("image"):
                    first_image = detail.get("image")

        if len(text_lines) > 10:
            text_lines = text_lines[:10]
            text_lines.append(f"... 等共 {len(transfer_details)} 项")

        self._post_message(
            mtype=NotificationType.Plugin,
            title="【SubTransfer115】转存完成",
            text=f"本次共转存 {total_count} 个文件\n\n" + "\n".join(text_lines)
        )
