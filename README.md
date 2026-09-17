# 知识星球股票观点关键词分析工具

用于分析你有权访问的知识星球主题与股票后续表现。同步使用知识星球官方 `zsxq-cli` 的 OAuth 只读接口，不依赖浏览器 Cookie，不绕过登录、验证码或平台访问控制；行情支持 CSV 和 AKShare。

## 快速启动

```bash
cp .env.example .env
docker compose up --build
```

打开 http://localhost:8000/docs。

## 知识星球直连同步

`zsxq-cli` 需要安装在 Mac 主机上（不要装进 Docker），凭据由官方 CLI 保存到系统 Keychain：

```bash
npm install -g zsxq-cli
zsxq-cli auth login
zsxq-cli auth status
```

首次默认同步“星辰财经”圈子的最新主题 5 页（最多 100 篇）：

```bash
cd /Users/zhangyun/Documents/code/planet-stock-analyzer
python3 scripts/sync_zsxq.py
```

只读取并检查数量，不写入应用：

```bash
python3 scripts/sync_zsxq.py --dry-run
```

### 按月回填今年历史主题（支持断点续拉）

下面的命令会从当前月向前按月读取 2026 年主题；每成功同步一页，就把下一页游标写入 `data/zsxq-backfill-2026.json`。网络中断、关闭终端或达到 100 页上限后，直接重复执行**同一条命令**即可续拉，不会重复入库。

```bash
python3 scripts/sync_zsxq.py --backfill-year 2026 --pages 100 --count 30
```

终端最终会显示 `next_month`：有值表示尚未完成，重复执行即可；为 `null` 表示该年回填已完成。断点文件不包含主题正文、Cookie 或 Token，且已被 Git 忽略。需要从头重新回填时，停止同步后删除该断点文件：

```bash
rm data/zsxq-backfill-2026.json
```

只想验证某一个月的内容和耗时，不进入更早月份：

```bash
/usr/bin/time -p python3 scripts/sync_zsxq.py --backfill-month 2026-09 --pages 100 --count 30
```

这个命令的断点文件是 `data/zsxq-backfill-2026-09.json`；命令结束后显示的 `real` 就是本次实际耗时。若 `next_month` 为 `null`，说明 9 月同步完成。

常用参数：`--group-id`、`--pages 5`、`--count 20`（单页最多 30）、`--end-time '2026-09-16T10:52:34.729+0800'`、`--app-url http://127.0.0.1:8000`。

脚本使用官方 CLI 支持的 `group +topics` 命令读取并分页；当前 CLI 没有提供“仅精华”筛选参数，因此 v1 同步主题流，再在本地分析和筛选。不要再使用浏览器接口路径 `/v2/groups/<圈子ID>/topics`。

同步脚本在主机上调用官方 CLI，再把脱敏后的主题字段 POST 到本机 `/api/topics/sync/zsxq`；容器内不会接触 Keychain 或 Token。

## 一个月涨幅关键词统计

“未来 1 月”按主题事件日后的 20 个交易日计算：期间最高收盘价相对事件日收盘价涨幅达到 10% 即标记为成功。只有完整取得 20 个交易日行情的主题会进入该排行；它是历史相关性统计，不是投资建议。

在历史主题同步完成后，下载这些主题实际提到股票的前复权行情。脚本每批完成都会保存断点，重复同一命令会继续，不会重新下载已完成股票：

```bash
python3 scripts/sync_quotes.py --start-date 2026-01-01 --end-date 2026-09-16 --batch-size 10 --batches 20
```

最终输出的 `remaining` 为 0 后，重算收益：

```bash
curl -X POST http://localhost:8000/api/analyze/rebuild
```

刷新首页的“关键词统计”，即可按“未来 1 月涨幅≥10%比例”查看排行。

## 日常操作命令

以下命令都在项目根目录执行：

```bash
cd /Users/zhangyun/Documents/code/planet-stock-analyzer
```

### 启动项目（后台运行）

```bash
docker compose up -d
```

首次启动或修改了 `Dockerfile`、`requirements.txt`、Compose 配置时，使用：

```bash
docker compose up -d --build
```

### 查看运行状态

```bash
docker compose ps
```

正常情况下，`postgres` 应显示 `healthy`，`app` 应显示 `running`。

### 查看 App 日志

```bash
docker compose logs -f app
```

按 `Ctrl + C` 只退出日志查看，不会停止后台容器。

### 查看 PostgreSQL 日志

```bash
docker compose logs -f postgres
```

### 停止并删除容器

```bash
docker compose down
```

该命令不会删除 PostgreSQL 数据卷。

### 重启项目

```bash
docker compose restart
```

### 修改代码后重新启动 App

当前代码会在构建镜像时复制到容器中。修改 Python 代码后，需要重新构建 App 镜像：

```bash
docker compose up -d --build app
```

查看新的启动日志：

```bash
docker compose logs -f app
```

如果只需要重启、没有修改代码：

```bash
docker compose restart app
```

修改 `docker-compose.yml`、数据库配置或依赖后，重新构建全部服务：

```bash
docker compose up -d --build
```

### 进入 PostgreSQL 命令行

```bash
docker compose exec postgres psql -U planet -d planet_stock
```

### 重要提醒

不要随意使用下面的命令：

```bash
docker compose down -v
```

`-v` 会删除 PostgreSQL 数据卷，可能导致数据库数据丢失。

### 导入主题 JSON

```json
[
  {
    "topic_id": "topic-1",
    "title": "某公司订单增长",
    "content": "公司订单增长，产能扩张，关注 600519 贵州茅台",
    "author": "作者",
    "published_at": "2026-01-05T08:30:00+08:00",
    "source_url": "https://wx.zsxq.com/"
  }
]
```

```bash
curl -X POST http://localhost:8000/api/topics/import \
  -H 'content-type: application/json' --data @topics.json
```

人工修正某篇主题的股票/关键词关联（会替换自动识别结果）：

```bash
curl -X PUT http://localhost:8000/api/topics/14425422115255122/annotations \
  -H 'content-type: application/json' \
  -d '{"stocks":[{"code":"300782","name":"卓胜微","confidence":1}],"keywords":["涨价","需求"]}'
```

## 环境变量

- `DATABASE_URL`：PostgreSQL 连接串
- `KNOWLEDGE_PLANET_TOKEN`：兼容旧配置，官方 CLI 流程不读取此变量；不要把 Token 写入代码或日志
- `MARKET_DATA_PROVIDER`：`akshare` 或 `csv`
- `MARKET_DATA_CSV`：CSV 行情文件路径（`code,date,open,high,low,close,volume,amount,turnover_rate`）

Docker 镜像已包含 AKShare；如果直接在主机运行 Python，再安装可选依赖：

```bash
python3 -m pip install -r requirements-market.txt
curl -X POST http://localhost:8000/api/quotes/sync \
  -H 'content-type: application/json' \
  -d '{"stock_codes":["600519"],"start_date":"2026-01-01","end_date":"2026-09-16","adjust_type":"qfq"}'
```

## 说明

默认上涨标签为：主题事件日之后未来 5 个交易日最高收盘收益率 >= 10%。样本少于 10 篇的关键词会标记为样本不足，不进入默认排名。统计是相关性分析，不代表因果关系或投资建议。
