# 知识星球股票观点关键词分析工具

用于分析**已授权导出的**知识星球主题与股票后续表现。第一版不绕过登录、验证码或平台访问控制，支持 JSON/CSV 导入；行情通过 AKShare 适配器获取，未配置行情源时可导入行情 CSV。

## 快速启动

```bash
cp .env.example .env
docker compose up --build
```

打开 http://localhost:8000/docs。

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

## 环境变量

- `DATABASE_URL`：PostgreSQL 连接串
- `KNOWLEDGE_PLANET_TOKEN`：仅供后续授权适配器使用，当前不会打印或写入数据库
- `MARKET_DATA_PROVIDER`：`akshare` 或 `csv`
- `MARKET_DATA_CSV`：CSV 行情文件路径（`code,date,open,high,low,close,volume,amount,turnover_rate`）

## 说明

默认上涨标签为：主题事件日之后未来 5 个交易日最高收盘收益率 >= 10%。样本少于 10 篇的关键词会标记为样本不足，不进入默认排名。统计是相关性分析，不代表因果关系或投资建议。
