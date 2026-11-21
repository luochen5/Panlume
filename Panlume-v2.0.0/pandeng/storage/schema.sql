CREATE TABLE IF NOT EXISTS app_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

INSERT INTO app_metadata(key, value, updated_at)
SELECT 'schema_version', '6', CURRENT_TIMESTAMP
WHERE NOT EXISTS (
    SELECT 1 FROM app_metadata WHERE key = 'schema_version'
);

CREATE TABLE IF NOT EXISTS position_lots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_code TEXT NOT NULL CHECK (length(stock_code) = 6),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    buy_date TEXT NOT NULL,
    stop_loss_price REAL CHECK (stop_loss_price IS NULL OR stop_loss_price > 0),
    confirmed_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    deactivated_at TEXT,
    deactivation_reason TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_position_lots_code_date
ON position_lots(stock_code, buy_date);

CREATE TABLE IF NOT EXISTS run_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    level TEXT NOT NULL,
    event TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_run_logs_created_at
ON run_logs(created_at);

CREATE TABLE IF NOT EXISTS ingestion_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    data_kind TEXT NOT NULL,
    adapter TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ok', 'unknown')),
    received_count INTEGER NOT NULL DEFAULT 0,
    accepted_count INTEGER NOT NULL DEFAULT 0,
    duplicate_count INTEGER NOT NULL DEFAULT 0,
    rejected_count INTEGER NOT NULL DEFAULT 0,
    replay_clock TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ingestion_runs_kind_id
ON ingestion_runs(data_kind, id);

CREATE TABLE IF NOT EXISTS data_quality_issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES ingestion_runs(id),
    record_index INTEGER,
    data_kind TEXT NOT NULL,
    stock_code TEXT,
    data_time TEXT,
    issue_type TEXT NOT NULL,
    severity TEXT NOT NULL CHECK (severity IN ('info', 'error')),
    message TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_quality_issues_run
ON data_quality_issues(run_id, severity);

CREATE TABLE IF NOT EXISTS minute_bars (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_code TEXT NOT NULL,
    bar_time TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume INTEGER NOT NULL,
    source TEXT NOT NULL,
    adjustment TEXT NOT NULL CHECK (adjustment = 'none'),
    confirmed INTEGER NOT NULL CHECK (confirmed IN (0, 1)),
    ingestion_run_id INTEGER NOT NULL REFERENCES ingestion_runs(id),
    raw_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(stock_code, bar_time)
);

CREATE INDEX IF NOT EXISTS idx_minute_bars_code_time
ON minute_bars(stock_code, bar_time);

CREATE TABLE IF NOT EXISTS daily_qfq_bars (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_code TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume INTEGER NOT NULL,
    source TEXT NOT NULL,
    adjustment TEXT NOT NULL CHECK (adjustment = 'qfq'),
    confirmed INTEGER NOT NULL CHECK (confirmed IN (0, 1)),
    ingestion_run_id INTEGER NOT NULL REFERENCES ingestion_runs(id),
    raw_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(stock_code, trade_date)
);

CREATE INDEX IF NOT EXISTS idx_daily_qfq_bars_code_date
ON daily_qfq_bars(stock_code, trade_date);

