# ProbableBook

## 1. 项目简介
ProbableBook 是一个 Python 脚本工具，用于抓取 Probable Markets 各预测市场的实时盘口，计算 Yes/No 卖一价格之和（Sum），并找出 Sum 最小（最接近 1）的市场，同时给出对应的美元挂单量（notional USD）。

## 2. 核心功能
*   **自动发现市场**：自动从官方 API 拉取所有未关闭（`closed=false`）的市场事件，无需手动维护列表。
*   **实时盘口抓取**：抓取每个市场的 Yes / No order book。
*   **数据聚合与计算**：
    *   提取 Yes / No 卖一价格（Best Ask Price）。
    *   计算 Sum = Yes Best Ask + No Best Ask。
    *   计算卖一对应的美元名义量（Notional USD = Best Price × Aggregated Size at that Price）。
*   **Sum 状态标记**：自动标记 Sum 为 `LT1` (小于1)、`EQ1` (等于1)、`GT1` (大于1) 或 `NA` (数据缺失)。
*   **Best Opportunity**：自动筛选并输出 Sum 最小的市场机会。
*   **Telegram 报警**：支持定时监控并在 Sum 满足阈值时发送 Telegram 通知。

## 3. 安装与运行

### 环境要求

**⚠️ 强制要求：必须使用 Python 虚拟环境 (venv) 运行本项目。**
为确保依赖一致性，请勿使用系统 Python 或 Conda base 环境直接运行。

*   Python 3.8+
*   网络环境需能访问 `probable.markets` 相关 API

### 安装步骤

1.  **创建并激活虚拟环境**
    ```bash
    # 1. 创建 venv
    python3 -m venv venv

    # 2. 激活 venv (必须执行)
    # macOS/Linux:
    source venv/bin/activate
    # Windows:
    # venv\Scripts\activate
    ```

2.  **安装依赖**
    ```bash
    # 确保 pip 是最新的
    pip install --upgrade pip
    
    # 安装项目依赖
    pip install -r requirements.txt
    ```

3.  **配置 Telegram（可选）**
    复制示例配置文件并填入您的 Bot Token 和 Chat ID：
    ```bash
    cp .env.example .env
    # 编辑 .env 文件
    # TG_BOT_TOKEN=123456:ABCDEF...
    # TG_CHAT_ID=123456789
    ```

### 运行示例

**单次运行并输出美化表格（最常用）：**
```bash
python probable_orderbook.py --all --once --pretty
```

**启动监控（每 60 秒扫描，Sum <= 1.0 时报警）：**
推荐使用 `.env` 配置 TG 信息，直接运行：
```bash
python probable_orderbook.py --all --interval 60 --alert-sum-threshold 1.0 --pretty
```

**临时覆盖 TG 配置（调试用）：**
```bash
python probable_orderbook.py --all --once --pretty --alert-sum-threshold 1.0 \
  --tg-token "YOUR_TOKEN" --tg-chat-id "YOUR_ID"
```

**调试模式（限制抓取前 5 个市场）：**
```bash
python probable_orderbook.py --all --once --pretty --max-events 5
```

**持续运行并输出到 JSONL 文件：**
```bash
python probable_orderbook.py --all --interval 60 --out summary.jsonl
```

## 4. Telegram Alert Behavior (报警行为)

本工具采用**极简条件触发**逻辑，不包含任何去重、限流或状态记忆机制。

**核心规则：**
> 每一轮扫描（interval）都会独立判断：只要满足 `Best Market Sum < Threshold`，就发送报警。

### 行为示例
假设配置 `--interval 60 --alert-sum-threshold 1.05`：

1.  **09:00 (Sum 1.01)**: 满足条件 (< 1.05) → **发送 TG**
2.  **09:01 (Sum 1.02)**: 满足条件 (< 1.05) → **发送 TG** (即使只变了 0.01)
3.  **09:02 (Sum 1.01)**: 满足条件 (< 1.05) → **发送 TG** (即使和 09:00 一样)
4.  **09:03 (Sum 1.06)**: 不满足条件 (>= 1.05) → **不发送**
5.  **09:04 (Sum 1.04)**: 满足条件 (< 1.05) → **发送 TG**

### 典型使用场景

**场景 A：持续监控高价值机会**
每 30 秒扫描一次，只要 Sum < 1.0 就疯狂报警，直到机会消失。
```bash
python probable_orderbook.py --interval 30 --alert-sum-threshold 1.0 --pretty
```

**场景 B：每 5 分钟汇报一次**
如果您不想被频繁打扰，请直接调大 `--interval` 参数（例如 300 秒）。
```bash
python probable_orderbook.py --interval 300 --alert-sum-threshold 1.05 --pretty
```

## 5. 快速自测指南

**1. 验证 Telegram 连通性**
```bash
# 需先配置 .env 或环境变量
python probable_orderbook.py --test-telegram
```
如果成功，你会收到一条 "ProbableBook Telegram test message"。

**2. 验证报警逻辑**
使用极短间隔（10秒）来观察行为：
```bash
python probable_orderbook.py --all --interval 10 --alert-sum-threshold 1.1 --pretty
```
观察控制台日志，您应该看到每 10 秒都会尝试发送报警（如果找到的市场 Sum < 1.1），日志会显示：
`INFO: Alert sent (sum 1.0020 < 1.1)`

## 6. 命令行参数说明

| 参数 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `--all` | `True` | **自动发现**：自动从官方 API 拉取所有未关闭（closed=false）的市场事件。 |
| `--once` | `False` | **单次运行**：只执行一轮抓取与计算后立即退出。 |
| `--pretty` | `False` | **美化输出**：以人类可读的表格形式输出结果到终端。 |
| `--max-events N` | `None` | **数量限制**：限制最多处理的 event 数量（调试用）。 |
| `--interval N` | `60` | **轮询间隔**：循环运行模式下的等待间隔（秒）。 |
| `--out FILE` | `None` | **文件输出**：指定输出 JSONL 文件的路径。 |
| `--alert-sum-threshold` | `None` | **报警阈值**：当 Best Market Sum < 该值时触发 Telegram 通知。 |
| `--tg-token` | `None` | **TG Token**：覆盖 `.env` 中的 `TG_BOT_TOKEN`。 |
| `--tg-chat-id` | `None` | **TG Chat ID**：覆盖 `.env` 中的 `TG_CHAT_ID`。 |


## 5. 输出说明

### Best Opportunity 区块 (Pretty Mode)
脚本仅输出一个 "Best Opportunity" 区块，展示当前扫描到的 **Sum 最小** 的市场。

*   **筛选逻辑**：在所有 Sum 有效的市场中，选择 Sum 值最小的那个。
*   **Executable USD**：可执行美元规模。
    *   定义：`min(Yes Notional USD, No Notional USD)`
    *   含义：在当前卖一盘口下，能够同时买入 Yes 和 No 的最大可执行美元规模。
*   **Notional USD**：表示吃掉单边卖一档位所需的美元名义金额。
    *   计算公式：`Best Ask Price × Aggregated Size (at that price)`

## 6. 注意事项
*   **价格聚合**：Order book 的 asks 是按 price 进行聚合计算的，确保 notional USD 反映的是该价格档位的真实深度。
*   **名义金额**：Notional USD 仅为名义金额，实际交易可能包含手续费、滑点或其他成本。
*   **数据时效**：输出结果为 API 抓取时刻的快照，预测市场波动剧烈，请以实时数据为准。
