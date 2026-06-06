"""企业级结构化日志配置"""

import sys
import logging
from pathlib import Path
from datetime import datetime
import structlog

# 日志目录
LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)

# 日志文件路径（按日期滚动）
LOG_FILE = LOG_DIR / f"agent_{datetime.now().strftime('%Y%m%d')}.log"


def setup_logging(level: str = "INFO"):
    """配置企业级结构化日志系统
    
    特性：
    - 结构化日志（JSON 格式）
    - 同时输出到控制台和文件
    - 按日期自动滚动
    - 包含时间戳、级别、模块、函数、行号
    - 支持额外的上下文字段（session_id, tool_name 等）
    """
    
    # 配置 structlog 处理器
    structlog.configure(
        processors=[
            # 添加日志级别
            structlog.stdlib.add_log_level,
            # 添加时间戳
            structlog.processors.TimeStamper(fmt="iso"),
            # 添加调用栈信息（模块、函数、行号）
            structlog.processors.CallsiteParameterAdder(
                parameters=[
                    structlog.processors.CallsiteParameter.MODULE,
                    structlog.processors.CallsiteParameter.FUNC_NAME,
                    structlog.processors.CallsiteParameter.LINENO,
                ]
            ),
            # 渲染成 JSON（文件）或彩色输出（控制台）
            structlog.processors.dict_tracebacks,
            structlog.dev.ConsoleRenderer() if sys.stderr.isatty() 
            else structlog.processors.JSONRenderer(),
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    
    # 配置标准库 logging（structlog 底层使用）
    logging.basicConfig(
        format="%(message)s",
        level=getattr(logging, level.upper()),
        handlers=[
            # 控制台输出
            logging.StreamHandler(sys.stdout),
            # 文件输出
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
        ],
    )


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
    return structlog.get_logger(name)


# 预配置的日志记录器
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


# 初始化日志系统（模块加载时自动配置）
setup_logging()
