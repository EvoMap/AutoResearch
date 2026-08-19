"""补跑 pending_forge_seeds.json 里的 Idea Forge 种子。"""
import sys
import json
import subprocess
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).parent

sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "src" / "collectors"))

LOG_DIR = REPO_ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)

PENDING_FILE = REPO_ROOT / "data" / "pending_forge_seeds.json"


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_DIR / f"pending_forge_{datetime.now().strftime('%Y%m%d')}.log", "a") as f:
        f.write(line + "\n")


def publish_generated_pages(repo_root=REPO_ROOT):
    pages = [
        name for name in ("index.html", "ideas.html")
        if (repo_root / name).is_file()
    ]
    if not pages:
        log("没有生成页面，跳过 Git 提交")
        return

    def git(*argv):
        return subprocess.run(
            ["git", *argv],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
        )

    try:
        add = git("add", "--force", "--", *pages)
        if add.returncode != 0:
            log(f"git add 失败: {add.stderr.strip()[:200]}")
            return

        message = f"Pending forge: {datetime.now().strftime('%Y-%m-%d')}"
        done = git("commit", "-m", message, "--", *pages)
        if done.returncode != 0:
            if "nothing to commit" in (done.stdout + done.stderr):
                log("生成页面没有变化，不提交")
                return
            log(f"git commit 失败: {(done.stderr or done.stdout).strip()[:200]}")
            return

        push = git("push", "origin", "HEAD:gh-pages")
        if push.returncode != 0:
            log(f"git push 失败: {(push.stderr or push.stdout).strip()[:200]}")
            return
        log("Git push 完成")
    except OSError as e:
        log(f"Git 操作起不来: {e}")


def main():
    if not PENDING_FILE.exists():
        log("无 pending_forge_seeds.json，退出")
        return

    with open(PENDING_FILE) as f:
        seeds = json.load(f)

    # 兼容两种格式：直接是 list，或带壳 {"seeds": [...], "count": N}
    if isinstance(seeds, dict):
        seeds = seeds.get("seeds") or seeds.get("candidates") or seeds.get("final_candidates") or []

    if not seeds:
        log("pending_forge_seeds.json 为空，退出")
        return

    log(f"读取到 {len(seeds)} 个待补跑种子")

    from idea_forge.forge import run_idea_forge
    from idea_forge.b_library import select_b_directions, print_selection

    selected, why = select_b_directions()
    print_selection(selected, why)
    b_ids = [b["id"] for b in selected]
    try:
        result = run_idea_forge(seeds, b_ids=b_ids)
        s = result.get("summary", {})
        log(f"Forge 完成: ideas={s.get('total_ideas')} "
            f"validated={s.get('total_validated')} plans={s.get('total_plans')}")
    except Exception as e:
        log(f"Forge 失败: {e}")
        return

    # 清空 pending 文件（保留与读入时一致的壳结构）
    with open(PENDING_FILE, "w") as f:
        json.dump({"seeds": [], "count": 0}, f, ensure_ascii=False, indent=2)
    log("pending_forge_seeds.json 已清空")

    # 更新网页
    try:
        from generate_dashboard import generate_html
        generate_html()
        log("主页已更新")
    except Exception as e:
        log(f"主页更新失败: {e}")

    try:
        from generate_idea_page import generate as gen_idea
        gen_idea()
        log("Idea 页面已更新")
    except Exception as e:
        log(f"Idea 页面更新失败: {e}")

    publish_generated_pages()


if __name__ == "__main__":
    main()
