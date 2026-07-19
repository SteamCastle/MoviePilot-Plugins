import threading
from queue import Queue
from time import time, sleep
from typing import Any, List, Dict, Tuple
import json

from app.core.event import eventmanager, Event
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType, NotificationType
from app.utils.http import RequestUtils


class HermesMsg(_PluginBase):
    plugin_name = "Hermes消息通知"
    plugin_desc = "将MoviePilot消息通知通过Hermes转发到微信。"
    plugin_icon = "hermes.png"
    plugin_version = "1.0"
    plugin_author = "xzh"
    author_url = "https://github.com/NousResearch/hermes-agent"
    plugin_config_prefix = "hermesmsg_"
    plugin_order = 26
    auth_level = 1

    _enabled = False
    _webhook_url = None
    _msgtypes = []

    processing_thread = None
    last_send_time = 0
    message_queue = Queue()
    send_interval = 3
    __event = threading.Event()

    def init_plugin(self, config: dict = None):
        self.__event.clear()
        if config:
            self._enabled = config.get("enabled")
            self._webhook_url = config.get("webhook_url")
            self._msgtypes = config.get("msgtypes") or []

            if self._enabled and self._webhook_url:
                self.processing_thread = threading.Thread(target=self.process_queue)
                self.processing_thread.daemon = True
                self.processing_thread.start()

    def get_state(self) -> bool:
        return self._enabled and bool(self._webhook_url)

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        MsgTypeOptions = []
        for item in NotificationType:
            MsgTypeOptions.append({
                "title": item.value,
                "value": item.name
            })
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [{
                            'component': 'VCol',
                            'props': {'cols': 12, 'md': 6},
                            'content': [{
                                'component': 'VSwitch',
                                'props': {
                                    'model': 'enabled',
                                    'label': '启用插件',
                                }
                            }]
                        }]
                    },
                    {
                        'component': 'VRow',
                        'content': [{
                            'component': 'VCol',
                            'props': {'cols': 12},
                            'content': [{
                                'component': 'VTextField',
                                'props': {
                                    'model': 'webhook_url',
                                    'label': 'Webhook地址',
                                    'placeholder': 'http://192.168.66.62:18644',
                                }
                            }]
                        }]
                    },
                    {
                        'component': 'VRow',
                        'content': [{
                            'component': 'VCol',
                            'props': {'cols': 12},
                            'content': [{
                                'component': 'VSelect',
                                'props': {
                                    'multiple': True,
                                    'chips': True,
                                    'model': 'msgtypes',
                                    'label': '消息类型',
                                    'items': MsgTypeOptions
                                }
                            }]
                        }]
                    },
                ]
            }
        ], {
            "enabled": False,
            'webhook_url': 'http://192.168.66.62:18644',
            'msgtypes': ['Download', 'Organize', 'Subscribe', 'Manual']
        }

    def get_page(self) -> List[dict]:
        pass

    @eventmanager.register(EventType.NoticeMessage)
    def send(self, event: Event):
        if not self.get_state() or not event.event_data:
            return

        msg_body = event.event_data
        if not msg_body.get("title") and not msg_body.get("text"):
            return

        self.message_queue.put(msg_body)
        logger.info("Hermes消息已加入队列")

    def process_queue(self):
        while True:
            if self.__event.is_set():
                break
            msg_body = self.message_queue.get()

            current_time = time()
            time_since_last_send = current_time - self.last_send_time
            if time_since_last_send < self.send_interval:
                sleep(self.send_interval - time_since_last_send)

            channel = msg_body.get("channel")
            if channel:
                continue
            msg_type = msg_body.get("type")
            title = msg_body.get("title")
            text = msg_body.get("text")

            if msg_type and self._msgtypes and msg_type.name not in self._msgtypes:
                continue

            try:
                payload = {"title": title, "text": text or ""}
                res = RequestUtils(content_type="application/json").post(
                    self._webhook_url, data=json.dumps(payload)
                )
                if res and res.status_code == 200:
                    logger.info("Hermes消息发送成功")
                    self.last_send_time = time()
                elif res is not None:
                    logger.warn(f"Hermes消息发送失败，状态码：{res.status_code}")
                else:
                    logger.warn("Hermes消息发送失败，无响应")
            except Exception as e:
                logger.error(f"Hermes消息发送异常：{str(e)}")

            self.message_queue.task_done()

    def stop_service(self):
        self.__event.set()
