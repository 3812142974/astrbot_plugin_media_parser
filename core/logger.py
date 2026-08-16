"""日志初始化模块，导出全局可复用日志实例。"""

try:
    from astrbot.api import logger as _astrbot_logger

    get_child = getattr(_astrbot_logger, "getChild", None)
    logger = (
        get_child("astrbot_plugin_media_parser")
        if callable(get_child)
        else _astrbot_logger
    )
except ImportError:
    import logging

    logger = logging.getLogger("astrbot_plugin_media_parser")
