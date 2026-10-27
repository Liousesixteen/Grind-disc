# Grind-disc · AI 顶级交易员

本地运行的 A 股研究与盘中观察工具。现行规则及历史材料的使用边界见 `AGENTS.md`，交易分析框架见 `熔炼系统.md`。读数和经验评分不能替代交易判断。

## 本地启动

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
export TUSHARE_TOKEN='<在本机配置，不提交到 Git>'
.venv/bin/python scripts/intraday_board.py --port 6060 --interval 15
```

看板依赖已生成的 `cache/pool_*` 股池、相关画像缓存和本地数据库。缓存、数据库、API 凭据、会话 cookie、生成报告及虚拟环境均不随源码上传。首次数据准备流程与数据限制见 `DATA_SOURCE_POLICY.md`、`docs/环境与依赖.md` 和各数据脚本的命令行帮助；直接克隆源码不会带入现成数据。

## 验证

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/check_refs.py
```

## 版本管理

- 初始 `main` 保存 2026-10-27 看板优化前的源码与文档基线。
- 看板优化在独立分支完成；用户审核通过后才创建 PR。
- 历史材料仅供溯源，不自动成为现行需求。
