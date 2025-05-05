from bot_config import (
    CROSS_SUB_BAN_REASON,
    EXEMPT_USERS,
    DAILY_BAN_LIMIT,
    MAX_LOG_AGE_MINUTES,
    ROW_RETENTION_DAYS,
    TRUSTED_SUBS,
    TRUSTED_SOURCES,
    setup_google_sheet,
    setup_reddit
)

sheet, client = setup_google_sheet()
reddit = setup_reddit()
