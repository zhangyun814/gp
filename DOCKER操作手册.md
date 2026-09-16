# Docker 操作手册

项目目录：

```bash
cd /Users/zhangyun/Documents/code/planet-stock-analyzer
```

## 1. 启动项目

后台启动：

```bash
docker compose up -d
```

首次启动、修改依赖或修改 Dockerfile 后：

```bash
docker compose up -d --build
```

前台启动并实时查看日志：

```bash
docker compose up --build
```

前台模式下按 `Ctrl + C` 会停止容器。

## 2. 查看状态

```bash
docker compose ps
```

预期状态：

```text
postgres   healthy
app        running
```

## 3. 查看日志

查看 App 日志：

```bash
docker compose logs -f app
```

查看 PostgreSQL 日志：

```bash
docker compose logs -f postgres
```

查看全部服务日志：

```bash
docker compose logs -f
```

后台模式下按 `Ctrl + C` 只会退出日志查看，不会停止容器。

## 4. 停止项目

```bash
docker compose down
```

这会停止并删除容器、网络，但保留 PostgreSQL 数据卷。

## 5. 重启项目

```bash
docker compose restart
```

如果修改了代码依赖或 Docker 配置，使用：

```bash
docker compose up -d --build
```

## 6. 修改代码后重启 App

当前项目会在构建镜像时把代码复制到容器中。修改 Python 代码后执行：

```bash
docker compose up -d --build app
```

查看 App 是否启动成功：

```bash
docker compose logs -f app
```

如果只是重启现有容器、没有修改代码：

```bash
docker compose restart app
```

修改了 `docker-compose.yml`、数据库配置或 `requirements.txt` 时，重新构建全部服务：

```bash
docker compose up -d --build
```

## 7. 访问地址

网站：<http://localhost:8000>

接口文档：<http://localhost:8000/docs>

Navicat：

```text
主机：127.0.0.1
端口：5432
数据库：planet_stock
用户名：planet
密码：planet
```

## 8. 进入数据库

```bash
docker compose exec postgres psql -U planet -d planet_stock
```

## 9. 危险命令

不要在确认数据已备份前执行：

```bash
docker compose down -v
```

该命令会删除 PostgreSQL 数据卷，可能造成数据库数据丢失。