CREATE TABLE IF NOT EXISTS trading_calendar (
    trade_date TEXT PRIMARY KEY,
    is_open INTEGER NOT NULL CHECK (is_open IN (0, 1)),
    source TEXT NOT NULL,
    ingestion_run_id INTEGER NOT NULL REFERENCES ingestion_runs(id),
    raw_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS processing_checkpoints (
    stream_key TEXT PRIMARY KEY,
    last_data_time TEXT,
    last_run_id INTEGER REFERENCES ingestion_runs(id),
    status TEXT NOT NULL CHECK (status IN ('ok', 'unknown')),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS monitor_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    session_date TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('idle', 'running', 'paused', 'stopped', 'closed', 'error')
    ),
    calendar_status TEXT NOT NULL CHECK (calendar_status IN ('unknown', 'ok', 'closed')),
    daily_status TEXT NOT NULL CHECK (daily_status IN ('not_ready', 'ok', 'unknown')),
    last_round_at TEXT,
    last_successful_round_at TEXT,
    next_poll_at TEXT,
    stop_reason TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS monitor_code_states (
    session_date TEXT NOT NULL,
    stock_code TEXT NOT NULL,
    last_contiguous_time TEXT,
    last_attempt_time TEXT,
    status TEXT NOT NULL CHECK (status IN ('ok', 'unknown')),
    unknown_reason TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (session_date, stock_code)
);

CREATE INDEX IF NOT EXISTS idx_monitor_code_status
ON monitor_code_states(session_date, status);

CREATE TABLE IF NOT EXISTS monitor_rounds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_date TEXT NOT NULL,
    trigger TEXT NOT NULL,
    recovery INTEGER NOT NULL CHECK (recovery IN (0, 1)),
    scheduled_at TEXT NOT NULL,
    target_time TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('ok', 'unknown', 'skipped', 'interrupted')),
    analysis_ready_count INTEGER NOT NULL DEFAULT 0,
    received_count INTEGER NOT NULL DEFAULT 0,
    accepted_count INTEGER NOT NULL DEFAULT 0,
    duplicate_count INTEGER NOT NULL DEFAULT 0,
    rejected_count INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_monitor_rounds_session_id
ON monitor_rounds(session_date, id);

CREATE TABLE IF NOT EXISTS monitor_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id INTEGER REFERENCES monitor_rounds(id),
    session_date TEXT NOT NULL,
    stock_code TEXT,
    event_type TEXT NOT NULL,
    data_time TEXT,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_monitor_events_session_id
ON monitor_events(session_date, id);

CREATE TABLE IF NOT EXISTS position_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_id INTEGER REFERENCES position_lots(id),
    action TEXT NOT NULL CHECK (action IN ('add', 'archive')),
    before_json TEXT,
    after_json TEXT,
    confirmation_text TEXT NOT NULL,
    confirmed_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS analysis_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_date TEXT NOT NULL,
    stock_code TEXT NOT NULL,
    target_time TEXT NOT NULL,
    analysis_mode TEXT NOT NULL CHECK (analysis_mode IN ('live', 'offline_recovery', 'replay')),
    monitor_round_id INTEGER REFERENCES monitor_rounds(id),
    status TEXT NOT NULL CHECK (status IN ('ok', 'unknown')),
    input_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(session_date, stock_code, analysis_mode, target_time)
);

CREATE TABLE IF NOT EXISTS indicator_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    analysis_run_id INTEGER NOT NULL REFERENCES analysis_runs(id),
    session_date TEXT NOT NULL,
    stock_code TEXT NOT NULL,
    data_time TEXT NOT NULL,
    latest_minute_bar_id INTEGER REFERENCES minute_bars(id),
    status TEXT NOT NULL CHECK (status IN ('ok', 'unknown')),
    unknown_reasons_json TEXT NOT NULL,
    values_json TEXT NOT NULL,
    source_bar_ids_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_indicator_snapshots_code_time
ON indicator_snapshots(stock_code, data_time);

CREATE TABLE IF NOT EXISTS rule_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL,
    event_type TEXT NOT NULL,
    stock_code TEXT NOT NULL,
    session_date TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'cleared')),
    priority TEXT NOT NULL CHECK (priority IN ('normal', 'high')),
    position_lot_id INTEGER REFERENCES position_lots(id),
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    last_reminded_at TEXT,
    check_count INTEGER NOT NULL DEFAULT 0,
    reminder_count INTEGER NOT NULL DEFAULT 0,
    latest_message TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rule_events_state
ON rule_events(fingerprint, status);

