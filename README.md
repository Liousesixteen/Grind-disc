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
node tests/test_board_health.js
.venv/bin/python scripts/check_refs.py
```

## 版本管理

- 初始 `main` 保存 2026-10-27 看板优化前的源码与文档基线。
- 看板优化在独立分支完成；用户审核通过后才创建 PR。
- 历史材料仅供溯源，不自动成为现行需求。

## 量化研究层（2026-10-28）

独立于看板的研究基础设施：真实涨跌停价、每日指标、指数日线与事件研究。数据写入 `cache/product/history.sqlite` 的独立分区表，按交易日发布，缺日/截断即拒绝。

```bash
# 按日回补全市场涨跌停、每日指标、8 个指数（幂等）
.venv/bin/python scripts/research_data.py --target 2026-10-27 --days 121 --verify

# 全市场事件研究：T+1 开盘入场、真实涨跌停不可成交、日期聚类标准误、市值中性
.venv/bin/python scripts/event_study.py --target 2026-10-27 --days 121 \
  --signals signal_a,b_confirmed --horizons 1,5,10 \
  --out reports/2026-10-28/quant/event-study.json
```

排序已改为 `readiness → setup_state → risk_adj → …`（`risk_adj` = 20日相对收益 ÷ 20日已实现波动）；回测新增单笔风险预算 0.5% NAV、相关性组暴露 20%、单日新开 3 只三道闸门。口径、验收与回滚见 [量化优化（二）：排序与组合约束](docs/量化优化_排序与组合约束_2026-10-28.md)。

两年样本的首批结论与局限见 [量化优化（一）：全市场事件研究](docs/量化优化_事件研究_2026-10-28.md)：`signal_a` 与 `b_confirmed` 均无正净超额，`signal_a` 的拖累来自突破/放量代理；121 天窗口曾出现的 `momentum_turn` 正超额未复现。事件研究是条件统计，不是已验证策略。

## 看板审核版（6063）

6060原版模板保存在 `templates/intraday_board_legacy.html`。新版沿用其视觉方式，使用独立研究服务：

```bash
.venv/bin/python scripts/product_service.py --port 6063
```

6063直接采集行情，不依赖6060的HTTP服务；不重复运行旧服务的数据维护、股池任务或旧曝光日志。自选/核验/观察预案/研究快照写独立数据库，旧观察日志只幂等导入。

策略注册位于 `config/strategy_catalog.json`；全部策略处于实验状态，未知条件不通过，不产生下单指令。来源、数据覆盖、实验工具和限制详见 [策略研究框架与验证协议](docs/策略研究框架与验证协议_2026-10-27.md)。

审核版模板为 `templates/intraday_board_v2.html`、`templates/product-v2.js`、`templates/product-v2.css`。日线增量、ETF复权、关注标的财务事实由独立维护线程验收发布，交易日历控制盘前/盘中/盘后口径。旧 `preview_board.py` 与 v1 模板保留作溯源。

macOS用户级服务（仅6063，本机127.0.0.1）支持登录启动、进程异常恢复和日志轮转：

```bash
.venv/bin/python scripts/research_service_ctl.py install
.venv/bin/python scripts/research_service_ctl.py status
.venv/bin/python scripts/research_service_ctl.py restart
.venv/bin/python scripts/research_service_ctl.py stop
.venv/bin/python scripts/research_service_ctl.py uninstall
```

安装需要当前用户LaunchAgents目录写权限；不会管理6060。macOS注销或机器休眠期间不承诺采集，唤醒后重新核验源时刻。Linux可使用 `deploy/grind-research.service` 并按部署用户修改路径。

新增预案按证券×策略×风格独立保存，修订不可覆盖；旧预案迁移后暂停，补齐依据再启用。策略实验支持日线假设登记、固定名单技术成交模型、冻结输入复算和前瞻观察，四者均不等同于已验证交易策略。日频模型覆盖下一交易日开盘、T+1、最小交易单位、涨跌停、费用、公司行为和无法成交记录；缺关键数据时不发布绩效。实验结果与失败记录保留，源码/行情输入按哈希冻结。原始数据修订按内容归档，不用新数据冒充旧回测。

```bash
node tests/test_product_v2.js
```

本轮完整验收说明见 [6063全面优化实施与验收](docs/6063全面优化实施与验收_2026-10-28.md)。
