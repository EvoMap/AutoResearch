"""AutoResearch 全局配置"""

from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "data"
CANDIDATES_DIR = DATA_DIR / "candidates"
VERIFIED_DIR = DATA_DIR / "verified"
LOGS_DIR = PROJECT_ROOT / "logs"

# arXiv 配置
ARXIV_CATEGORIES = ["cs.AI", "cs.LG", "cs.CL", "cs.CV", "cs.MA"]
ARXIV_MAX_RESULTS = 50

# HuggingFace Daily Papers
HF_PAPERS_API = "https://huggingface.co/api/daily_papers"

# RSS 源
RSS_FEEDS = {
    "qbitai": "https://www.qbitai.com/feed",
    "leiphone_ai": "https://www.leiphone.com/feed",
    "marktechpost": "https://www.marktechpost.com/feed/",
    "venturebeat": "https://venturebeat.com/feed/",
}

# Hacker News
HN_API_BASE = "https://hacker-news.firebaseio.com/v0"
HN_AI_KEYWORDS = [
    "LLM", "GPT", "Claude", "transformer", "diffusion",
    "multimodal", "attention", "RLHF", "agent", "reasoning",
    "fine-tuning", "RAG", "world model", "embodied",
]
HN_MIN_SCORE = 50

# Reddit (需要 OAuth，先用公开 JSON)
REDDIT_SUBREDDITS = ["MachineLearning", "LocalLLaMA", "singularity"]
REDDIT_USER_AGENT = "AutoResearch/0.1 (research aggregation)"

# GitHub Trending
GITHUB_TRENDING_URL = "https://github.com/trending"

# 信号权重（关1共识评分）
CONSENSUS_WEIGHTS = {
    "chinese_media": 2.0,
    "english_media": 1.5,
    "academic": 2.5,
    "engineering": 1.0,
}

# 共识阈值：至少在 2 类源中出现
CONSENSUS_MIN_CATEGORIES = 2
