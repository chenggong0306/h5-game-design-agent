"""企业级结构化日志配置

特性：
- 结构化日志（structlog → 控制台彩色 / 文件 JSON）
- 按日期自动滚动（TimedRotatingFileHandler，午夜切换）
- 只在首次 get_logger() 时初始化（import 无副作用）
- 包含时间戳、级别、模块、函数、行号
- 支持额外的上下文字段（session_id, tool_name 等）
"""

import sys
import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
import structlog


LOG_DIR = Path("logs")

_logger_initialized = False


def _ensure_logging(level: str = "INFO"):
    """延迟初始化日志系统（首次调用时自动配置，import 无副作用）。"""
    global _logger_initialized
    if _logger_initialized:
        return
    _logger_initialized = True

    LOG_DIR.mkdir(exist_ok=True)

    # 配置 structlog 处理器链
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.CallsiteParameterAdder(
                parameters=[
                    structlog.processors.CallsiteParameter.MODULE,
                    structlog.processors.CallsiteParameter.FUNC_NAME,
                    structlog.processors.CallsiteParameter.LINENO,
                ]
            ),
            structlog.processors.dict_tracebacks,
            structlog.dev.ConsoleRenderer() if sys.stderr.isatty()
            else structlog.processors.JSONRenderer(),
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # 标准库 logging：控制台 + 按日期滚动文件
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # 控制台
    if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        root.addHandler(logging.StreamHandler(sys.stdout))

    # 文件：TimedRotatingFileHandler 午夜自动切换，保留 30 天
    log_file = LOG_DIR / "agent.log"
    file_handler = TimedRotatingFileHandler(
        filename=str(log_file),
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8",
    )
    file_handler.suffix = "%Y%m%d"  # agent.log.20260609 样式
    file_handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(file_handler)


def get_logger(name: str = None):
    """获取结构化日志记录器

    使用示例：
        logger = get_logger(__name__)
        logger.info("tool_call",
            session_id="abc123",
            tool="replace_code",
            params_size=1024,
            success=True)
    """
    _ensure_logging()
    return structlog.get_logger(name)


# 预配置的日志记录器（首次 import 时延迟初始化，避免 import 时机依赖）
logger = get_logger("game_agent")


def log_tool_call(session_id: str, tool_name: str, success: bool,
                  params_summary: str = "", error: str = None, duration_ms: float = None):
    """记录工具调用日志（标准格式）

    Args:
        session_id: 会话 ID
        tool_name: 工具名称
        success: 是否成功
        params_summary: 参数摘要（不要记录完整代码，只记录长度等）
        error: 错误信息（如果失败）
        duration_ms: 执行耗时（毫秒）
    """
    logger.info(
        "tool_call",
        session_id=session_id,
        tool=tool_name,
        success=success,
        params=params_summary,
        error=error,
        duration_ms=duration_ms,
    )


def log_error(session_id: str, error_code: str, message: str, exception: Exception = None):
    """记录错误日志（标准格式）

    Args:
        session_id: 会话 ID
        error_code: 错误码
        message: 错误消息
        exception: 异常对象（如果有）
    """
    logger.error(
        "error",
        session_id=session_id,
        error_code=error_code,
        message=message,
        exception=str(exception) if exception else None,
        exc_info=exception is not None,
    )


def log_session_event(session_id: str, event_type: str, **kwargs):
    """记录会话事件（创建、清理、超时等）

    Args:
        session_id: 会话 ID
        event_type: 事件类型（session_created, session_cleared, session_timeout 等）
        **kwargs: 额外字段
    """
    logger.info(
        "session_event",
        session_id=session_id,
        event_type=event_type,
        **kwargs
    )
