"""
AI 一线大佬名单
被这些人提到 / 转发 / 支持 / 评论的工作，在筛选时直接加权（或跳过次级筛选）

维护原则:
- 只放真正有技术判断力的人（不是只有名气的）
- 按所属机构分组，便于识别
- 附注 Twitter/X handle 和公众识别词（中文名、英文名、昵称）
"""

INFLUENTIAL_PEOPLE = {
    "openai": [
        {"name": "Sam Altman", "handle": "sama", "zh": "山姆·奥特曼"},
        {"name": "Greg Brockman", "handle": "gdb", "zh": "Greg"},
        {"name": "Ilya Sutskever", "handle": "ilyasut", "zh": "Ilya", "status": "left, now SSI"},
        {"name": "Jakub Pachocki", "handle": "merettm"},
        {"name": "Noam Brown", "handle": "polynoamial"},
        {"name": "Lilian Weng", "handle": "lilianweng", "zh": "翁荔"},
        {"name": "John Schulman", "handle": "johnschulman2", "status": "left, now Anthropic"},
        {"name": "Sebastien Bubeck", "handle": "SebastienBubeck"},
        {"name": "Jason Wei", "handle": "_jasonwei"},
        {"name": "Hyung Won Chung", "handle": "hwchung27"},
    ],
    "anthropic": [
        {"name": "Dario Amodei", "handle": "DarioAmodei"},
        {"name": "Jared Kaplan", "handle": "jaredkaplan"},
        {"name": "Chris Olah", "handle": "ch402"},
        {"name": "Tom Brown", "handle": "tommccbrown"},
        {"name": "Jack Clark", "handle": "jackclarkSF"},
        {"name": "Jan Leike", "handle": "janleike"},
    ],
    "google_deepmind": [
        {"name": "Demis Hassabis", "handle": "demishassabis"},
        {"name": "Jeff Dean", "handle": "JeffDean"},
        {"name": "Oriol Vinyals", "handle": "OriolVinyalsML"},
        {"name": "Quoc Le", "handle": "quocleix"},
        {"name": "Jeff Dean", "handle": "JeffDean"},
        {"name": "Denny Zhou", "handle": "denny_zhou"},
        {"name": "Noam Shazeer", "handle": "NoamShazeer"},
    ],
    "meta_fair": [
        {"name": "Yann LeCun", "handle": "ylecun", "zh": "杨立昆"},
        {"name": "Soumith Chintala", "handle": "soumithchintala"},
        {"name": "Armand Joulin", "handle": "armandjoulin"},
    ],
    "xai": [
        {"name": "Elon Musk", "handle": "elonmusk", "zh": "马斯克"},
        {"name": "Greg Yang", "handle": "TheGregYang"},
        {"name": "Jimmy Ba", "handle": "jimmybaml"},
    ],
    "standalone": [
        {"name": "Andrej Karpathy", "handle": "karpathy"},
        {"name": "Yi Tay", "handle": "YiTayML"},
        {"name": "Andrew Ng", "handle": "AndrewYNg", "zh": "吴恩达"},
        {"name": "Sebastian Raschka", "handle": "rasbt"},
        {"name": "Tri Dao", "handle": "tri_dao"},
        {"name": "Percy Liang", "handle": "percyliang"},
        {"name": "Christopher Manning", "handle": "chrmanning"},
        {"name": "Jim Fan", "handle": "DrJimFan"},
        {"name": "Soumith Chintala", "handle": "soumithchintala"},
        {"name": "Simon Willison", "handle": "simonw"},
    ],
    # 国内
    "china_bigtech": [
        {"name": "梁文锋", "zh": "梁文锋", "company": "DeepSeek", "en": "Liang Wenfeng"},
        {"name": "杨植麟", "zh": "杨植麟", "company": "Moonshot/Kimi", "en": "Yang Zhilin"},
        {"name": "张小珺", "zh": "张小珺", "company": "记者/访谈"},
        {"name": "周明", "zh": "周明", "company": "澜舟科技/前MSRA"},
        {"name": "唐杰", "zh": "唐杰", "company": "清华/智谱AI"},
        {"name": "林咏华", "zh": "林咏华", "company": "智源"},
        {"name": "黄铁军", "zh": "黄铁军", "company": "智源/北大"},
        {"name": "贾扬清", "zh": "贾扬清", "company": "前阿里/PyTorch核心"},
        {"name": "翁家翌", "zh": "翁家翌", "company": "OpenAI"},
    ],
    "china_academia": [
        {"name": "朱军", "zh": "朱军", "company": "清华"},
        {"name": "周志华", "zh": "周志华", "company": "南大"},
        {"name": "马毅", "zh": "马毅", "company": "港大/伯克利"},
        {"name": "李飞飞", "zh": "李飞飞", "company": "斯坦福", "en": "Fei-Fei Li"},
        {"name": "何恺明", "zh": "何恺明", "company": "MIT/FAIR", "en": "Kaiming He"},
        {"name": "孙剑", "zh": "孙剑", "company": "已故/旷视"},
    ],
}


def get_all_identifiers():
    """获取所有大佬的可识别字符串（中英文名、handle），用于文本匹配"""
    identifiers = []
    for group, people in INFLUENTIAL_PEOPLE.items():
        for p in people:
            names = [p["name"]]
            if p.get("zh"):
                names.append(p["zh"])
            if p.get("en"):
                names.append(p["en"])
            if p.get("handle"):
                names.append(f"@{p['handle']}")
                names.append(p["handle"])
            for n in names:
                if n and len(n) > 2:
                    identifiers.append({
                        "text": n,
                        "person": p["name"],
                        "group": group,
                    })
    return identifiers


def check_mentions(text):
    """检查文本中是否提及大佬，返回匹配列表"""
    if not text:
        return []
    text_lower = text.lower()
    matches = []
    for item in get_all_identifiers():
        t = item["text"].lower()
        if t in text_lower:
            matches.append(item)
    # 去重
    seen = set()
    unique = []
    for m in matches:
        key = m["person"]
        if key not in seen:
            seen.add(key)
            unique.append(m)
    return unique


def get_boost_score(text):
    """
    根据文本中提及的大佬数量，计算加权分数
    0 个大佬 = 1.0 (无加权)
    1 个大佬 = 1.5
    2+ 个大佬 = 2.0 (多个大佬背书)
    """
    mentions = check_mentions(text)
    if not mentions:
        return 1.0, []
    if len(mentions) >= 2:
        return 2.0, mentions
    return 1.5, mentions


if __name__ == "__main__":
    # 测试
    test_texts = [
        "Sam Altman just tweeted about this new paper",
        "Yann LeCun 对这篇论文的评价是...",
        "马斯克点赞了这个工作",
        "OpenAI翁家翌：梯度之外，下一个AI训练范式有着落了？",
        "某篇随便的论文",
    ]
    for t in test_texts:
        score, mentions = get_boost_score(t)
        print(f"'{t}'")
        print(f"  → 加权: {score}, 提及: {[m['person'] for m in mentions]}")
