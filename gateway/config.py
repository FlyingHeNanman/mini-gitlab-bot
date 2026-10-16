"""环境变量配置。"""
import os
from dataclasses import dataclass, field


@dataclass
class Settings:
    # 网关自身
    webhook_secret: str = field(default_factory=lambda: os.getenv("WEBHOOK_SECRET", "change-me"))
    agent_concurrency: int = field(default_factory=lambda: int(os.getenv("AGENT_CONCURRENCY", "2")))
    work_dir: str = field(default_factory=lambda: os.getenv("WORK_DIR", "/work"))
    db_path: str = field(default_factory=lambda: os.getenv("DB_PATH", "/data/tasks.db"))
    port: int = field(default_factory=lambda: int(os.getenv("PORT", "8080")))

    # GitLab
    gitlab_url: str = field(default_factory=lambda: os.getenv("GITLAB_URL", "https://gitlab.example.com"))
    gitlab_token: str = field(default_factory=lambda: os.getenv("GITLAB_TOKEN", ""))
    target_branch: str = field(default_factory=lambda: os.getenv("TARGET_BRANCH", "main"))
    ai_fix_label: str = field(default_factory=lambda: os.getenv("AI_FIX_LABEL", "ai-fix"))
    repo_whitelist: list = field(default_factory=lambda: [p for p in os.getenv("REPO_WHITELIST", "").split(",") if p])

    # Codex / LLM
    model_provider: str = field(default_factory=lambda: os.getenv("MODEL_PROVIDER", "internal"))
    model_name: str = field(default_factory=lambda: os.getenv("MODEL_NAME", ""))
    agent_timeout_sec: int = field(default_factory=lambda: int(os.getenv("AGENT_TIMEOUT_SEC", "1800")))
    max_iterations: int = field(default_factory=lambda: int(os.getenv("MAX_ITERATIONS", "3")))


settings = Settings()
