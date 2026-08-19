"""日志初始化模块，导出全局可复用日志实例。"""

import logging

_PLUGIN_TAG = "astrbot_plugin_media_parser"


class _PluginTagFilter(logging.Filter):
    """保证每条日志都带有 ``plugin_tag`` 字段。

    AstrBot 的部分日志 Formatter 会以 ``%(plugin_tag)s`` 渲染，该字段通常由
    AstrBot 在插件钩子上下文中注入。当插件在钩子上下文之外（后台任务、子线程、
    或本模块级 ``logger`` 的深层调用链）打日志时，若字段缺失会导致
    ``Formatting field not found in record: 'plugin_tag'`` 异常，进而被 AstrBot
    误报为「处理函数 auto_parse 异常」。此处兜底补齐，避免不影响业务逻辑的
    日志格式化错误冒泡、并掩盖真正的异常。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "plugin_tag", ""):
            record.plugin_tag = _PLUGIN_TAG
        return True


try:
    from astrbot.api import logger as _astrbot_logger

    get_child = getattr(_astrbot_logger, "getChild", None)
    logger = (
        get_child(_PLUGIN_TAG)
        if callable(get_child)
        else _astrbot_logger
    )
    logger.addFilter(_PluginTagFilter())
except ImportError:
    logger = logging.getLogger(_PLUGIN_TAG)
    logger.addFilter(_PluginTagFilter())
