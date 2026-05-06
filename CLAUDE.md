# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

MoviePilot 插件项目，SubTransfer115 插件通过 PanSou / Jackett 搜索 115 网盘资源和种子资源并转存到个人网盘，配合 STRM 助手实现完整的自动化追剧流程。

## Plugin Development

### Plugin Structure

```
plugins.v2/{plugin_id}/
├── __init__.py        # Required: Plugin main class inheriting _PluginBase
├── clients/           # External service API clients
├── handlers/          # Business logic processors
├── ui/                # UI form/page configuration
├── utils/             # Utility functions
└── lib/               # Native libraries or third-party dependencies
```

### Plugin Class Template

```python
from app.plugins import _PluginBase

class MyPlugin(_PluginBase):
    plugin_name = "插件名称"
    plugin_desc = "插件描述"
    plugin_version = "1.0.0"
    plugin_author = "作者"

    def init_plugin(self, config: dict = None): pass
    def get_state(self) -> bool: return self._enabled
    def get_form(self): return [], {}
    def get_page(self): return None  # Optional
    def get_api(self): return []     # Optional
    def get_service(self): return [] # Optional:定时任务
    def stop_service(self): pass
```

### Plugin Registration

在 `package.v2.json` 中注册插件元数据：

```json
{
  "PluginId": {
    "name": "显示名称",
    "release": true,
    "description": "描述",
    "version": "1.0.0",
    "icon": "图标URL",
    "author": "作者",
    "level": 1
  }
}
```

## Available MoviePilot APIs

| Import | Usage |
|--------|-------|
| `app.core.config.settings` | 系统配置（时区、代理等） |
| `app.log.logger` | 日志记录 |
| `app.schemas.types.MediaType` | MOVIE/TV 枚举 |
| `app.schemas.types.EventType` | 事件类型枚举 |
| `app.core.metainfo.MetaInfo` | 元数据解析 |
| `app.chain.download.DownloadChain` | 下载链 |
| `app.chain.subscribe.SubscribeChain` | 订阅链 |
| `app.db.subscribe_oper.SubscribeOper` | 订阅操作 |

## Release

GitHub Actions 自动发布：修改 `package.v2.json` 后触发，自动打包 `plugins.v2/` 下对应插件并创建 Release。

### 版本号更新

升级版本号时，需要同步更新以下**两个文件**：

1. `plugins.v2/subtransfer115/__init__.py` — `plugin_version` 字段
2. `package.v2.json` — `version` 字段 + `history` 中新增对应版本的 changelog 条目

## Dependencies

当前插件依赖：
- `p115client>=0.0.8.2`
- `sqlitetools>=0.0.7`