CREATE UNIQUE INDEX IF NOT EXISTS idx_rule_events_one_active
ON rule_events(fingerprint) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS rule_event_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL REFERENCES rule_events(id),
    snapshot_id INTEGER NOT NULL REFERENCES indicator_snapshots(id),
    analysis_run_id INTEGER NOT NULL REFERENCES analysis_runs(id),
    data_time TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    is_active INTEGER CHECK (is_active IS NULL OR is_active IN (0, 1)),
    notified INTEGER NOT NULL CHECK (notified IN (0, 1)),
    notification_kind TEXT CHECK (
        notification_kind IS NULL OR notification_kind IN ('initial', 'repeat', 'recovery')
    ),
    payload_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rule_checks_event_id
ON rule_event_checks(event_id, id);

CREATE TABLE IF NOT EXISTS daily_analysis (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_code TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    daily_bar_id INTEGER NOT NULL REFERENCES daily_qfq_bars(id),
    status TEXT NOT NULL CHECK (status IN ('ok', 'unknown')),
    ma20 REAL,
    previous_ma20 REAL,
    turn_days INTEGER,
    close REAL,
    formal_signal INTEGER CHECK (formal_signal IS NULL OR formal_signal IN (0, 1)),
    analysis_mode TEXT NOT NULL CHECK (analysis_mode IN ('live', 'offline_recovery', 'replay')),
    is_backfill INTEGER NOT NULL CHECK (is_backfill IN (0, 1)),
    unknown_reason TEXT,
    source_daily_bar_ids_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(stock_code, trade_date)
);

CREATE INDEX IF NOT EXISTS idx_daily_analysis_code_date
ON daily_analysis(stock_code, trade_date);


CREATE TABLE IF NOT EXISTS summary_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_check_id INTEGER NOT NULL UNIQUE REFERENCES rule_event_checks(id),
    status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'succeeded', 'fallback')),
    input_json TEXT NOT NULL,
    prompt_text TEXT NOT NULL,
    template_text TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    recovery_count INTEGER NOT NULL DEFAULT 0,
    fallback_reason TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_summary_tasks_status_id
ON summary_tasks(status, id);

CREATE TABLE IF NOT EXISTS summary_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL UNIQUE REFERENCES summary_tasks(id),
    event_check_id INTEGER NOT NULL UNIQUE REFERENCES rule_event_checks(id),
    summary_kind TEXT NOT NULL CHECK (summary_kind IN ('model', 'template')),
    text TEXT NOT NULL,
    validation_reasons_json TEXT NOT NULL,
    model_name TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_summary_results_task
ON summary_results(task_id);

CREATE TABLE IF NOT EXISTS model_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES summary_tasks(id),
    provider TEXT NOT NULL CHECK (provider = 'ollama'),
    model_name TEXT NOT NULL,
    status TEXT NOT NULL,
    request_json TEXT NOT NULL,
    response_text TEXT,
    thinking_text TEXT,
    error_type TEXT,
    error_message TEXT,
    elapsed_ms INTEGER NOT NULL,
    total_duration_ns INTEGER,
    load_duration_ns INTEGER,
    eval_count INTEGER,
    eval_duration_ns INTEGER,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_model_calls_task_id
ON model_calls(task_id, id);

CREATE TABLE IF NOT EXISTS review_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_date TEXT NOT NULL,
    stock_code TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    input_json TEXT NOT NULL,
    local_report TEXT NOT NULL,
    data_status TEXT NOT NULL CHECK (data_status IN ('ok', 'unknown')),
    created_at TEXT NOT NULL,
    UNIQUE(session_date, stock_code, input_hash)
);

CREATE INDEX IF NOT EXISTS idx_review_runs_date_code
ON review_runs(session_date, stock_code, id);

CREATE TABLE IF NOT EXISTS cloud_review_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id INTEGER NOT NULL UNIQUE REFERENCES review_runs(id),
    provider TEXT NOT NULL CHECK (provider = 'openai_compatible'),
    model_name TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
    request_json TEXT NOT NULL,
    response_text TEXT,
    result_text TEXT,
    validation_reasons_json TEXT NOT NULL DEFAULT '[]',
    error_type TEXT,
    error_message TEXT,
    elapsed_ms INTEGER NOT NULL DEFAULT 0,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    total_tokens INTEGER,
    started_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_cloud_review_calls_status
ON cloud_review_calls(status, id);
