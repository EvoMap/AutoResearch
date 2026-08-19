# 用户知识库

## 1. 用法

仓库不附带真实研究语料。复制 `TEMPLATE.md`，用你有权使用和公开的内容建立方向文件：

```bash
cp knowledge_base/TEMPLATE.md knowledge_base/my_direction.md
```

如需把文件纳入公开发行，必须将文件名加入 `public_manifest.json`。本机私有知识不应提交到 Git。

## 2. 可选的 GPT Researcher 草稿

仓库不内置 GPT Researcher fork。需要联网生成知识草稿时，在单独的 Python 3.11 环境安装
`requirements-research.txt` 中固定版本的 official upstream，再通过本仓适配器写入待审目录：

```bash
python3.11 -m venv .venv-research
.venv-research/bin/python -m pip install -r requirements-research.txt
.venv-research/bin/python scripts/research_to_knowledge.py \
  "your research topic" \
  --confirm-paid-network
```

该命令可能产生网络请求和模型费用，因此必须显式确认。结果默认写入被 Git 忽略的
`workspaces/knowledge-drafts/`，不会自动写入 `knowledge_base/`。生成内容仍需人工核对来源、
许可和个人信息；确认前不要加入 `public_manifest.json`。
