# 会话日志索引与 Docker

## 做什么用

- 完整对话 JSON 仍在 `data/conversations/YYYY-MM-DD/{conversation_id}.json`。
- `data/conversation_index.sqlite3`（或你自定义的路径）只存相对路径和列表/导出用的元数据，管理接口优先查库，必要时回退为原来的 `glob`。

## 环境变量

| 变量 | 含义 |
|------|------|
| `CONVERSATION_INDEX_PATH` | SQLite 文件路径。相对路径相对于 `DATA_DIR`（默认即 `data/`）。不设则使用 `data/conversation_index.sqlite3`。 |
| `CONVERSATION_INDEX_DISABLED` | 设为 `1` / `true` 时关闭索引，全部走磁盘扫描（调试用）。 |

应用启动时会建表；若索引为空但 `data/conversations` 里已有 JSON，会自动做一次全量重建。

## Docker 建议

1. **持久化数据目录**（推荐）：在代码里 `settings.json` 的路径是 `DATA_DIR/settings.json`（仓库内即 `data/settings.json`，`DATA_DIR` 定义在 `settings.py`）。Docker 时把宿主机目录 **挂载到容器内与 `settings.json` 同级的那个目录**——也就是容器里的 `DATA_DIR`，不要挂到仓库根或其它层级，否则配置和会话会写到未挂载的路径。

   若镜像里工作目录是 `/app` 且未改 `DATA_DIR`，则一般为 `/app/data`：

   ```yaml
   volumes:
     - ./data:/app/data
   ```

   这样会话目录 `conversations/`、索引库、`settings.json`、`log_notes.json` 等都在同一卷里，索引与文件不会脱节。若你的 `WORKDIR` 或打包方式不同，先确认容器内 `settings.json` 的实际路径，再把宿主 `./data` 挂到 **该文件所在目录**。

2. **只挂 conversations、索引单独放**：可以把 `CONVERSATION_INDEX_PATH` 指到卷上的文件，例如 `/data/index/conversation_index.sqlite3`，并挂载该目录；仍要保证 `data/conversations` 可写且与索引一致。

3. **不要**只挂 JSON 不挂索引：每次容器重建会触发「空索引 + 有文件」的自动重建，数据大时启动会多耗一点时间。

## 索引与磁盘不一致时

- 清空历史（管理面板「清空」）会在删完文件后清空索引表。
- 若手工删过 JSON，可重启服务触发「空索引则重建」，或在代码里调用 `rebuild_from_disk()`（当前管理 API 未单独暴露；练手项目可直接用 Python shell 调 `utils.conversation_index.rebuild_from_disk()`）。

## 时间范围导出

- 依赖每条记录在索引里的 `ts_min` / `ts_max`（由会话文档内时间字段汇总）。
- 很老的文件若从未经当前版本写入过，可重建索引以补全时间边界。
